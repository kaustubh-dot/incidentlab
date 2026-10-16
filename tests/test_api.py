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


