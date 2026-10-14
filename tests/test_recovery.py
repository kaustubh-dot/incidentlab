"""Tests for restart recovery, ack loss crash harness, unknown-outcome CLI, and cancellation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from httpx import ASGITransport
from psycopg.types.json import Jsonb

from incidentlab.api import create_app
from incidentlab.approvals import claim_execution
from incidentlab.cli import resolve_unknown_cli
from incidentlab.config import Settings
from incidentlab.db import get_control_connection, get_sandbox_connection
from incidentlab.executor import (
    execute_claim,
    reconcile_execution,
    reconcile_startup,
)
from incidentlab.graph import compute_payload_digest
from incidentlab.sandbox import service as sandbox_service
from incidentlab.schemas import (
    RepairReceipt,
    ResumeOrderWorkerPayload,
    SetInventoryEndpointPayload,
)


def _get_target_revision(live_settings: Settings) -> int:
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        return sandbox_service.get_target_state(s_conn)["revision"]


def _seed_incident_and_proposal(
    conn: psycopg.Connection,
    incident_id: UUID,
    revision: int,
    expires_in_seconds: int = 600,
    cancel_requested: bool = False,
    state: str = "awaiting_approval",
    action: str = "set_inventory_endpoint",
) -> tuple[UUID, str]:
    if action == "set_inventory_endpoint":
        payload = SetInventoryEndpointPayload(
            action="set_inventory_endpoint",
            endpoint_key="inventory-local",
        )
    else:
        payload = ResumeOrderWorkerPayload(
            action="resume_order_worker",
            worker_id="checkout-worker",
        )
    digest = compute_payload_digest(payload)
    proposal_id = uuid4()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=expires_in_seconds)

    with conn.cursor() as cur:
        # Close any prior active incidents to satisfy one-active constraint
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


class CrashRun:
    """Represents an execution run after an ack loss crash before control receipt recording."""

    def __init__(
        self,
        incident_id: UUID,
        execution_key: UUID,
        live_settings: Settings,
        tracking_client: TrackingClient,
    ):
        self.incident_id = incident_id
        self.execution_key = execution_key
        self.live_settings = live_settings
        self.client = tracking_client
        self.receipt: RepairReceipt | None = None

    @property
    def repair_post_count(self) -> int:
        return self.client.repair_post_count

    async def reconcile(self) -> None:
        view = await reconcile_execution(
            self.execution_key,
            settings=self.live_settings,
            client=self.client,
        )
        self.receipt = view.receipt


class TrackingClient(httpx.AsyncClient):
    """HTTP client that counts repair POST calls and delegates to real sandbox."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.repair_post_count = 0

    async def post(self, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        url_str = str(url)
        if "/repairs" in url_str:
            self.repair_post_count += 1
        return await super().post(url, *args, **kwargs)


class CrashHarness:
    """Harness to inject real sandbox mutation followed by control ack loss."""

    def __init__(self, live_settings: Settings):
        self.live_settings = live_settings
        self.client = TrackingClient(timeout=10.0)

    async def close(self):
        await self.client.aclose()

    async def apply_then_restart_before_ack(self) -> CrashRun:
        # 1. Reset sandbox target to inventory-legacy
        with get_sandbox_connection(self.live_settings.il_sandbox_database_url) as s_conn:
            rev = sandbox_service.inject_fault(s_conn, "inventory_config")

        # 2. Seed approved incident and proposal
        inc_id = uuid4()
        with get_control_connection(self.live_settings) as c_conn:
            prop_id, digest = _seed_incident_and_proposal(
                c_conn, inc_id, revision=rev, state="approved"
            )
            with c_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO control.approvals (
                        proposal_id, decision, owner_id, reviewed_digest, reviewed_revision
                    ) VALUES (%s, 'approve', 'owner', %s, %s)
                    """,
                    (prop_id, digest, rev),
                )
            c_conn.commit()

        # 3. Create execution claim
        claim = await claim_execution(
            incident_id=inc_id,
            proposal_id=prop_id,
            settings=self.live_settings,
            verify_precheck=True,
        )

        # 4. Mark execution as dispatching in control DB
        with get_control_connection(self.live_settings) as c_conn:
            with c_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE control.executions
                    SET state = 'dispatching', dispatched_at = NOW()
                    WHERE execution_key = %s
                    """,
                    (claim.execution_key,),
                )
            c_conn.commit()

        # 5. Send real POST to sandbox /repairs (real transaction committed in sandbox)
        repair_url = f"{self.live_settings.il_sandbox_base_url}/repairs"
        headers = {
            "Authorization": f"Bearer {self.live_settings.il_sandbox_write_token}",
            "Content-Type": "application/json",
        }
        req_body = {
            "execution_key": str(claim.execution_key),
            "incident_id": str(inc_id),
            "proposal_id": str(prop_id),
            "payload": claim.payload.model_dump(mode="json"),
            "payload_digest": claim.payload_digest,
            "expected_revision": claim.expected_revision,
        }
        resp = await self.client.post(repair_url, headers=headers, json=req_body)
        assert resp.status_code == 200, f"Sandbox repair failed: {resp.text}"

        # 6. Simulate crash/ack loss BEFORE control DB records the receipt:
        # Control DB remains in state 'dispatching' with receipt = NULL.
        # This accurately models process termination or network partition after sandbox commit.
        return CrashRun(
            incident_id=inc_id,
            execution_key=claim.execution_key,
            live_settings=self.live_settings,
            tracking_client=self.client,
        )


