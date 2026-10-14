"""Focused tests for bound owner approval, immutable repair claims, and prechecks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from httpx import ASGITransport
from psycopg.types.json import Jsonb

from incidentlab.api import create_app
from incidentlab.approvals import (
    ApprovalError,
    claim_execution,
)
from incidentlab.db import get_control_connection, get_sandbox_connection
from incidentlab.graph import compute_payload_digest
from incidentlab.sandbox import service as sandbox_service
from incidentlab.schemas import (
    SetInventoryEndpointPayload,
)


def _get_target_revision(live_settings) -> int:
    """Read current target revision from sandbox."""
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        return sandbox_service.get_target_state(s_conn)["revision"]


def _seed_incident_and_proposal(
    conn: psycopg.Connection,
    incident_id: UUID,
    revision: int,
    expires_in_seconds: int = 600,
    cancel_requested: bool = False,
    state: str = "awaiting_approval",
) -> tuple[UUID, str]:
    """Helper to seed an incident and immutable proposal under test."""
    payload = SetInventoryEndpointPayload(
        action="set_inventory_endpoint",
        endpoint_key="inventory-local",
    )
    digest = compute_payload_digest(payload)
    proposal_id = uuid4()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=expires_in_seconds)

    with conn.cursor() as cur:
        # Reset prior active incidents to satisfy one-active constraint
        cur.execute(
            """
            UPDATE control.incidents
            SET state = 'closed_unknown'
            WHERE state NOT IN (
                'verified_recovered', 'verification_failed', 'escalated',
                'failed', 'rejected', 'expired', 'cancelled', 'closed_unknown'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO control.incidents (id, state, symptom, request_id, cancel_requested)
            VALUES (%s, %s, 'Inventory unavailable', %s, %s)
            """,
            (incident_id, state, uuid4(), cancel_requested),
        )
        cur.execute(
            """
            INSERT INTO control.proposals (
                id, incident_id, payload, payload_digest,
                target_revision, created_at, expires_at, evidence_ids
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                proposal_id,
                incident_id,
                Jsonb(payload.model_dump()),
                digest,
                revision,
                now,
                expires_at,
                Jsonb(["ev_health_1"]),
            ),
        )
    conn.commit()
    return proposal_id, digest


