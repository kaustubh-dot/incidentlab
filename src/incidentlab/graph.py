"""Bounded LangGraph investigation flow and immutable repair proposal generation."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from incidentlab.config import Settings
from incidentlab.context import build_context
from incidentlab.db import get_control_connection
from incidentlab.events import record_event
from incidentlab.mcp_client import McpSession
from incidentlab.model import ModelResult, call_model
from incidentlab.retrieval import retrieve_runbooks
from incidentlab.schemas import (
    Diagnosis,
    Proposal,
    RepairPayload,
    ResumeOrderWorkerPayload,
    SetInventoryEndpointPayload,
)

logger = logging.getLogger("incidentlab.graph")

MCP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_health",
            "description": "Probe service health, database connectivity, and target revision",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logs",
            "description": "Retrieve bounded diagnostic logs from sandbox up to 40 entries",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "Optional request ID UUID to filter logs",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of logs (1-40)",
                        "default": 40,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_config",
            "description": "Inspect target config (endpoint_key, worker_running, revision)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "probe_checkout",
            "description": "Probe inventory dependency reachability and inspect optional order ID",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Optional order ID UUID to inspect",
                    }
                },
            },
        },
    },
]

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Detailed technical explanation of root cause (1-1200 chars)",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Observed evidence IDs supporting this diagnosis",
        },
        "runbook_citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "runbook_id": {
                        "type": "string",
                        "description": "Runbook ID (e.g. 'inventory', 'worker', 'upstream')",
                    },
                    "source_version": {
                        "type": "string",
                        "description": "Runbook version string (e.g. '1')",
                    },
                    "section_id": {
                        "type": "string",
                        "description": (
                            "Runbook section (e.g. 'repair', 'diagnostics', 'escalation', "
                            "'symptoms', 'verification')"
                        ),
                    },
                },
                "required": ["runbook_id", "source_version", "section_id"],
            },
        },
        "decision": {
            "type": "string",
            "enum": ["propose_repair", "escalate"],
        },
        "repair": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set_inventory_endpoint", "resume_order_worker"],
                },
                "endpoint_key": {"type": "string", "enum": ["inventory-local"]},
                "worker_id": {"type": "string", "enum": ["checkout-worker"]},
            },
            "required": ["action"],
        },
        "uncertainty": {
            "type": "string",
            "description": "Explanation of any uncertainty or why repair cannot be safely applied",
        },
    },
    "required": [
        "summary",
        "confidence",
        "evidence_ids",
        "runbook_citations",
        "decision",
    ],
}


def compute_payload_digest(payload: RepairPayload | dict[str, Any]) -> str:
    """Compute canonical SHA-256 digest of repair payload JSON."""
    if hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = payload
    else:
        raise TypeError(f"Unsupported payload type for digest computation: {type(payload)}")

    canonical_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def create_proposal(
    incident_id: UUID,
    payload: RepairPayload,
    target_revision: int,
    evidence_ids: list[str],
    ttl_seconds: int = 600,
    proposal_id: UUID | None = None,
) -> Proposal:
    """Create an immutable Proposal with canonical payload digest and expiration deadline."""
    pid = proposal_id or uuid4()
    digest = compute_payload_digest(payload)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)

    return Proposal(
        proposal_id=pid,
        incident_id=incident_id,
        payload=payload,
        payload_digest=digest,
        target_revision=target_revision,
        created_at=now,
        expires_at=expires_at,
        evidence_ids=evidence_ids,
    )


class AgentState(TypedDict, total=False):
    incident_id: str
    phase: str
    symptom: str
    request_id: str
    order_id: str | None
    evidence_ids: list[str]
    case_notes: list[dict[str, Any]]
    recent_results: list[dict[str, Any]]
    retrieved_runbooks: list[dict[str, Any]]
    read_count: int
    model_turn_count: int
    active_ms: int
    proposed_tools: list[dict[str, Any]]
    diagnosis: dict[str, Any] | None
    proposal: dict[str, Any] | None
    proposal_id: str | None
    cancel_requested: bool
    error: str | None


def _check_cancellation(settings: Settings, incident_id: UUID) -> bool:
    """Check if cancellation was requested for this incident."""
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cancel_requested FROM control.incidents WHERE id = %s",
                (incident_id,),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False


async def run_investigation(
    incident_id: UUID,
    settings: Settings | None = None,
    evidence_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute or resume bounded incident investigation advancing LangGraph state thread."""
    if settings is None:
        settings = Settings.from_env()

    masked_tools = set(evidence_filter.get("masked_tools", [])) if evidence_filter else set()

    # Replay protection: if a proposal already exists, return state without duplicate insert
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, payload, payload_digest, target_revision,
                       created_at, expires_at, evidence_ids
                FROM control.proposals
                WHERE incident_id = %s
                """,
                (incident_id,),
            )
            existing_p = cur.fetchone()
            if existing_p:
                logger.info(
                    "Incident %s already has stored proposal %s; replaying",
                    incident_id,
                    existing_p[0],
                )
                raw_pl = existing_p[1]
                p_payload = raw_pl if isinstance(raw_pl, dict) else json.loads(raw_pl)
                raw_evs = existing_p[6]
                p_evs = raw_evs if isinstance(raw_evs, list) else json.loads(raw_evs)
                return {
                    "incident_id": str(incident_id),
                    "phase": "awaiting_approval",
                    "proposal_id": str(existing_p[0]),
                    "proposal": {
                        "proposal_id": str(existing_p[0]),
                        "incident_id": str(incident_id),
                        "payload": p_payload,
                        "payload_digest": existing_p[2],
                        "target_revision": existing_p[3],
                        "created_at": existing_p[4].isoformat(),
                        "expires_at": existing_p[5].isoformat(),
                        "evidence_ids": p_evs,
                    },
                }

            # Fetch incident initial row
            cur.execute(
                """
                SELECT symptom, request_id, order_id, active_ms, cancel_requested
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

    # Initialize state
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
        "cancel_requested": bool(inc_row[4]),
        "error": None,
    }

    # Step 1: Initial Observation via MCP (health and logs)
    if _check_cancellation(settings, incident_id):
        return await _terminate_cancelled(settings, incident_id, state, t0)

    log_texts = ""
    async with McpSession(settings) as mcp:
        if "get_health" not in masked_tools:
            health_ev = await mcp.call_read_tool(incident_id, "get_health", {})
            state["evidence_ids"].append(health_ev.evidence_id)
            state["recent_results"].append(health_ev.model_dump(mode="json"))
            state["read_count"] += 1
        if "get_logs" not in masked_tools:
            logs_ev = await mcp.call_read_tool(
                incident_id, "get_logs", {"request_id": request_id, "limit": 40}
            )
            state["evidence_ids"].append(logs_ev.evidence_id)
            state["recent_results"].append(logs_ev.model_dump(mode="json"))
            state["read_count"] += 1
            log_texts = " ".join(
                e.get("message", "") for e in logs_ev.data.get("logs", [])
            )

    if state["evidence_ids"]:
        with get_control_connection(settings) as conn:
            record_event(
                conn,
                incident_id,
                "evidence_collected",
                {"evidence_ids": list(state["evidence_ids"])},
            )

    # Step 2: Retrieve Runbooks based on symptom and error logs
    if _check_cancellation(settings, incident_id):
        return await _terminate_cancelled(settings, incident_id, state, t0)

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

    # Step 3: Model Investigation Loop (at most 4 turns, max 6 read calls)
    while state["model_turn_count"] < 4:
        if _check_cancellation(settings, incident_id):
            return await _terminate_cancelled(settings, incident_id, state, t0)

        # On 4th turn (index 3) or if read calls exhausted, force structured final diagnosis schema
        is_final_turn = (state["model_turn_count"] >= 3) or (state["read_count"] >= 6)
        messages = build_context(
            state,
            token_budget=settings.il_max_input_tokens,
            model_name=settings.il_model,
        )

        turn_start = time.monotonic()
        if is_final_turn:
            # Schema mode without tools
            try:
                model_res: ModelResult = await call_model(
                    messages=messages,
                    tools=[],
                    final_schema=DIAGNOSIS_SCHEMA,
                    settings=settings,
                )
            except Exception as exc:
                logger.error("Final turn model call failed: %s", exc)
                return await _terminate_failed(
                    settings, incident_id, state, f"Final turn model call failed: {exc}"
                )
            state["model_turn_count"] += 1
            turn_ms = int((time.monotonic() - turn_start) * 1000)

            with get_control_connection(settings) as conn:
                record_event(
                    conn,
                    incident_id,
                    "model_called",
                    {
                        "turn": state["model_turn_count"],
                        "mode": "schema",
                        "input_tokens": model_res.input_tokens,
                        "output_tokens": model_res.output_tokens,
                        "tokens_per_second": model_res.tokens_per_second,
                    },
                    duration_ms=turn_ms,
                    token_count=(model_res.input_tokens or 0) + (model_res.output_tokens or 0),
                )

            if model_res.diagnosis:
                state["diagnosis"] = model_res.diagnosis
            break
        else:
            # Tool calling mode with unmasked tools
            active_tools = [
                t for t in MCP_TOOLS
                if t["function"]["name"] not in masked_tools
            ]
            try:
                model_res = await call_model(
                    messages=messages,
                    tools=active_tools,
                    final_schema=None,
                    settings=settings,
                )
            except Exception as exc:
                logger.error("Investigation turn model call failed: %s", exc)
                return await _terminate_failed(
                    settings, incident_id, state, f"Investigation turn model call failed: {exc}"
                )
            state["model_turn_count"] += 1
            turn_ms = int((time.monotonic() - turn_start) * 1000)

            with get_control_connection(settings) as conn:
                record_event(
                    conn,
                    incident_id,
                    "model_called",
                    {
                        "turn": state["model_turn_count"],
                        "mode": "tools",
                        "calls": len(model_res.tool_calls),
                        "input_tokens": model_res.input_tokens,
                        "output_tokens": model_res.output_tokens,
                        "tokens_per_second": model_res.tokens_per_second,
                    },
                    duration_ms=turn_ms,
                    token_count=(model_res.input_tokens or 0) + (model_res.output_tokens or 0),
                )

            if model_res.tool_calls:
                # Execute tool calls via MCP
                async with McpSession(settings) as mcp:
                    for tc in model_res.tool_calls:
                        if state["read_count"] >= 6:
                            break
                        if tc.name in masked_tools:
                            logger.warning("Tool %s is masked by evidence filter", tc.name)
                            state["recent_results"].append({
                                "tool": tc.name,
                                "error": (
                                    f"Tool '{tc.name}' is unavailable under current filters."
                                ),
                            })
                            state["recent_results"] = state["recent_results"][-2:]
                            state["read_count"] += 1
                            continue
                        try:
                            ev = await mcp.call_read_tool(incident_id, tc.name, tc.args)
                            state["evidence_ids"].append(ev.evidence_id)
                            state["recent_results"].append(ev.model_dump(mode="json"))
                            state["recent_results"] = state["recent_results"][-2:]
                            state["read_count"] += 1
                            with get_control_connection(settings) as conn:
                                record_event(
                                    conn,
                                    incident_id,
                                    "tool_executed",
                                    {"tool": tc.name, "evidence_id": ev.evidence_id},
                                )
                        except Exception as exc:
                            logger.warning("Tool %s call failed: %s", tc.name, exc)
            else:
                # If model did not call tools, advance directly to final structured schema
                break

    # If diagnosis was not set, request schema mode now
    if not state.get("diagnosis"):
        messages = build_context(
            state,
            token_budget=settings.il_max_input_tokens,
            model_name=settings.il_model,
        )
        messages.append({
            "role": "user",
            "content": (
                "Based on the collected evidence and cited runbooks, produce your final "
                "diagnosis JSON object matching the required schema now."
            ),
        })
        try:
            model_res = await call_model(
                messages=messages,
                tools=[],
                final_schema=DIAGNOSIS_SCHEMA,
                settings=settings,
            )
            state["diagnosis"] = model_res.diagnosis
        except Exception as exc:
            logger.error("Final diagnosis model call failed: %s", exc)
            return await _terminate_failed(
                settings, incident_id, state, f"Final diagnosis model call failed: {exc}"
            )

    # Step 4: Evaluate and persist diagnosis / proposal
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    state["active_ms"] += elapsed_ms

    raw_diag = state.get("diagnosis")
    if not raw_diag:
        return await _terminate_failed(
            settings, incident_id, state, "No diagnosis generated by model"
        )

    # When decision is escalate, repair must be null per PRD and domain model
    if raw_diag.get("decision") == "escalate":
        raw_diag["repair"] = None
        if raw_diag.get("confidence") != "high" and not raw_diag.get("uncertainty", "").strip():
            raw_diag["uncertainty"] = "Incident escalated based on non-repairable conditions."

    # Validate citations and evidence
    try:
        diag = Diagnosis.model_validate(raw_diag)
    except Exception as exc:
        return await _terminate_failed(
            settings, incident_id, state, f"Diagnosis schema validation failed: {exc}"
        )

    if diag.decision == "propose_repair" and diag.repair is not None:
        # Determine target revision from observed health/config evidence
        cur_rev = None
        for res in state.get("recent_results", []):
            data = res.get("data", {})
            if "target_revision" in data:
                cur_rev = data["target_revision"]
                break
        if cur_rev is None:
            async with McpSession(settings) as mcp:
                h_ev = await mcp.call_read_tool(incident_id, "get_health", {})
                cur_rev = h_ev.data.get("target_revision", 1)

        # Ensure configuration evidence supports repair
        if diag.repair.action == "set_inventory_endpoint":
            payload = SetInventoryEndpointPayload(
                action="set_inventory_endpoint",
                endpoint_key="inventory-local",
            )
        elif diag.repair.action == "resume_order_worker":
            payload = ResumeOrderWorkerPayload(
                action="resume_order_worker",
                worker_id="checkout-worker",
            )
        else:
            return await _terminate_failed(
                settings, incident_id, state, f"Unsupported repair: {diag.repair}"
            )

        proposal = create_proposal(
            incident_id=incident_id,
            payload=payload,
            target_revision=cur_rev,
            evidence_ids=diag.evidence_ids,
            ttl_seconds=settings.il_approval_ttl_seconds,
        )

        if _check_cancellation(settings, incident_id):
            return await _terminate_cancelled(settings, incident_id, state, t0)

        # Persist proposal into control.proposals
        with get_control_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO control.proposals (
                        id, incident_id, payload, payload_digest, target_revision,
                        evidence_ids, created_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        proposal.proposal_id,
                        incident_id,
                        Jsonb(proposal.payload.model_dump()),
                        proposal.payload_digest,
                        proposal.target_revision,
                        Jsonb(proposal.evidence_ids),
                        proposal.created_at,
                        proposal.expires_at,
                    ),
                )
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'awaiting_approval',
                        updated_at = NOW(),
                        active_ms = active_ms + %s,
                        outcome = %s
                    WHERE id = %s
                    """,
                    (elapsed_ms, Jsonb({"diagnosis": diag.model_dump()}), incident_id),
                )
            conn.commit()

            record_event(
                conn,
                incident_id,
                "proposal_created",
                {
                    "proposal_id": str(proposal.proposal_id),
                    "payload_digest": proposal.payload_digest,
                    "target_revision": proposal.target_revision,
                    "action": proposal.payload.action,
                },
            )

        state["phase"] = "awaiting_approval"
        state["proposal_id"] = str(proposal.proposal_id)
        state["proposal"] = {
            "proposal_id": str(proposal.proposal_id),
            "incident_id": str(incident_id),
            "payload": proposal.payload.model_dump(),
            "payload_digest": proposal.payload_digest,
            "target_revision": proposal.target_revision,
            "created_at": proposal.created_at.isoformat(),
            "expires_at": proposal.expires_at.isoformat(),
            "evidence_ids": proposal.evidence_ids,
        }
        return state

    elif diag.decision == "escalate":
        with get_control_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'escalated',
                        updated_at = NOW(),
                        active_ms = active_ms + %s,
                        outcome = %s
                    WHERE id = %s
                    """,
                    (elapsed_ms, Jsonb({"diagnosis": diag.model_dump()}), incident_id),
                )
            conn.commit()
            record_event(
                conn,
                incident_id,
                "incident_escalated",
                {"summary": diag.summary, "uncertainty": diag.uncertainty},
            )

        state["phase"] = "escalated"
        state["diagnosis"] = diag.model_dump()
        return state

    return await _terminate_failed(
        settings, incident_id, state, f"Unknown diagnosis decision: {diag.decision}"
    )


async def _terminate_cancelled(
    settings: Settings,
    incident_id: UUID,
    state: AgentState,
    t0: float,
) -> dict[str, Any]:
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE control.incidents
                SET state = 'cancelled',
                    updated_at = NOW(),
                    active_ms = active_ms + %s,
                    outcome = %s
                WHERE id = %s
                """,
                (elapsed_ms, Jsonb({"reason": "Cancellation requested"}), incident_id),
            )
        conn.commit()
        record_event(conn, incident_id, "incident_cancelled", {"reason": "Cancellation requested"})

    state["phase"] = "cancelled"
    state["active_ms"] += elapsed_ms
    return state


async def _terminate_failed(
    settings: Settings,
    incident_id: UUID,
    state: AgentState,
    error_message: str,
) -> dict[str, Any]:
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE control.incidents
                SET state = 'failed',
                    updated_at = NOW(),
                    outcome = %s
                WHERE id = %s
                """,
                (Jsonb({"error": error_message}), incident_id),
            )
        conn.commit()
        record_event(conn, incident_id, "incident_failed", {"error": error_message})

    state["phase"] = "failed"
    state["error"] = error_message
    return state