@pytest.fixture
async def crash_harness(live_settings: Settings):
    harness = CrashHarness(live_settings)
    yield harness
    await harness.close()


@pytest.mark.integration
async def test_ack_loss_does_not_replay_mutation(crash_harness: CrashHarness):
    """Verify ack loss crash recovers via read-only GET without replaying mutation POST."""
    run = await crash_harness.apply_then_restart_before_ack()
    await run.reconcile()
    assert run.repair_post_count == 1
    assert run.receipt is not None
    assert run.receipt.result == "applied"


@pytest.mark.integration
async def test_restart_at_awaiting_approval_preserves_proposal(live_settings: Settings):
    """Verify backend restart at awaiting_approval preserves proposal without model rerun."""
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)
    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(conn, inc_id, revision=rev)

    # Simulate restart reconciliation
    results = await reconcile_startup(live_settings)
    assert any(
        r["incident_id"] == inc_id and r["action"] == "retained_awaiting_approval"
        for r in results
    )

    # Readback confirms incident is still awaiting_approval with unchanged proposal
    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id,))
            assert cur.fetchone()[0] == "awaiting_approval"

            cur.execute(
                "SELECT payload_digest, target_revision FROM control.proposals WHERE id = %s",
                (prop_id,),
            )
            p_row = cur.fetchone()
            assert p_row[0] == digest
            assert p_row[1] == rev

    # Stored authenticated decision succeeds and resumes normally
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
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
        assert decision_resp.status_code == 200
        assert decision_resp.json()["decision"] == "approve"