def _execution_count(conn: psycopg.Connection, proposal_id: UUID) -> int:
    """Read-only test helper to check executions count for a proposal."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM control.executions WHERE proposal_id = %s",
            (proposal_id,),
        )
        return int(cur.fetchone()[0])


@pytest.mark.integration
async def test_owner_auth_enforced_on_decision(live_settings):
    """Verify owner token required to submit approval decision."""
    app = create_app(live_settings)
    transport = ASGITransport(app=app)
    inc_id = uuid4()
    prop_id = uuid4()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # Unauthenticated
        resp = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            json={
                "proposal_id": str(prop_id),
                "payload_digest": "0" * 64,
                "target_revision": 1,
                "decision": "approve",
            },
        )
        assert resp.status_code == 401

        # Invalid token
        resp = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            headers={"Authorization": "Bearer invalid_token"},
            json={
                "proposal_id": str(prop_id),
                "payload_digest": "0" * 64,
                "target_revision": 1,
                "decision": "approve",
            },
        )
        assert resp.status_code == 401


@pytest.mark.integration
async def test_decide_proposal_approve_claims_execution(live_settings):
    """Verify owner approval records approval, claims unique execution, and updates state."""
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)

    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(conn, inc_id, revision=rev)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id),
                "payload_digest": digest,
                "target_revision": rev,
                "decision": "approve",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["proposal_id"] == str(prop_id)
        assert data["decision"] == "approve"
        assert data["owner_id"] == "owner"
        assert data["reviewed_digest"] == digest
        assert data["reviewed_revision"] == rev

    # Verify execution claim and incident state in database
    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id,))
            assert cur.fetchone()[0] in ("executing", "verifying", "verified_recovered")

            cur.execute(
                """
                SELECT execution_key, state, payload_digest, expected_revision
                FROM control.executions
                WHERE proposal_id = %s
                """,
                (prop_id,),
            )
            claim = cur.fetchone()
            assert claim is not None
            assert claim[1] in ("claimed", "dispatching", "applied")
            assert claim[2] == digest
            assert claim[3] == rev

        assert _execution_count(conn, prop_id) == 1


@pytest.mark.integration
async def test_decide_proposal_reject_creates_zero_claims(live_settings):
    """Verify rejecting proposal sets state to rejected and creates ZERO execution claims."""
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)

    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(conn, inc_id, revision=rev)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id),
                "payload_digest": digest,
                "target_revision": rev,
                "decision": "reject",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "reject"

    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id,))
            assert cur.fetchone()[0] == "rejected"

        # Explicit requirement: assert execution_count == 0
        assert _execution_count(conn, prop_id) == 0


@pytest.mark.integration
async def test_decide_proposal_conflicting_decision_fails(live_settings):
    """Verify second conflicting decision on same proposal is rejected with 409."""
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)

    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(conn, inc_id, revision=rev)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # First decision: approve
        r1 = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id),
                "payload_digest": digest,
                "target_revision": rev,
                "decision": "approve",
            },
        )
        assert r1.status_code == 200

        # Second decision: conflicting reject
        r2 = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id),
                "payload_digest": digest,
                "target_revision": rev,
                "decision": "reject",
            },
        )
        assert r2.status_code == 409
        err = r2.json()["error"]
        assert err["code"] == "conflicting_decision_error"


@pytest.mark.integration
async def test_decide_proposal_identical_duplicate_is_idempotent(live_settings):
    """Verify repeating identical approval decision returns existing ApprovalView idempotently."""
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)

    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(conn, inc_id, revision=rev)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        body = {
            "proposal_id": str(prop_id),
            "payload_digest": digest,
            "target_revision": rev,
            "decision": "approve",
        }
        r1 = await ac.post(f"/api/v1/incidents/{inc_id}/decision", headers=headers, json=body)
        assert r1.status_code == 200

        r2 = await ac.post(f"/api/v1/incidents/{inc_id}/decision", headers=headers, json=body)
        assert r2.status_code == 200
        assert r1.json() == r2.json()

    with get_control_connection(live_settings) as conn:
        assert _execution_count(conn, prop_id) == 1


@pytest.mark.integration
async def test_decide_proposal_digest_and_revision_mismatches_rejected(live_settings):
    """Verify browser cannot supply altered digest or revision."""
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)

    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(conn, inc_id, revision=rev)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # Mismatched digest
        r1 = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id),
                "payload_digest": "a" * 64,
                "target_revision": rev,
                "decision": "approve",
            },
        )
        assert r1.status_code == 400
        assert r1.json()["error"]["code"] == "payload_digest_mismatch"

        # Mismatched revision
        r2 = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id),
                "payload_digest": digest,
                "target_revision": rev + 999,
                "decision": "approve",
            },
        )
        assert r2.status_code == 400
        assert r2.json()["error"]["code"] == "target_revision_mismatch"


@pytest.mark.integration
async def test_decide_proposal_expired_and_cancelled_proposals_rejected(live_settings):
    """Verify expired proposal transitions incident to expired and rejects decision."""
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    rev = _get_target_revision(live_settings)

    # 1. Expired proposal
    inc_id1 = uuid4()
    with get_control_connection(live_settings) as conn:
        prop_id1, digest1 = _seed_incident_and_proposal(
            conn, inc_id1, revision=rev, expires_in_seconds=-10
        )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r1 = await ac.post(
            f"/api/v1/incidents/{inc_id1}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id1),
                "payload_digest": digest1,
                "target_revision": rev,
                "decision": "approve",
            },
        )
        assert r1.status_code == 409
        assert r1.json()["error"]["code"] == "proposal_expired"

    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id1,))
            assert cur.fetchone()[0] == "expired"
        assert _execution_count(conn, prop_id1) == 0

    # 2. Cancelled incident
    inc_id2 = uuid4()
    with get_control_connection(live_settings) as conn:
        prop_id2, digest2 = _seed_incident_and_proposal(
            conn, inc_id2, revision=rev, cancel_requested=True
        )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r2 = await ac.post(
            f"/api/v1/incidents/{inc_id2}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id2),
                "payload_digest": digest2,
                "target_revision": rev,
                "decision": "approve",
            },
        )
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "incident_cancelled"

    with get_control_connection(live_settings) as conn:
        assert _execution_count(conn, prop_id2) == 0


@pytest.mark.integration
async def test_concurrent_approvals_only_one_claim(live_settings):
    """Verify concurrent approval requests produce exactly one execution claim."""
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)

    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(conn, inc_id, revision=rev)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        body = {
            "proposal_id": str(prop_id),
            "payload_digest": digest,
            "target_revision": rev,
            "decision": "approve",
        }
        resps = await asyncio.gather(
            ac.post(f"/api/v1/incidents/{inc_id}/decision", headers=headers, json=body),
            ac.post(f"/api/v1/incidents/{inc_id}/decision", headers=headers, json=body),
            ac.post(f"/api/v1/incidents/{inc_id}/decision", headers=headers, json=body),
        )
        for r in resps:
            assert r.status_code == 200

    with get_control_connection(live_settings) as conn:
        assert _execution_count(conn, prop_id) == 1


@pytest.mark.integration
async def test_claim_execution_stale_target_revision(live_settings):
    """Verify target revision precheck fails and marks incident stale if target revision changed."""
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)
    with get_control_connection(live_settings) as conn:
        # Seed proposal expecting an altered revision
        prop_id, digest = _seed_incident_and_proposal(
            conn, inc_id, revision=rev + 50, state="approved"
        )
        # Record approval
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.approvals (
                    proposal_id, decision, owner_id, reviewed_digest, reviewed_revision
                )
                VALUES (%s, 'approve', 'owner', %s, %s)
                """,
                (prop_id, digest, rev + 50),
            )
        conn.commit()

    # Precheck compares with actual rev and detects mismatch
    with pytest.raises(ApprovalError) as exc_info:
        await claim_execution(inc_id, prop_id, settings=live_settings)

    assert exc_info.value.code == "target_revision_stale"

    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id,))
            assert cur.fetchone()[0] == "stale"
        assert _execution_count(conn, prop_id) == 0


