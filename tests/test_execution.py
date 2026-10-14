"""Tests for repair execution, receipt reconciliation, and functional verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from httpx import ASGITransport
from psycopg.types.json import Jsonb

from incidentlab.api import create_app
from incidentlab.approvals import claim_execution, decide_proposal
from incidentlab.db import get_control_connection, get_sandbox_connection
from incidentlab.executor import execute_claim, reconcile_execution
from incidentlab.graph import compute_payload_digest
from incidentlab.sandbox import service as sandbox_service
from incidentlab.schemas import (
    ApprovalDecision,
    ExecutionClaim,
    SetInventoryEndpointPayload,
)
from incidentlab.verifier import verify_checkout


def _get_target_state(live_settings) -> dict:
    """Read current target state from sandbox."""
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        return sandbox_service.get_target_state(s_conn)


def _seed_incident_proposal_claim(
    conn: psycopg.Connection,
    incident_id: UUID,
    revision: int,
    action: str = "set_inventory_endpoint",
    endpoint_key: str = "inventory-local",
    expires_in_seconds: int = 600,
    cancel_requested: bool = False,
) -> ExecutionClaim:
    """Helper to seed an approved incident, proposal, and claim."""
    payload = SetInventoryEndpointPayload(
        action=action,
        endpoint_key=endpoint_key,
    )
    digest = compute_payload_digest(payload)
    proposal_id = uuid4()
    execution_key = uuid4()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=expires_in_seconds)

    with conn.cursor() as cur:
        # Clear previous active incidents to satisfy one-active-incident constraint
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
            VALUES (%s, 'executing', 'Inventory unavailable', %s, %s)
            """,
            (incident_id, uuid4(), cancel_requested),
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
        cur.execute(
            """
            INSERT INTO control.approvals (
                proposal_id, decision, owner_id, reviewed_digest,
                reviewed_revision, decided_at
            )
            VALUES (%s, 'approve', 'owner', %s, %s, %s)
            """,
            (proposal_id, digest, revision, now),
        )
        cur.execute(
            """
            INSERT INTO control.executions (
                execution_key, proposal_id, incident_id, state,
                payload_digest, expected_revision, claimed_at
            )
            VALUES (%s, %s, %s, 'claimed', %s, %s, %s)
            """,
            (execution_key, proposal_id, incident_id, digest, revision, now),
        )
    conn.commit()

    return ExecutionClaim(
        execution_key=execution_key,
        proposal_id=proposal_id,
        incident_id=incident_id,
        state="claimed",
        payload_digest=digest,
        expected_revision=revision,
        payload=payload,
        claimed_at=now,
    )


