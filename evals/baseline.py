"""Fixed diagnostic baseline workflow for IncidentLab evaluation.

Per PRD IL-03, IL-05, IL-06, IL-12 and ROADMAP IL-T14:
The fixed baseline executes the same 4 diagnostic reads in fixed order:
1. get_health
2. get_logs
3. get_config
4. probe_checkout
followed by keyword runbook retrieval and a single final schema-constrained model call.
It uses identical model, prompts, schemas, token budgets, and authority boundaries.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from incidentlab.config import Settings
from incidentlab.context import build_context
from incidentlab.db import get_control_connection
from incidentlab.events import record_event
from incidentlab.graph import (
    DIAGNOSIS_SCHEMA,
    AgentState,
    create_proposal,
)
from incidentlab.mcp_client import McpSession
from incidentlab.model import ModelResult, call_model
from incidentlab.retrieval import retrieve_runbooks
from incidentlab.schemas import (
    Diagnosis,
    ResumeOrderWorkerPayload,
    SetInventoryEndpointPayload,
)

logger = logging.getLogger("incidentlab.evals.baseline")


async def run_fixed_investigation(
    incident_id: UUID,
    settings: Settings | None = None,
    evidence_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute fixed 4-step diagnostic collection.

    Followed by a single structured schema model call.
    """
    if settings is None:
        settings = Settings.from_env()

    masked_tools = set(evidence_filter.get("masked_tools", [])) if evidence_filter else set()

    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symptom, request_id, order_id, active_ms
                FROM control.incidents
                WHERE id = %s
                """,
                (incident_id,),
            )
            inc_row = cur.fetchone()
            if not inc_row:
                raise ValueError(f"Incident {incident_id} not found")

    symptom = inc_row[0]
    request_id = str(inc_row[1])
    order_id = str(inc_row[2]) if inc_row[2] else None
    active_ms = inc_row[3] or 0
    t0 = time.monotonic()

    state: AgentState = {
        "incident_id": str(incident_id),
        "phase": "investigating",
        "symptom": symptom,
        "request_id": request_id,
        "order_id": order_id,
        "evidence_ids": [],
        "case_notes": [],
        "recent_results": [],
        "retrieved_runbooks": [],
        "read_count": 0,
        "model_turn_count": 0,
        "active_ms": active_ms,
        "proposed_tools": [],
        "diagnosis": None,
        "proposal": None,
        "proposal_id": None,
        "cancel_requested": False,
        "error": None,
    }

    # Execute 4 fixed reads in order via MCP: health -> logs -> config -> probe
    fixed_calls = [
        ("get_health", {}),
        ("get_logs", {"request_id": request_id, "limit": 40}),
        ("get_config", {}),
        ("probe_checkout", {"order_id": order_id} if order_id else {}),
    ]

    log_texts = ""
    async with McpSession(settings) as mcp:
        for tool_name, tool_args in fixed_calls:
            if tool_name in masked_tools:
                logger.warning("Fixed baseline tool %s is masked by evidence filter", tool_name)
                state["recent_results"].append({
                    "tool": tool_name,
                    "error": f"Tool '{tool_name}' is unavailable under current filters.",
                })
                state["recent_results"] = state["recent_results"][-2:]
                state["read_count"] += 1
                continue

            try:
                ev = await mcp.call_read_tool(incident_id, tool_name, tool_args)
                state["evidence_ids"].append(ev.evidence_id)
                state["recent_results"].append(ev.model_dump(mode="json"))
                state["recent_results"] = state["recent_results"][-2:]
                state["read_count"] += 1

                if tool_name == "get_logs":
                    log_texts = " ".join(
                        e.get("message", "") for e in ev.data.get("logs", [])
                    )

                with get_control_connection(settings) as conn:
                    record_event(
                        conn,
                        incident_id,
                        "tool_executed",
                        {"tool": tool_name, "evidence_id": ev.evidence_id},
                    )
            except Exception as exc:
                logger.warning("Fixed tool %s call failed: %s", tool_name, exc)

    # Keyword runbook retrieval
    query = f"{symptom} {log_texts}".strip()
    with get_control_connection(settings) as conn:
        runbook_evs = retrieve_runbooks(conn, query=query, limit=3)

    rb_data = []
    for rbe in runbook_evs:
        state["evidence_ids"].append(rbe.evidence_id)
        rb_data.append({
            "chunk_id": rbe.data.get("chunk_id"),
            "content": rbe.data.get("content"),
            "source": rbe.source,
            "source_version": rbe.source_version,
            "section_id": rbe.section_id,
        })
    state["retrieved_runbooks"] = rb_data

    with get_control_connection(settings) as conn:
        record_event(
            conn,
            incident_id,
            "runbooks_retrieved",
            {"citations": [rb["chunk_id"] for rb in rb_data]},
        )

    # Exactly ONE final schema model call
    messages = build_context(
        state,
        token_budget=settings.il_max_input_tokens,
        model_name=settings.il_model,
    )
    turn_start = time.monotonic()
    model_res: ModelResult = await call_model(
        messages=messages,
        tools=[],
        final_schema=DIAGNOSIS_SCHEMA,
        settings=settings,
    )
    turn_ms = int((time.monotonic() - turn_start) * 1000)
    state["model_turn_count"] = 1

    with get_control_connection(settings) as conn:
        record_event(
            conn,
            incident_id,
            "model_called",
            {"turn": 1, "mode": "schema", "approach": "fixed"},
            duration_ms=turn_ms,
            token_count=(model_res.input_tokens or 0) + (model_res.output_tokens or 0),
        )

    if model_res.diagnosis:
        state["diagnosis"] = model_res.diagnosis

    # Process diagnosis into proposal or escalation
    raw_diag = state.get("diagnosis")
    if not raw_diag:
        state["error"] = "Model failed to produce valid structured diagnosis"
        with get_control_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'failed',
                        updated_at = NOW(),
                        active_ms = active_ms + %s
                    WHERE id = %s
                    """,
                    (int((time.monotonic() - t0) * 1000), incident_id),
                )
            conn.commit()
        return state

    try:
        diag = Diagnosis.model_validate(raw_diag)
    except Exception as exc:
        state["error"] = f"Diagnosis validation error: {exc}"
        with get_control_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'failed',
                        updated_at = NOW(),
                        active_ms = active_ms + %s
                    WHERE id = %s
                    """,
                    (int((time.monotonic() - t0) * 1000), incident_id),
                )
            conn.commit()
        return state

    if diag.decision == "escalate":
        raw_diag["repair"] = None
        state["phase"] = "escalated"
        with get_control_connection(settings) as conn:
            with conn.cursor() as cur:
                from psycopg.types.json import Jsonb

                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'escalated',
                        updated_at = NOW(),
                        outcome = %s,
                        active_ms = active_ms + %s
                    WHERE id = %s
                    """,
                    (Jsonb(raw_diag), int((time.monotonic() - t0) * 1000), incident_id),
                )
            record_event(
                conn,
                incident_id,
                "incident_escalated",
                {
                    "summary": diag.summary,
                    "citations": [c.model_dump() for c in diag.runbook_citations],
                },
            )
            conn.commit()
        return state

    # Decision == "propose_repair"
    if not diag.repair:
        raise ValueError("propose_repair decision must include repair payload")

    action = diag.repair.action
    if action == "set_inventory_endpoint":
        if not diag.repair.endpoint_key:
            raise ValueError("set_inventory_endpoint requires endpoint_key")
        payload = SetInventoryEndpointPayload(endpoint_key=diag.repair.endpoint_key)
    elif action == "resume_order_worker":
        worker_id = diag.repair.worker_id or "checkout-worker"
        payload = ResumeOrderWorkerPayload(worker_id=worker_id)
    else:
        raise ValueError(f"Unsupported repair action: {action}")

    # Fetch latest target revision
    async with McpSession(settings) as mcp:
        h_ev = await mcp.call_read_tool(incident_id, "get_health", {})
        target_revision = h_ev.data.get("revision", 1)

    proposal = create_proposal(
        incident_id=incident_id,
        payload=payload,
        target_revision=target_revision,
        evidence_ids=list(state["evidence_ids"]),
        ttl_seconds=settings.il_proposal_ttl_seconds,
    )

    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            from psycopg.types.json import Jsonb

            cur.execute(
                """
                INSERT INTO control.proposals (
                    id, incident_id, payload, payload_digest, target_revision,
                    created_at, expires_at, evidence_ids
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    proposal.proposal_id,
                    proposal.incident_id,
                    Jsonb(proposal.payload.model_dump()),
                    proposal.payload_digest,
                    proposal.target_revision,
                    proposal.created_at,
                    proposal.expires_at,
                    Jsonb(proposal.evidence_ids),
                ),
            )
            cur.execute(
                """
                UPDATE control.incidents
                SET state = 'awaiting_approval',
                    updated_at = NOW(),
                    active_ms = active_ms + %s
                WHERE id = %s
                """,
                (int((time.monotonic() - t0) * 1000), incident_id),
            )
        record_event(
            conn,
            incident_id,
            "proposal_created",
            {
                "proposal_id": str(proposal.proposal_id),
                "action": proposal.payload.action,
                "payload_digest": proposal.payload_digest,
                "target_revision": proposal.target_revision,
            },
        )
        conn.commit()

    state["phase"] = "awaiting_approval"
    state["proposal_id"] = str(proposal.proposal_id)
    state["proposal"] = proposal.model_dump(mode="json")
    return state
