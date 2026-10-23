"""Bounded evidence-linked context assembly and token accounting."""

from __future__ import annotations

import json
from typing import Any

from incidentlab.model import count_request_tokens

SYSTEM_PROMPT = """You are IncidentLab's autonomous diagnostic and repair agent.
Your objective is to investigate the reported incident symptom using available
read-only MCP tools and retrieved runbook evidence, then produce a strictly
validated diagnosis.

Operational Constraints:
1. Grounding: All diagnostic conclusions must cite observed evidence IDs
   and retrieved runbook sections.
2. Permitted Repairs: Only two finite repair actions are authorized:
   - set_inventory_endpoint: {"action": "set_inventory_endpoint", "endpoint_key": "inventory-local"}
   - resume_order_worker: {"action": "resume_order_worker", "worker_id": "checkout-worker"}
3. Escalation: If the incident is caused by external/upstream maintenance,
   or if observed state does not meet the exact preconditions for an authorized repair,
   you MUST choose decision="escalate" with repair=null.
4. Confidence: If confidence is medium or low,
   you MUST provide an explicit explanation in uncertainty.
5. In schema mode, return ONLY the validated JSON object matching the Diagnosis schema."""


def build_context(
    state: dict[str, Any],
    token_budget: int = 6000,
    model_name: str = "qwen3:8b",
) -> list[dict[str, Any]]:
    """Assemble bounded model messages with evidence links, case notes, and token budget checks."""
    symptom = state.get("symptom", "Unknown symptom")
    request_id = state.get("request_id", "N/A")
    order_id = state.get("order_id", "None")
    known_evidence_ids = set(state.get("evidence_ids", []))

    # Format runbook sections
    runbook_chunks = state.get("retrieved_runbooks", [])
    rb_lines: list[str] = []
    for chunk in runbook_chunks:
        cid = chunk.get("chunk_id", "unknown")
        body = chunk.get("content", "")
        citation = {
            "runbook_id": chunk.get("source"),
            "source_version": chunk.get("source_version"),
            "section_id": chunk.get("section_id"),
        }
        rb_lines.append(
            f"--- Runbook Section [{cid}] ---\n"
            f"Citation fields (copy exactly): {json.dumps(citation)}\n{body}"
        )
    runbooks_text = "\n\n".join(rb_lines) if rb_lines else "None retrieved."

    # Validate and format case notes
    valid_notes: list[str] = []
    for note in state.get("case_notes", []):
        note_ev_ids = note.get("evidence_ids", [])
        # Only preserve notes whose evidence IDs have actually been observed
        valid_evs = [eid for eid in note_ev_ids if eid in known_evidence_ids]
        hypo = note.get("hypothesis", "")
        uncert = note.get("uncertainty", "")
        valid_notes.append(f"- Hypothesis: {hypo} (Evidence: {valid_evs}, Uncertainty: {uncert})")
    case_notes_text = "\n".join(valid_notes) if valid_notes else "None recorded."

    # Format recent results (bound to at most 2 most recent)
    recent = state.get("recent_results", [])[-2:]
    recent_text = json.dumps(recent, indent=2, default=str) if recent else "None."

    user_content = (
        f"Incident Context:\n"
        f"- Symptom: {symptom}\n"
        f"- Request ID: {request_id}\n"
        f"- Order ID: {order_id}\n\n"
        f"Retrieved Runbook Guidance:\n{runbooks_text}\n\n"
        f"Recent Observations:\n{recent_text}\n\n"
        f"Current Case Notes:\n{case_notes_text}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    tokens = count_request_tokens(messages, model_name=model_name)
    if tokens > token_budget:
        # Prune oldest nonessential tool text first
        compact_recent = json.dumps(
            [{"id": r.get("evidence_id"), "kind": r.get("kind")} for r in recent],
            default=str,
        )
        pruned_user_content = (
            f"Incident Context:\n"
            f"- Symptom: {symptom}\n"
            f"- Request ID: {request_id}\n"
            f"- Order ID: {order_id}\n\n"
            f"Retrieved Runbook Guidance:\n{runbooks_text}\n\n"
            f"Recent Observations (compacted):\n{compact_recent}\n\n"
            f"Current Case Notes:\n{case_notes_text}"
        )
        messages[1]["content"] = pruned_user_content
        tokens = count_request_tokens(messages, model_name=model_name)
        if tokens > token_budget:
            raise ValueError(
                f"context_budget_exceeded: Assembled prompt requires {tokens} tokens "
                f"(budget: {token_budget})"
            )

    return messages
