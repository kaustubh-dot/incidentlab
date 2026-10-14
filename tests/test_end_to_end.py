"""End-to-end integration tests for browser/API recovery and rejection workflows.

Tests execute strictly against http://127.0.0.1:8000 using the owner bearer token.
"""

from __future__ import annotations

import asyncio
import time
from uuid import UUID, uuid4

import httpx
import pytest

from incidentlab.config import Settings


@pytest.mark.live_model
@pytest.mark.integration
@pytest.mark.asyncio
async def test_inventory_recovery_and_rejection():
    """Verify full end-to-end flow: healthy checkout, fault injection, real Ollama

    investigation, proposal generation, owner approval -> functional recovery with
    a newly fulfilled order, followed by fault injection -> owner rejection with zero
    repair executions.
    """
    settings = Settings.from_env()
    base_url = f"http://127.0.0.1:{settings.il_port}"
    owner_headers = {
        "Authorization": f"Bearer {settings.il_owner_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # --- 0. Static Frontend UI Verification ---
        ui_resp = await client.get("/")
        assert ui_resp.status_code == 200
        assert "<div id=\"root\">" in ui_resp.text
        assert "IncidentLab" in ui_resp.text

        # =========================================================================
        # PART 1: APPROVED RECOVERY FLOW
        # =========================================================================

        # 1. Reset sandbox to healthy baseline
        reset_resp = await client.post("/api/v1/demo/reset", headers=owner_headers)
        if reset_resp.status_code == 409:
            err_msg = reset_resp.json().get("error", {}).get("message", "")
            for word in err_msg.split():
                try:
                    active_id = str(UUID(word))
                    await client.post(
                        f"/api/v1/incidents/{active_id}/cancel",
                        headers=owner_headers,
                    )
                    break
                except ValueError:
                    continue
            reset_resp = await client.post("/api/v1/demo/reset", headers=owner_headers)

        assert reset_resp.status_code == 200
        reset_data = reset_resp.json()
        assert reset_data["status"] == "reset"
        initial_revision = reset_data["revision"]

        # 2. Healthy demo checkout fulfills
        chk_resp = await client.post(
            "/api/v1/demo/checkout",
            headers=owner_headers,
            json={"request_key": str(uuid4())},
        )
        assert chk_resp.status_code == 200
        chk_data = chk_resp.json()
        assert chk_data["status"] == "fulfilled"
        assert chk_data["order_id"] is not None

        # 3. Inject inventory fault
        fault_resp = await client.post(
            "/api/v1/demo/fault",
            headers=owner_headers,
            json={"family": "inventory_config", "variant_seed": 1},
        )
        assert fault_resp.status_code == 200
        fault_data = fault_resp.json()
        assert fault_data["status"] == "injected"
        fault_revision = fault_data["revision"]
        assert fault_revision > initial_revision

        # 4. Observe real checkout failure
        fail_chk_resp = await client.post(
            "/api/v1/demo/checkout",
            headers=owner_headers,
            json={"request_key": str(uuid4())},
        )
        assert fail_chk_resp.status_code == 200
        fail_chk_data = fail_chk_resp.json()
        assert fail_chk_data["status"] == "failed"
        failing_req_id = fail_chk_data["request_id"]
        assert failing_req_id is not None

        # 5. Start incident investigation
        start_resp = await client.post(
            "/api/v1/incidents",
            headers=owner_headers,
            json={
                "symptom": f"Checkout failed: {fail_chk_data.get('error', '503 error')}",
                "request_id": failing_req_id,
            },
        )
        assert start_resp.status_code == 202
        inc_data = start_resp.json()
        incident_id = inc_data["id"]
        assert inc_data["state"] in ("investigating", "awaiting_approval")

        # 6. Poll until awaiting_approval (allow up to 600s for local Ollama CPU inference)
        deadline = time.monotonic() + 600.0
        while time.monotonic() < deadline:
            get_resp = await client.get(
                f"/api/v1/incidents/{incident_id}", headers=owner_headers
            )
            assert get_resp.status_code == 200
            inc_data = get_resp.json()
            if inc_data["state"] == "awaiting_approval":
                break
            if inc_data["state"] in ("failed", "verification_failed"):
                err_outcome = inc_data.get("outcome")
                raise AssertionError(f"Investigation failed prematurely: {err_outcome}")
            await asyncio.sleep(2.0)

        assert inc_data["state"] == "awaiting_approval"
        proposal = inc_data["proposal"]
        assert proposal is not None
        assert proposal["payload"]["action"] == "set_inventory_endpoint"
        assert proposal["payload"]["endpoint_key"] == "inventory-local"
        assert proposal["target_revision"] == fault_revision
        prop_id = proposal["proposal_id"]
        prop_digest = proposal["payload_digest"]
        prop_rev = proposal["target_revision"]

        # Verify events and evidence retrieval
        events_resp = await client.get(
            f"/api/v1/incidents/{incident_id}/events?after_seq=0&limit=50",
            headers=owner_headers,
        )
        assert events_resp.status_code == 200
        events_data = events_resp.json()
        assert len(events_data["events"]) > 0

        if proposal.get("evidence_ids"):
            first_ev_id = proposal["evidence_ids"][0]
            ev_resp = await client.get(
                f"/api/v1/incidents/{incident_id}/evidence/{first_ev_id}",
                headers=owner_headers,
            )
            assert ev_resp.status_code == 200
            assert ev_resp.json()["evidence_id"] == first_ev_id

        # 7. Submit owner approval decision
        decide_resp = await client.post(
            f"/api/v1/incidents/{incident_id}/decision",
            headers=owner_headers,
            json={
                "proposal_id": prop_id,
                "payload_digest": prop_digest,
                "target_revision": prop_rev,
                "decision": "approve",
            },
        )
        assert decide_resp.status_code == 200
        assert decide_resp.json()["decision"] == "approve"

        # 8. Poll until terminal state verified_recovered
        # (allow up to 60s for execution & verification)
        recov_deadline = time.monotonic() + 60.0
        while time.monotonic() < recov_deadline:
            get_resp = await client.get(
                f"/api/v1/incidents/{incident_id}", headers=owner_headers
            )
            assert get_resp.status_code == 200
            inc_data = get_resp.json()
            if inc_data["state"] in ("verified_recovered", "verification_failed"):
                break
            await asyncio.sleep(1.0)

        assert inc_data["state"] == "verified_recovered"

        # 9. Verify execution receipt and independent verification order
        reconcile_resp = await client.post(
            f"/api/v1/incidents/{incident_id}/reconcile",
            headers=owner_headers,
        )
        assert reconcile_resp.status_code == 200
        exec_view = reconcile_resp.json()
        assert exec_view["state"] == "applied"
        assert exec_view["receipt"] is not None
        assert exec_view["receipt"]["result"] == "applied"
        assert exec_view["receipt"]["before_revision"] == fault_revision
        assert exec_view["receipt"]["after_revision"] > fault_revision

        # Check outcome contains verification proof
        outcome = inc_data.get("outcome")
        assert outcome is not None
        verif_data = outcome.get("verification", {})
        assert verif_data.get("verdict") == "pass"
        verification_order_id = verif_data.get("order_id")
        assert verification_order_id is not None
        # Must be a brand new order, different from initial checkout order
        assert verification_order_id != str(chk_data["order_id"])

        # =========================================================================
        # PART 2: OWNER REJECTION FLOW
        # =========================================================================

        # 1. Inject fault again
        fault_resp_2 = await client.post(
            "/api/v1/demo/fault",
            headers=owner_headers,
            json={"family": "inventory_config", "variant_seed": 2},
        )
        assert fault_resp_2.status_code == 200

        # 2. Observe failing checkout
        fail_chk_2 = await client.post(
            "/api/v1/demo/checkout",
            headers=owner_headers,
            json={"request_key": str(uuid4())},
        )
        assert fail_chk_2.status_code == 200
        assert fail_chk_2.json()["status"] == "failed"

        # 3. Start second incident
        start_resp_2 = await client.post(
            "/api/v1/incidents",
            headers=owner_headers,
            json={
                "symptom": "Checkout failed on legacy inventory endpoint",
                "request_id": fail_chk_2.json()["request_id"],
            },
        )
        assert start_resp_2.status_code == 202
        inc_id_2 = start_resp_2.json()["id"]

        # 4. Poll until awaiting_approval (allow up to 600s for local Ollama CPU inference)
        deadline_2 = time.monotonic() + 600.0
        while time.monotonic() < deadline_2:
            get_resp_2 = await client.get(
                f"/api/v1/incidents/{inc_id_2}", headers=owner_headers
            )
            assert get_resp_2.status_code == 200
            inc_data_2 = get_resp_2.json()
            if inc_data_2["state"] == "awaiting_approval":
                break
            if inc_data_2["state"] in ("failed", "verification_failed"):
                err_outcome = inc_data_2.get("outcome")
                raise AssertionError(f"Investigation failed prematurely: {err_outcome}")
            await asyncio.sleep(2.0)

        assert inc_data_2["state"] == "awaiting_approval"
        proposal_2 = inc_data_2["proposal"]
        assert proposal_2 is not None

        # 5. Submit owner REJECTION decision
        decide_resp_2 = await client.post(
            f"/api/v1/incidents/{inc_id_2}/decision",
            headers=owner_headers,
            json={
                "proposal_id": proposal_2["proposal_id"],
                "payload_digest": proposal_2["payload_digest"],
                "target_revision": proposal_2["target_revision"],
                "decision": "reject",
            },
        )
        assert decide_resp_2.status_code == 200
        assert decide_resp_2.json()["decision"] == "reject"

        # 6. Read incident state: must be rejected
        final_inc_resp_2 = await client.get(
            f"/api/v1/incidents/{inc_id_2}", headers=owner_headers
        )
        assert final_inc_resp_2.status_code == 200
        assert final_inc_resp_2.json()["state"] == "rejected"

        # 7. Reconcile endpoint confirms zero execution claims created
        reconcile_resp_2 = await client.post(
            f"/api/v1/incidents/{inc_id_2}/reconcile",
            headers=owner_headers,
        )
        assert reconcile_resp_2.status_code == 404
        assert reconcile_resp_2.json()["error"]["code"] == "execution_not_found"