@pytest.mark.integration
async def test_claim_execution_cancelled_rejects(live_settings):
    """Verify claim_execution rejects if incident cancellation was requested."""
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)
    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(
            conn, inc_id, revision=rev, state="approved", cancel_requested=True
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.approvals (
                    proposal_id, decision, owner_id, reviewed_digest, reviewed_revision
                )
                VALUES (%s, 'approve', 'owner', %s, %s)
                """,
                (prop_id, digest, rev),
            )
        conn.commit()

    with pytest.raises(ApprovalError) as exc_info:
        await claim_execution(inc_id, prop_id, settings=live_settings)

    assert exc_info.value.code == "incident_cancelled"
    with get_control_connection(live_settings) as conn:
        assert _execution_count(conn, prop_id) == 0


@pytest.mark.integration
async def test_claim_execution_expired_proposal_rejects(live_settings):
    """Verify claim_execution rejects if proposal has expired and marks incident expired."""
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)
    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(
            conn, inc_id, revision=rev, state="approved", expires_in_seconds=-10
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.approvals (
                    proposal_id, decision, owner_id, reviewed_digest, reviewed_revision
                )
                VALUES (%s, 'approve', 'owner', %s, %s)
                """,
                (prop_id, digest, rev),
            )
        conn.commit()

    with pytest.raises(ApprovalError) as exc_info:
        await claim_execution(inc_id, prop_id, settings=live_settings)

    assert exc_info.value.code == "proposal_expired"
    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id,))
            assert cur.fetchone()[0] == "expired"
        assert _execution_count(conn, prop_id) == 0


@pytest.mark.integration
async def test_cancel_endpoint_prevents_subsequent_decision(live_settings):
    """Verify owner cancel route cancels awaiting incident and blocks subsequent approval."""
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)

    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(conn, inc_id, revision=rev)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Cancel incident
        cancel_resp = await ac.post(f"/api/v1/incidents/{inc_id}/cancel", headers=headers)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["state"] == "cancelled"

        # 2. Attempt approval
        decision_resp = await ac.post(
            f"/api/v1/incidents/{inc_id}/decision",
            headers=headers,
            json={
                "proposal_id": str(prop_id),
                "payload_digest": digest,
                "target_revision": rev,
                "decision": "approve",
            },
        )
        assert decision_resp.status_code == 409
        assert decision_resp.json()["error"]["code"] == "incident_cancelled"

