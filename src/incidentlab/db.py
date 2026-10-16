"""Database connection management, migrations, and checkpointer setup."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver

from incidentlab.config import Settings


def get_control_connection(settings: Settings) -> psycopg.Connection:
    """Obtain a direct psycopg connection for the control role."""
    return psycopg.connect(settings.il_database_url, autocommit=False)


def get_sandbox_connection(dsn: str) -> psycopg.Connection:
    """Obtain a direct psycopg connection for the sandbox role."""
    return psycopg.connect(dsn, autocommit=False)


def check_database_readiness(dsn: str, timeout_seconds: int = 30) -> bool:
    """Wait for PostgreSQL readiness with bounded polling."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    return True
        except Exception:
            time.sleep(1)
    return False


def run_migrations(
    migration_dsn: str,
    control_password: str | None = None,
    sandbox_password: str | None = None,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Execute numbered SQL migrations and initialize official LangGraph checkpointer."""
    if not check_database_readiness(migration_dsn, timeout_seconds=30):
        raise RuntimeError("PostgreSQL database is not reachable within 30 seconds.")

    if migrations_dir is None:
        migrations_dir = Path(__file__).resolve().parent.parent.parent / "migrations"

    migration_files = sorted(migrations_dir.glob("*.sql"))
    if not migration_files:
        raise FileNotFoundError(f"No migration files found in {migrations_dir}")

    applied: list[str] = []
    with psycopg.connect(migration_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Ensure bootstrap roles exist if passwords provided
            from psycopg import sql

            if control_password:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'incidentlab_control'")
                if not cur.fetchone():
                    cur.execute(
                        sql.SQL("CREATE ROLE incidentlab_control WITH LOGIN PASSWORD {}").format(
                            sql.Literal(control_password)
                        )
                    )
                else:
                    cur.execute(
                        sql.SQL("ALTER ROLE incidentlab_control WITH PASSWORD {}").format(
                            sql.Literal(control_password)
                        )
                    )

            if sandbox_password:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'incidentlab_sandbox'")
                if not cur.fetchone():
                    cur.execute(
                        sql.SQL("CREATE ROLE incidentlab_sandbox WITH LOGIN PASSWORD {}").format(
                            sql.Literal(sandbox_password)
                        )
                    )
                else:
                    cur.execute(
                        sql.SQL("ALTER ROLE incidentlab_sandbox WITH PASSWORD {}").format(
                            sql.Literal(sandbox_password)
                        )
                    )

            # Ensure schema_migrations table exists
            cur.execute("CREATE SCHEMA IF NOT EXISTS control;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS control.schema_migrations (
                    version INTEGER PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            for sql_file in migration_files:
                version_str = sql_file.name.split("_")[0]
                version = int(version_str)

                content = sql_file.read_text(encoding="utf-8")
                checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()

                cur.execute(
                    "SELECT checksum FROM control.schema_migrations WHERE version = %s",
                    (version,),
                )
                row = cur.fetchone()
                if row:
                    stored_checksum = row[0]
                    if stored_checksum != checksum:
                        raise ValueError(
                            f"Migration {sql_file.name} checksum mismatch! "
                            f"Stored: {stored_checksum}, Current: {checksum}"
                        )
                    continue

                # Apply migration
                cur.execute(content)
                cur.execute(
                    """
                    INSERT INTO control.schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    """,
                    (version, checksum),
                )
                applied.append(sql_file.name)

            conn.commit()

    # Set up official LangGraph Postgres checkpointer under restricted checkpoints search path
    dsn_with_search_path = migration_dsn
    if "?" in dsn_with_search_path:
        dsn_with_search_path += "&options=-csearch_path%3Dcheckpoints"
    else:
        dsn_with_search_path += "?options=-csearch_path%3Dcheckpoints"

    try:
        with PostgresSaver.from_conn_string(dsn_with_search_path) as saver:
            saver.setup()
    except Exception as exc:
        raise RuntimeError(f"Failed to setup LangGraph PostgreSQL checkpointer: {exc}") from exc

    # Ensure role grants are applied across all tables (including newly created ones)
    with psycopg.connect(migration_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'incidentlab_control') THEN
                        GRANT USAGE ON SCHEMA control, checkpoints TO incidentlab_control;
                        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES
                            IN SCHEMA control, checkpoints TO incidentlab_control;
                        GRANT USAGE, SELECT ON ALL SEQUENCES
                            IN SCHEMA control, checkpoints TO incidentlab_control;
                        ALTER DEFAULT PRIVILEGES IN SCHEMA control
                            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO incidentlab_control;
                        ALTER DEFAULT PRIVILEGES IN SCHEMA checkpoints
                            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO incidentlab_control;
                    END IF;
                    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'incidentlab_sandbox') THEN
                        GRANT USAGE ON SCHEMA sandbox TO incidentlab_sandbox;
                        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES
                            IN SCHEMA sandbox TO incidentlab_sandbox;
                        GRANT USAGE, SELECT ON ALL SEQUENCES
                            IN SCHEMA sandbox TO incidentlab_sandbox;
                        ALTER DEFAULT PRIVILEGES IN SCHEMA sandbox
                            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO incidentlab_sandbox;
                    END IF;
                END
                $$;
                """
            )
    return applied