@pytest.mark.integration
async def test_missing_receipt_stays_unknown_and_blocks_new_incidents(live_settings: Settings):
    """Verify missing sandbox receipt marks execution unknown and blocks new incidents."""
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)
    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(
            conn, inc_id, revision=rev, state="executing"
        )
        # Seed an execution key that does NOT exist in sandbox
        unreached_key = uuid4()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.executions (
                    execution_key, proposal_id, incident_id, state,
                    payload_digest, expected_revision, claimed_at, dispatched_at
                ) VALUES (%s, %s, %s, 'dispatching', %s, %s, NOW(), NOW())
                """,
                (unreached_key, prop_id, inc_id, digest, rev),
            )
        conn.commit()

    # Startup reconciliation looks up receipt in sandbox, gets 404, leaves unknown
    results = await reconcile_startup(live_settings)
    assert any(
        r["incident_id"] == inc_id and r["action"] == "execution_unknown"
        for r in results
    )

    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id,))
            assert cur.fetchone()[0] == "execution_unknown"

            cur.execute(
                "SELECT state FROM control.executions WHERE execution_key = %s",
                (unreached_key,),
            )
            assert cur.fetchone()[0] == "unknown"

    # Attempting to start a new incident MUST be blocked by one-active constraint
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        new_inc_resp = await ac.post(
            "/api/v1/incidents",
            headers=headers,
            json={
                "symptom": "Second incident during unknown",
                "request_id": str(uuid4()),
            },
        )
        assert new_inc_resp.status_code == 409
        assert new_inc_resp.json()["error"]["code"] == "active_incident_conflict"

        # Demo reset is also refused while an incident is nonterminal
        reset_resp = await ac.post("/api/v1/demo/reset", headers=headers)
        assert reset_resp.status_code == 409
        assert reset_resp.json()["error"]["code"] == "active_incident_exists"


@pytest.mark.integration
async def test_resolve_unknown_cli_closure_releases_constraint(live_settings: Settings):
    """Verify operator resolve-unknown procedure closes unresolved incident and releases lock."""
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)
    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(
            conn, inc_id, revision=rev, state="execution_unknown"
        )
        unreached_key = uuid4()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.executions (
                    execution_key, proposal_id, incident_id, state,
                    payload_digest, expected_revision, claimed_at, dispatched_at
                ) VALUES (%s, %s, %s, 'unknown', %s, %s, NOW(), NOW())
                """,
                (unreached_key, prop_id, inc_id, digest, rev),
            )
        conn.commit()

    # 1. Validation checks:
    # Acknowledgment mismatch fails
    rc_bad_ack = await resolve_unknown_cli(
        incident_id_str=str(inc_id),
        note="Manual inspection confirmed unknown",
        acknowledge="WRONG ACKNOWLEDGMENT",
        confirm_quiesced=True,
        settings=live_settings,
    )
    assert rc_bad_ack == 1

    # Empty note fails
    rc_empty_note = await resolve_unknown_cli(
        incident_id_str=str(inc_id),
        note="   ",
        acknowledge=f"ACKNOWLEDGE UNKNOWN {unreached_key}",
        confirm_quiesced=True,
        settings=live_settings,
    )
    assert rc_empty_note == 1

    # Unquiesced fails
    rc_unquiesced = await resolve_unknown_cli(
        incident_id_str=str(inc_id),
        note="Inspected",
        acknowledge=f"ACKNOWLEDGE UNKNOWN {unreached_key}",
        confirm_quiesced=False,
        settings=live_settings,
    )
    assert rc_unquiesced == 1

    # 2. Valid operator acknowledgment succeeds
    rc_ok = await resolve_unknown_cli(
        incident_id_str=str(inc_id),
        note="Operator inspected sandbox: no receipt found, outcome truly unknown",
        acknowledge=f"ACKNOWLEDGE UNKNOWN {unreached_key}",
        confirm_quiesced=True,
        settings=live_settings,
    )
    assert rc_ok == 0

    # Inspect control DB: incident is closed_unknown, outcome has effect_unknown=True
    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, outcome FROM control.incidents WHERE id = %s",
                (inc_id,),
            )
            row = cur.fetchone()
            assert row[0] == "closed_unknown"
            outcome = row[1]
            assert outcome.get("effect_unknown") is True
            assert "Operator inspected" in outcome.get("note", "")

            # Execution row remains 'unknown' for audit
            cur.execute(
                "SELECT state FROM control.executions WHERE execution_key = %s",
                (unreached_key,),
            )
            assert cur.fetchone()[0] == "unknown"

            # Event execution.closed_unknown is recorded
            cur.execute(
                """
                SELECT event_type, payload
                FROM control.events
                WHERE incident_id = %s AND event_type = 'execution.closed_unknown'
                """,
                (str(inc_id),),
            )
            ev_row = cur.fetchone()
            assert ev_row is not None
            assert ev_row[1]["effect_unknown"] is True

    # 3. Verify that the one-active-incident constraint is now RELEASED:
    # A new incident can be created without 409 conflict
    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        new_inc_resp = await ac.post(
            "/api/v1/incidents",
            headers=headers,
            json={
                "symptom": "Fresh incident after unknown closure",
                "request_id": str(uuid4()),
            },
        )
        assert new_inc_resp.status_code == 202


