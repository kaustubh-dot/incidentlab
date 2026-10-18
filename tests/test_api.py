"""Unit and contract tests for the control API and owner boundary."""

import pytest
from httpx import ASGITransport, AsyncClient

from incidentlab.api import create_app
from incidentlab.events import redact_sensitive_data
from incidentlab.schemas import ApiErrorResponse, EvidenceItem


@pytest.mark.asyncio
async def test_healthz_unauthenticated(test_settings):
    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_unauthenticated_incident_request_returns_401(test_settings):
    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/incidents/00000000-0000-0000-0000-000000000001")
        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "unauthorized"
        assert data["error"]["retryable"] is False
        # Validate against public error schema
        err = ApiErrorResponse.model_validate(data)
        assert err.error.code == "unauthorized"


@pytest.mark.asyncio
async def test_invalid_bearer_token_returns_401(test_settings):
    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer wrong-token"}
        response = await client.get(
            "/api/v1/incidents/00000000-0000-0000-0000-000000000001",
            headers=headers,
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_event_redaction():
    payload = {
        "user": "test",
        "IL_OWNER_TOKEN": "secret-token",
        "nested": {"password": "pwd", "normal_key": "safe"},
        "token": "tok123",
        "authorization": "Bearer xyz",
    }
    redacted = redact_sensitive_data(payload)
    assert redacted["user"] == "test"
    assert redacted["IL_OWNER_TOKEN"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["normal_key"] == "safe"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["authorization"] == "[REDACTED]"


def test_evidence_item_strict_schema():
    valid = {
        "evidence_id": "ev:1",
        "kind": "health",
        "source": "get_health",
        "observed_at": "2026-10-14T00:00:00Z",
        "data": {"status": "ok"},
    }
    item = EvidenceItem.model_validate(valid)
    assert item.evidence_id == "ev:1"

    # Extra fields must be forbidden
    invalid = dict(valid)
    invalid["unexpected_field"] = "bad"
    with pytest.raises(Exception):
        EvidenceItem.model_validate(invalid)


# --- Integration tests requiring PostgreSQL database ---


@pytest.mark.integration
def test_control_role_cannot_read_sandbox_tables():
    import psycopg

    from incidentlab.config import Settings

    settings = Settings.from_env()
    # Attempting to read sandbox tables as control role must fail with InsufficientPrivilege
    with psycopg.connect(settings.il_database_url) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM sandbox.target_state")


@pytest.mark.integration
def test_sandbox_role_cannot_read_control_tables():
    import psycopg

    from incidentlab.config import Settings

    settings = Settings.from_env()
    # Attempting to read control tables as sandbox role must fail with InsufficientPrivilege
    with psycopg.connect(settings.il_sandbox_database_url) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM control.incidents")


@pytest.mark.integration
def test_event_ordering_and_active_incident_constraint():
    import uuid

    import psycopg

    from incidentlab.config import Settings
    from incidentlab.events import append_event, read_events

    settings = Settings.from_env()
    inc1 = uuid.uuid4()
    inc2 = uuid.uuid4()

    with psycopg.connect(settings.il_database_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Insert first active incident
            cur.execute(
                """
                INSERT INTO control.incidents (id, state, symptom, request_id)
                VALUES (%s, 'investigating', 'Checkout failing', %s)
                """,
                (str(inc1), str(uuid.uuid4())),
            )

            # Enforce one-active constraint: second active incident must fail
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO control.incidents (id, state, symptom, request_id)
                        VALUES (%s, 'investigating', 'Another failure', %s)
                        """,
                        (str(inc2), str(uuid.uuid4())),
                    )

            # Append ordered events to inc1
            seq1 = append_event(cur, inc1, "incident.created", {"note": "started"})
            seq2 = append_event(cur, inc1, "model.started", {"turn": 1})
            seq3 = append_event(cur, inc1, "model.finished", {"turn": 1})

            assert seq1 == 1
            assert seq2 == 2
            assert seq3 == 3

            # Read events back
            page = read_events(cur, inc1, after_seq=0, limit=10)
            assert len(page.events) == 3
            assert [e.seq for e in page.events] == [1, 2, 3]
            assert page.has_more is False

            # Verify cryptographic SHA-256 prev_hash chain integrity
            ev1, ev2, ev3 = page.events
            assert ev1.payload["prev_hash"] == "0" * 64
            assert ev1.payload["event_hash"] is not None
            assert ev2.payload["prev_hash"] == ev1.payload["event_hash"]
            assert ev2.payload["event_hash"] is not None
            assert ev3.payload["prev_hash"] == ev2.payload["event_hash"]
            assert ev3.payload["event_hash"] is not None

            # Complete inc1 and verify a new active incident can now be created
            cur.execute(
                "UPDATE control.incidents SET state = 'verified_recovered' WHERE id = %s",
                (str(inc1),),
            )
            cur.execute(
                """
                INSERT INTO control.incidents (id, state, symptom, request_id)
                VALUES (%s, 'investigating', 'Next incident', %s)
                """,
                (str(inc2), str(uuid.uuid4())),
            )
        conn.rollback()  # Clean up test rows


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_nonexistent_incident_returns_404(live_settings):
    app = create_app(live_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
        response = await client.post(
            "/api/v1/incidents/00000000-0000-0000-0000-000000000001/cancel",
            headers=headers,
        )
        assert response.status_code == 404
        data = response.json()
        assert data["error"]["code"] == "incident_not_found"
        assert live_settings.il_owner_token not in str(data)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_nonexistent_execution_returns_404(live_settings):
    app = create_app(live_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
        response = await client.post(
            "/api/v1/incidents/00000000-0000-0000-0000-000000000001/reconcile",
            headers=headers,
        )
        assert response.status_code == 404
        data = response.json()
        assert data["error"]["code"] == "execution_not_found"
        assert live_settings.il_owner_token not in str(data)



@pytest.mark.asyncio
async def test_error_responses_never_echo_credentials(test_settings):
    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid bearer token
        secret_attempt = "super-secret-user-token-12345"
        headers = {"Authorization": f"Bearer {secret_attempt}"}
        response = await client.get(
            "/api/v1/incidents/00000000-0000-0000-0000-000000000001",
            headers=headers,
        )
        assert response.status_code == 401
        data = response.json()
        assert secret_attempt not in str(data)
        assert test_settings.il_owner_token not in str(data)
        assert data["error"]["code"] == "unauthorized"


def test_telemetry_keys_preserved_in_redaction():
    payload = {
        "token": "secret-auth-token",
        "input_tokens": 1420,
        "output_tokens": 85,
        "token_count": 1505,
        "tokens_per_second": 28.5,
        "authorization": "Bearer secret",
    }
    redacted = redact_sensitive_data(payload)
    assert redacted["token"] == "[REDACTED]"
    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["input_tokens"] == 1420
    assert redacted["output_tokens"] == 85
    assert redacted["token_count"] == 1505
    assert redacted["tokens_per_second"] == 28.5


@pytest.mark.integration
def test_migration_additive_upgrade_and_stale_state():
    """Verify migrations 001 and 002 apply cleanly and allow multiple 'stale' incidents."""
    import uuid

    import psycopg

    from incidentlab.config import Settings
    from incidentlab.db import run_migrations

    settings = Settings.from_env()
    # 1. Running migrations applies any pending migrations and records checksums
    applied = run_migrations(settings.il_migration_database_url)
    assert isinstance(applied, list)

    with psycopg.connect(settings.il_migration_database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version, checksum FROM control.schema_migrations ORDER BY version")
            rows = cur.fetchall()
            versions = [r[0] for r in rows]
            assert 1 in versions
            assert 2 in versions

            # 2. Verify multiple 'stale' incidents can coexist simultaneously
            inc1 = str(uuid.uuid4())
            inc2 = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO control.incidents (id, state, symptom, request_id)
                VALUES (%s, 'stale', 'Stale incident 1', %s)
                """,
                (inc1, str(uuid.uuid4())),
            )
            cur.execute(
                """
                INSERT INTO control.incidents (id, state, symptom, request_id)
                VALUES (%s, 'stale', 'Stale incident 2', %s)
                """,
                (inc2, str(uuid.uuid4())),
            )

            # 3. Verify that multiple active nonterminal incidents still trigger UniqueViolation
            inc_act1 = str(uuid.uuid4())
            inc_act2 = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO control.incidents (id, state, symptom, request_id)
                VALUES (%s, 'investigating', 'Active incident 1', %s)
                """,
                (inc_act1, str(uuid.uuid4())),
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO control.incidents (id, state, symptom, request_id)
                        VALUES (%s, 'investigating', 'Active incident 2', %s)
                        """,
                        (inc_act2, str(uuid.uuid4())),
                    )
        conn.rollback()


@pytest.mark.integration
def test_migration_genuine_v1_to_v2_upgrade(live_settings, tmp_path):
    """Verify genuine v1 -> v2 migration upgrade from a pre-existing 001 installation (FINAL-03)."""
    import hashlib
    import urllib.parse
    import uuid
    from pathlib import Path

    import psycopg

    from incidentlab.db import run_migrations

    parsed = urllib.parse.urlsplit(live_settings.il_migration_database_url)
    tmp_db_name = f"test_v1_upgrade_{uuid.uuid4().hex[:8]}"
    tmp_db_dsn = urllib.parse.urlunsplit(parsed._replace(path=f"/{tmp_db_name}"))

    # Create temporary database
    with psycopg.connect(live_settings.il_migration_database_url, autocommit=True) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{tmp_db_name}"')

    try:
        repo_migrations = Path(__file__).resolve().parent.parent / "migrations"
        v1_sql_path = repo_migrations / "001_initial.sql"
        canonical_v1_content = v1_sql_path.read_text(encoding="utf-8")
        canonical_v1_checksum = hashlib.sha256(canonical_v1_content.encode("utf-8")).hexdigest()

        # Step 1: Create an isolated v1-only migrations directory
        v1_dir = tmp_path / "v1_migrations"
        v1_dir.mkdir()
        (v1_dir / "001_initial.sql").write_text(canonical_v1_content, encoding="utf-8")

        # Step 2: Run migrations with 001 only (simulating old release)
        applied_v1 = run_migrations(
            tmp_db_dsn,
            migrations_dir=v1_dir,
        )
        assert applied_v1 == ["001_initial.sql"]

        # Confirm control.schema_migrations contains only version 1 with canonical checksum
        with psycopg.connect(tmp_db_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version, checksum FROM control.schema_migrations ORDER BY version"
                )
                rows = cur.fetchall()
                assert len(rows) == 1
                assert rows[0][0] == 1
                assert rows[0][1] == canonical_v1_checksum

                # Step 3 & 4: Confirm old ix_one_active_incident definition treats 'stale' as active
                inc1 = str(uuid.uuid4())
                inc2 = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO control.incidents (id, state, symptom, request_id)
                    VALUES (%s, 'stale', 'Stale 1 under v1', %s)
                    """,
                    (inc1, str(uuid.uuid4())),
                )
                with pytest.raises(psycopg.errors.UniqueViolation):
                    with conn.transaction():
                        cur.execute(
                            """
                            INSERT INTO control.incidents (id, state, symptom, request_id)
                            VALUES (%s, 'stale', 'Stale 2 under v1', %s)
                            """,
                            (inc2, str(uuid.uuid4())),
                        )
            conn.rollback()

        # Step 5: Run current migration runner with both migrations available
        applied_v2 = run_migrations(
            tmp_db_dsn,
            migrations_dir=repo_migrations,
        )
        # Verify 001 was skipped as already valid (no checksum error), and 002 was applied
        assert applied_v2 == ["002_stale_terminal_state.sql"]

        # Step 6: Verify schema_migrations contains 1 and 2, and new index excludes 'stale'
        with psycopg.connect(tmp_db_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version, checksum FROM control.schema_migrations ORDER BY version"
                )
                rows = cur.fetchall()
                assert len(rows) == 2
                assert rows[0][0] == 1
                assert rows[0][1] == canonical_v1_checksum
                assert rows[1][0] == 2

                # Verify two 'stale' incidents can now coexist
                inc_stale1 = str(uuid.uuid4())
                inc_stale2 = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO control.incidents (id, state, symptom, request_id)
                    VALUES (%s, 'stale', 'Stale 1 under v2', %s)
                    """,
                    (inc_stale1, str(uuid.uuid4())),
                )
                cur.execute(
                    """
                    INSERT INTO control.incidents (id, state, symptom, request_id)
                    VALUES (%s, 'stale', 'Stale 2 under v2', %s)
                    """,
                    (inc_stale2, str(uuid.uuid4())),
                )

                # Verify two 'investigating' incidents still cannot coexist
                inc_act1 = str(uuid.uuid4())
                inc_act2 = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO control.incidents (id, state, symptom, request_id)
                    VALUES (%s, 'investigating', 'Active 1 under v2', %s)
                    """,
                    (inc_act1, str(uuid.uuid4())),
                )
                with pytest.raises(psycopg.errors.UniqueViolation):
                    with conn.transaction():
                        cur.execute(
                            """
                            INSERT INTO control.incidents (id, state, symptom, request_id)
                            VALUES (%s, 'investigating', 'Active 2 under v2', %s)
                            """,
                            (inc_act2, str(uuid.uuid4())),
                        )
            conn.rollback()
    finally:
        migration_url = live_settings.il_migration_database_url
        with psycopg.connect(migration_url, autocommit=True) as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s AND pid <> pg_backend_pid()
                    """,
                    (tmp_db_name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{tmp_db_name}"')


