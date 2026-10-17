import os
from pathlib import Path

import pytest

from incidentlab.config import Settings

# Ensure local .env variables are available for integration fixtures if present
_env_path = Path(".env")
if _env_path.exists():
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k = _k.strip()
                _v = _v.strip().strip("'\"")
                if _k not in os.environ:
                    os.environ[_k] = _v


@pytest.fixture
def mock_env_mapping():
    return {
        "IL_BIND_HOST": "127.0.0.1",
        "IL_PORT": "8000",
        "IL_DATABASE_URL": "postgresql://incidentlab_control:secret1@127.0.0.1:5432/incidentlab",
        "IL_SANDBOX_DATABASE_URL": "postgresql://incidentlab_sandbox:secret2@127.0.0.1:5432/incidentlab",
        "IL_MIGRATION_DATABASE_URL": "postgresql://incidentlab_migrator:secret3@127.0.0.1:5432/incidentlab",
        "IL_OWNER_TOKEN": "a" * 64,
        "IL_SANDBOX_READ_TOKEN": "b" * 64,
        "IL_SANDBOX_WRITE_TOKEN": "c" * 64,
        "IL_SANDBOX_ADMIN_TOKEN": "d" * 64,
        "IL_SANDBOX_BASE_URL": "http://sandbox:8001",
        "IL_OLLAMA_BASE_URL": "http://ollama:11434",
        "IL_MODEL": "qwen3:8b",
        "IL_LOG_LEVEL": "INFO",
    }


@pytest.fixture
def test_settings(mock_env_mapping):
    return Settings.from_mapping(mock_env_mapping)


@pytest.fixture
def live_settings():
    """Load settings from current environment (used in integration runs)."""
    return Settings.from_env()


@pytest.fixture(autouse=True)
def clean_active_incidents(request):
    """Ensure no stray active incidents block integration tests."""
    if "integration" in request.keywords or "live_model" in request.keywords:
        try:
            settings = Settings.from_env()
            from incidentlab.db import get_control_connection

            with get_control_connection(settings) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE control.incidents
                        SET state = 'closed_unknown'
                        WHERE state NOT IN (
                            'verified_recovered', 'verification_failed', 'escalated',
                            'failed', 'rejected', 'expired', 'cancelled', 'closed_unknown', 'stale'
                        )
                        """
                    )
                conn.commit()
        except Exception:
            pass
        yield
        try:
            settings = Settings.from_env()
            from incidentlab.db import get_control_connection

            with get_control_connection(settings) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE control.incidents
                        SET state = 'closed_unknown'
                        WHERE state NOT IN (
                            'verified_recovered', 'verification_failed', 'escalated',
                            'failed', 'rejected', 'expired', 'cancelled', 'closed_unknown', 'stale'
                        )
                        """
                    )
                conn.commit()
        except Exception:
            pass
    else:
        yield


