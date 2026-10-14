"""Tests for agent context assembly, LangGraph investigation flow, and proposal immutability."""

from __future__ import annotations

from uuid import uuid4

import pytest

from incidentlab.context import build_context
from incidentlab.graph import compute_payload_digest, create_proposal
from incidentlab.schemas import (
    Diagnosis,
    RunbookCitation,
    SetInventoryEndpointPayload,
)


def test_proposal_canonical_digest_and_immutability():
    """Verify deterministic canonical serialization and SHA-256 payload digest."""
    payload = SetInventoryEndpointPayload(
        action="set_inventory_endpoint",
        endpoint_key="inventory-local",
    )
    digest1 = compute_payload_digest(payload)
    digest2 = compute_payload_digest(
        {"endpoint_key": "inventory-local", "action": "set_inventory_endpoint"}
    )
    assert digest1 == digest2
    assert len(digest1) == 64

    incident_id = uuid4()
    proposal = create_proposal(
        incident_id=incident_id,
        payload=payload,
        target_revision=5,
        evidence_ids=["ev_1", "ev_2"],
    )
    assert proposal.payload_digest == digest1
    assert proposal.target_revision == 5
    assert proposal.incident_id == incident_id


def test_diagnosis_validation():
    """Verify strict validation rules for Diagnosis schemas."""
    citation = RunbookCitation(
        runbook_id="inventory",
        source_version="1",
        section_id="repair",
    )

    # Escalate with a repair payload must be rejected
    with pytest.raises(ValueError, match="Repair payload must be null when decision is escalate"):
        Diagnosis(
            summary="Upstream maintenance active",
            confidence="high",
            evidence_ids=["ev_1"],
            runbook_citations=[citation],
            decision="escalate",
            repair=SetInventoryEndpointPayload(),
            uncertainty="",
        )

    # Propose repair with None repair must be rejected
    match_msg = "Repair payload is required when decision is propose_repair"
    with pytest.raises(ValueError, match=match_msg):
        Diagnosis(
            summary="Misconfigured inventory endpoint",
            confidence="high",
            evidence_ids=["ev_1"],
            runbook_citations=[citation],
            decision="propose_repair",
            repair=None,
            uncertainty="",
        )

    # Non-high confidence without uncertainty explanation must be rejected
    with pytest.raises(ValueError, match="Uncertainty explanation is required"):
        Diagnosis(
            summary="Probable worker issue",
            confidence="medium",
            evidence_ids=["ev_1"],
            runbook_citations=[citation],
            decision="escalate",
            repair=None,
            uncertainty="",
        )


def test_build_context_token_bounds_and_notes():
    """Verify context assembly, prompt formatting, token bounds, and case note validation."""
    state = {
        "incident_id": str(uuid4()),
        "symptom": "Checkout requests return 503 inventory_unavailable",
        "request_id": str(uuid4()),
        "order_id": None,
        "evidence_ids": ["ev_health_1", "ev_logs_1"],
        "case_notes": [
            {
                "hypothesis": "Endpoint misconfiguration",
                "uncertainty": "",
                "evidence_ids": ["ev_health_1"],
            }
        ],
        "recent_results": [
            {"evidence_id": "ev_health_1", "kind": "health", "data": {"status": "ok"}},
            {"evidence_id": "ev_logs_1", "kind": "logs", "data": {"logs": []}},
        ],
        "retrieved_runbooks": [
            {
                "chunk_id": "inventory:1:repair",
                "content": "Authorized repair action is set_inventory_endpoint.",
            }
        ],
    }

    messages = build_context(state, token_budget=6000)
    assert len(messages) >= 2
    assert messages[0]["role"] == "system"
    assert "set_inventory_endpoint" in messages[0]["content"]

    user_msg = messages[1]["content"]
    assert "inventory_unavailable" in user_msg
    assert "inventory:1:repair" in user_msg
    assert "Endpoint misconfiguration" in user_msg

    # Verify context_budget_exceeded when budget is impossibly small
    with pytest.raises(ValueError, match="context_budget_exceeded"):
        build_context(state, token_budget=20)


@pytest.mark.integration
@pytest.mark.live_model
async def test_live_investigation_inventory_fault(live_settings):
    """Verify live model investigates real inventory fault and produces immutable proposal."""
    from incidentlab.db import get_control_connection, get_sandbox_connection
    from incidentlab.executor import submit_checkout
    from incidentlab.graph import run_investigation
    from incidentlab.sandbox import service as sandbox_service

    # 1. Reset and inject fault
    with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
        rev_before = sandbox_service.inject_fault(
            s_conn, family="inventory_config", variant_seed=1
        )

    # 2. Trigger failing checkout
    with pytest.raises(Exception):
        await submit_checkout(request_key=uuid4(), settings=live_settings)

    # 3. Create incident and run investigation
    incident_id = uuid4()
    req_id = uuid4()
    with get_control_connection(live_settings) as conn:
        with conn.cursor() as cur:
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
                VALUES (%s, 'investigating', 'Checkout failed with 503 inventory_unavailable', %s)
                """,
                (incident_id, req_id),
            )
        conn.commit()

    try:
        # Advance graph thread
        result_state = await run_investigation(incident_id, settings=live_settings)

        # Verify essential assertions
        assert result_state["phase"] == "awaiting_approval"
        proposal = result_state.get("proposal")
        assert proposal is not None
        assert proposal["payload"]["action"] == "set_inventory_endpoint"
        assert proposal["payload"]["endpoint_key"] == "inventory-local"
        assert proposal["target_revision"] == rev_before

        # Verify sandbox revision is UNCHANGED (zero repair effect during investigation)
        with get_sandbox_connection(live_settings.il_sandbox_database_url) as s_conn:
            target_state = sandbox_service.get_target_state(s_conn)
            assert target_state["revision"] == rev_before
    finally:
        with get_control_connection(live_settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE control.incidents SET state = 'closed_unknown' WHERE id = %s",
                    (incident_id,),
                )
            conn.commit()