@pytest.mark.integration
async def test_resolve_unknown_cli_with_durable_receipt_reconciles_known(live_settings: Settings):
    """Verify resolve-unknown does not close as unknown if a durable receipt exists in sandbox."""
    # Reset sandbox target
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        rev = sandbox_service.inject_fault(s_conn, "inventory_config")

    inc_id = uuid4()
    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(
            conn, inc_id, revision=rev, state="approved"
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.approvals (
                    proposal_id, decision, owner_id, reviewed_digest, reviewed_revision
                ) VALUES (%s, 'approve', 'owner', %s, %s)
                """,
                (prop_id, digest, rev),
            )
        conn.commit()

    claim = await claim_execution(
        incident_id=inc_id,
        proposal_id=prop_id,
        settings=live_settings,
        verify_precheck=True,
    )

    # Execute repair on sandbox
    receipt = await execute_claim(claim, settings=live_settings)
    assert receipt.result == "applied"

    # Manually simulate incident state being set to execution_unknown
    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE control.incidents SET state = 'execution_unknown' WHERE id = %s",
                (inc_id,),
            )
        conn.commit()

    # Run resolve-unknown: should find the durable receipt and reconcile it!
    rc = await resolve_unknown_cli(
        incident_id_str=str(inc_id),
        note="Operator inspecting",
        acknowledge=f"ACKNOWLEDGE UNKNOWN {claim.execution_key}",
        confirm_quiesced=True,
        settings=live_settings,
    )
    assert rc == 0

    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id,))
            # Must be verifying (or terminal), NOT closed_unknown!
            assert cur.fetchone()[0] == "verifying"


@pytest.mark.integration
async def test_cancellation_before_dispatch_prevents_repair(live_settings: Settings):
    """Verify cancellation before dispatch cancels incident without sending repair POST."""
    inc_id = uuid4()
    rev = _get_target_revision(live_settings)
    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(
            conn, inc_id, revision=rev, state="approved"
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.approvals (
                    proposal_id, decision, owner_id, reviewed_digest, reviewed_revision
                ) VALUES (%s, 'approve', 'owner', %s, %s)
                """,
                (prop_id, digest, rev),
            )
        conn.commit()

    claim = await claim_execution(
        incident_id=inc_id,
        proposal_id=prop_id,
        settings=live_settings,
        verify_precheck=True,
    )

    # Request cancellation before dispatch
    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE control.incidents SET cancel_requested = TRUE WHERE id = %s",
                (inc_id,),
            )
        conn.commit()

    tracking_client = TrackingClient(timeout=10.0)
    try:
        with pytest.raises(RuntimeError, match="Execution cancelled before dispatch"):
            await execute_claim(claim, settings=live_settings, client=tracking_client)

        assert tracking_client.repair_post_count == 0
        with get_control_connection(live_settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM control.incidents WHERE id = %s", (inc_id,))
                assert cur.fetchone()[0] == "cancelled"
    finally:
        await tracking_client.aclose()


@pytest.mark.integration
async def test_cancellation_inflight_records_effect_without_verification(live_settings: Settings):
    """Verify in-flight cancellation records applied effect and skips verification."""
    # Reset sandbox target
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        rev = sandbox_service.inject_fault(s_conn, "inventory_config")

    inc_id = uuid4()
    with get_control_connection(live_settings) as conn:
        prop_id, digest = _seed_incident_and_proposal(
            conn, inc_id, revision=rev, state="approved"
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.approvals (
                    proposal_id, decision, owner_id, reviewed_digest, reviewed_revision
                ) VALUES (%s, 'approve', 'owner', %s, %s)
                """,
                (prop_id, digest, rev),
            )
        conn.commit()

    claim = await claim_execution(
        incident_id=inc_id,
        proposal_id=prop_id,
        settings=live_settings,
        verify_precheck=True,
    )

    # Simulate cancellation arriving while HTTP call is in flight:
    class InFlightCancelClient(TrackingClient):
        async def post(self, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
            url_str = str(url)
            if "/repairs" in url_str:
                with get_control_connection(live_settings) as c_conn:
                    with c_conn.cursor() as cur:
                        cur.execute(
                            "UPDATE control.incidents SET cancel_requested = TRUE WHERE id = %s",
                            (inc_id,),
                        )
                    c_conn.commit()
            return await super().post(url, *args, **kwargs)

    tracking_client = InFlightCancelClient(timeout=10.0)
    try:
        receipt = await execute_claim(claim, settings=live_settings, client=tracking_client)
        assert receipt.result == "applied"
        assert tracking_client.repair_post_count == 1

        with get_control_connection(live_settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state, outcome FROM control.incidents WHERE id = %s", (inc_id,))
                row = cur.fetchone()
                assert row[0] == "cancelled"
                outcome = row[1]
                assert outcome.get("cancellation") == "cancelled_in_flight"
                assert outcome.get("effect") == "applied"
    finally:
        await tracking_client.aclose()