@pytest.mark.asyncio
async def test_green_health_is_not_recovery(live_settings):
    """Essential example: green process/DB health does not prove recovery; checkout fails."""
    incident_id = uuid4()
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        sandbox_service.reset_sandbox(s_conn)
        sandbox_service.inject_fault(s_conn, family="inventory_config", variant_seed=1)
        health = sandbox_service.get_diagnostics_health(s_conn)
        assert health.database_reachable is True
        assert health.status == "ok"

    # Seed incident in control DB
    with get_control_connection(live_settings) as c_conn:
        with c_conn.cursor() as cur:
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
                INSERT INTO control.incidents (id, state, symptom, request_id)
                VALUES (%s, 'verifying', 'Fault injected', %s)
                """,
                (incident_id, uuid4()),
            )
        c_conn.commit()

    result = await verify_checkout(incident_id, uuid4(), settings=live_settings)
    assert result.verdict != "pass"
    assert result.verdict == "fail"
    assert result.observed_status == "unavailable"

    # Assert incident updated to verification_failed
    with get_control_connection(live_settings) as c_conn:
        with c_conn.cursor() as cur:
            cur.execute(
                "SELECT state, outcome FROM control.incidents WHERE id = %s",
                (incident_id,),
            )
            row = cur.fetchone()
            assert row[0] == "verification_failed"

            # Check verification evidence was stored
            cur.execute(
                "SELECT kind, source, data FROM control.evidence WHERE incident_id = %s",
                (incident_id,),
            )
            ev = cur.fetchone()
            assert ev is not None
            assert ev[0] == "verification"
            assert ev[1] == "verify_checkout"


@pytest.mark.asyncio
async def test_execute_claim_applies_repair_and_increments_revision(live_settings):
    """Executing an approved claim mutates sandbox, updates state, and writes receipt."""
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        sandbox_service.reset_sandbox(s_conn)
        sandbox_service.inject_fault(s_conn, family="inventory_config", variant_seed=1)
        state_before = sandbox_service.get_target_state(s_conn)
        rev_before = state_before["revision"]
        assert state_before["endpoint_key"] != "inventory-local"

    incident_id = uuid4()
    with get_control_connection(live_settings) as c_conn:
        claim = _seed_incident_proposal_claim(c_conn, incident_id, revision=rev_before)

    receipt = await execute_claim(claim, settings=live_settings)
    assert receipt.result == "applied"
    assert receipt.before_revision == rev_before
    assert receipt.after_revision == rev_before + 1
    assert receipt.execution_key == claim.execution_key
    assert receipt.payload_digest == claim.payload_digest

    # Verify sandbox target state updated
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        state_after = sandbox_service.get_target_state(s_conn)
        assert state_after["endpoint_key"] == "inventory-local"
        assert state_after["revision"] == rev_before + 1

    # Verify control state updated to verifying
    with get_control_connection(live_settings) as c_conn:
        with c_conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM control.incidents WHERE id = %s",
                (incident_id,),
            )
            assert cur.fetchone()[0] == "verifying"

            cur.execute(
                "SELECT state, receipt FROM control.executions WHERE execution_key = %s",
                (claim.execution_key,),
            )
            exec_row = cur.fetchone()
            assert exec_row[0] == "applied"
            assert exec_row[1]["result"] == "applied"

            # Verify event timeline contains dispatched and receipt events
            cur.execute(
                "SELECT event_type FROM control.events WHERE incident_id = %s ORDER BY seq ASC",
                (incident_id,),
            )
            event_types = [r[0] for r in cur.fetchall()]
            assert "execution.dispatched" in event_types
            assert "execution.receipt" in event_types


@pytest.mark.asyncio
async def test_execute_claim_idempotent_on_repeat_dispatch(live_settings):
    """Repeat dispatch with same execution key returns identical receipt without second mutation."""
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        sandbox_service.reset_sandbox(s_conn)
        sandbox_service.inject_fault(s_conn, family="inventory_config", variant_seed=1)
        rev = sandbox_service.get_target_state(s_conn)["revision"]

    incident_id = uuid4()
    with get_control_connection(live_settings) as c_conn:
        claim = _seed_incident_proposal_claim(c_conn, incident_id, revision=rev)

    # First dispatch
    receipt1 = await execute_claim(claim, settings=live_settings)
    assert receipt1.result == "applied"
    rev_after_first = receipt1.after_revision

    # Second dispatch with exact same claim
    receipt2 = await execute_claim(claim, settings=live_settings)
    assert receipt2.result == "applied"
    assert receipt2.after_revision == rev_after_first
    assert receipt2.execution_key == claim.execution_key

    # Sandbox revision must NOT have incremented again
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        current_rev = sandbox_service.get_target_state(s_conn)["revision"]
        assert current_rev == rev_after_first


@pytest.mark.asyncio
async def test_execute_claim_stale_target_revision_precondition_failed(live_settings):
    """Mismatched expected_revision produces precondition_failed receipt and fails incident."""
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        sandbox_service.reset_sandbox(s_conn)
        sandbox_service.inject_fault(s_conn, family="inventory_config", variant_seed=1)
        actual_rev = sandbox_service.get_target_state(s_conn)["revision"]

    incident_id = uuid4()
    stale_rev = actual_rev + 42  # Mismatched revision
    with get_control_connection(live_settings) as c_conn:
        claim = _seed_incident_proposal_claim(c_conn, incident_id, revision=stale_rev)

    receipt = await execute_claim(claim, settings=live_settings)
    assert receipt.result == "precondition_failed"
    assert receipt.before_revision == actual_rev
    assert receipt.after_revision == actual_rev

    # Target state in sandbox must NOT be changed
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        state_after = sandbox_service.get_target_state(s_conn)
        assert state_after["revision"] == actual_rev
        assert state_after["endpoint_key"] != "inventory-local"

    # Control incident must be failed
    with get_control_connection(live_settings) as c_conn:
        with c_conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (incident_id,))
            assert cur.fetchone()[0] == "failed"


@pytest.mark.asyncio
async def test_execute_claim_cancelled_before_dispatch(live_settings):
    """Cancellation requested before dispatch prevents repair POST and marks cancelled."""
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        sandbox_service.reset_sandbox(s_conn)
        rev = sandbox_service.get_target_state(s_conn)["revision"]

    incident_id = uuid4()
    with get_control_connection(live_settings) as c_conn:
        claim = _seed_incident_proposal_claim(
            c_conn, incident_id, revision=rev, cancel_requested=True
        )

    with pytest.raises(RuntimeError, match="cancelled"):
        await execute_claim(claim, settings=live_settings)

    # Sandbox revision unchanged
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        assert sandbox_service.get_target_state(s_conn)["revision"] == rev

    # Incident state updated to cancelled
    with get_control_connection(live_settings) as c_conn:
        with c_conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (incident_id,))
            assert cur.fetchone()[0] == "cancelled"


@pytest.mark.asyncio
async def test_reconcile_execution_reads_durable_receipt(live_settings):
    """Reconcile reads existing receipt from sandbox without dispatching mutation."""
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        sandbox_service.reset_sandbox(s_conn)
        rev = sandbox_service.get_target_state(s_conn)["revision"]

    incident_id = uuid4()
    with get_control_connection(live_settings) as c_conn:
        claim = _seed_incident_proposal_claim(c_conn, incident_id, revision=rev)

    # Dispatch to create durable receipt in sandbox
    receipt = await execute_claim(claim, settings=live_settings)
    assert receipt.result == "applied"

    # Reconcile reads receipt
    view = await reconcile_execution(claim.execution_key, settings=live_settings)
    assert view.reconciled is True
    assert view.receipt is not None
    assert view.receipt.result == "applied"
    assert view.receipt.execution_key == claim.execution_key
    assert view.execution_key == claim.execution_key


@pytest.mark.asyncio
async def test_full_recovery_flow_reaches_verified_recovered(live_settings):
    """Full slice: fault -> proposal -> approval -> execution -> verifier -> recovered."""
    # 1. Reset and inject fault
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        sandbox_service.reset_sandbox(s_conn)
        sandbox_service.inject_fault(s_conn, family="inventory_config", variant_seed=1)
        rev_before = sandbox_service.get_target_state(s_conn)["revision"]

    # 2. Seed proposal and approve
    incident_id = uuid4()
    with get_control_connection(live_settings) as c_conn:
        with c_conn.cursor() as cur:
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
                INSERT INTO control.incidents (id, state, symptom, request_id)
                VALUES (%s, 'awaiting_approval', 'Inventory unavailable', %s)
                """,
                (incident_id, uuid4()),
            )
            payload = SetInventoryEndpointPayload(
                action="set_inventory_endpoint",
                endpoint_key="inventory-local",
            )
            digest = compute_payload_digest(payload)
            proposal_id = uuid4()
            now = datetime.now(UTC)
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
                    rev_before,
                    now,
                    now + timedelta(seconds=600),
                    Jsonb(["ev_1"]),
                ),
            )
        c_conn.commit()

    # 3. Owner decides approve
    decision = ApprovalDecision(
        proposal_id=proposal_id,
        payload_digest=digest,
        target_revision=rev_before,
        decision="approve",
    )
    approval_view = await decide_proposal(
        incident_id=incident_id,
        owner_id="owner",
        decision=decision,
        settings=live_settings,
    )
    assert approval_view.decision == "approve"

    # 4. Claim execution
    claim = await claim_execution(
        incident_id=incident_id,
        proposal_id=proposal_id,
        settings=live_settings,
    )
    assert claim.state == "claimed"

    # 5. Execute claim
    receipt = await execute_claim(claim, settings=live_settings)
    assert receipt.result == "applied"
    assert receipt.after_revision == rev_before + 1

    # 6. Verify checkout recovery
    verif = await verify_checkout(incident_id, uuid4(), settings=live_settings)
    assert verif.verdict == "pass"
    assert verif.observed_status == "fulfilled"
    assert verif.order_id is not None

    # 7. Check incident state is verified_recovered
    with get_control_connection(live_settings) as c_conn:
        with c_conn.cursor() as cur:
            cur.execute(
                "SELECT state, outcome FROM control.incidents WHERE id = %s",
                (incident_id,),
            )
            row = cur.fetchone()
            assert row[0] == "verified_recovered"
            assert row[1]["status"] == "verified_recovered"


@pytest.mark.asyncio
async def test_reconcile_api_endpoint(live_settings):
    """POST /api/v1/incidents/{id}/reconcile requires owner auth and returns ExecutionView."""
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        sandbox_service.reset_sandbox(s_conn)
        rev = sandbox_service.get_target_state(s_conn)["revision"]

    incident_id = uuid4()
    with get_control_connection(live_settings) as c_conn:
        claim = _seed_incident_proposal_claim(c_conn, incident_id, revision=rev)

    await execute_claim(claim, settings=live_settings)

    app = create_app(live_settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated request fails with 401
        res = await client.post(f"/api/v1/incidents/{incident_id}/reconcile")
        assert res.status_code == 401

        # 2. Authenticated request succeeds with ExecutionView
        res = await client.post(
            f"/api/v1/incidents/{incident_id}/reconcile",
            headers={"Authorization": f"Bearer {live_settings.il_owner_token}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["execution_key"] == str(claim.execution_key)
        assert data["reconciled"] is True
        assert data["receipt"]["result"] == "applied"
