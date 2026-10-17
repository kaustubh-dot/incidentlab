"""IncidentLab isolated evaluation runner.

Implements run_case(case, approach) per EVALUATION.md and ROADMAP.md IL-T13.
Drives reproducible, isolated evaluation runs across development and held-out cases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from evals.baseline import run_fixed_investigation
from evals.score import CaseArtifact
from incidentlab.approvals import ApprovalError, claim_execution, decide_proposal
from incidentlab.config import Settings
from incidentlab.db import get_control_connection, get_sandbox_connection
from incidentlab.executor import (
    CheckoutError,
    execute_claim,
    submit_checkout,
    wait_for_order_fulfillment,
)
from incidentlab.graph import run_investigation
from incidentlab.sandbox.service import record_log
from incidentlab.schemas import ApprovalDecision
from incidentlab.verifier import verify_checkout

logger = logging.getLogger("incidentlab.evals.run")


@dataclass
class CaseRecipe:
    case_id: str
    split: str
    family: str
    variation: str
    seed: int
    initial_conditions: dict[str, Any]
    traffic_recipe: dict[str, int]
    evidence_filter: dict[str, Any] | None
    state_change: dict[str, Any] | None
    symptom_template: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaseRecipe:
        return cls(
            case_id=data["case_id"],
            split=data.get("split", "dev"),
            family=data["family"],
            variation=data.get("variation", "clean"),
            seed=data.get("seed", 1),
            initial_conditions=data.get("initial_conditions", {}),
            traffic_recipe=data.get("traffic_recipe", {}),
            evidence_filter=data.get("evidence_filter"),
            state_change=data.get("state_change"),
            symptom_template=data.get("symptom_template", "Checkout failed"),
        )


def get_git_commit() -> str:
    """Retrieve current git HEAD commit SHA."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


async def run_case(
    case: CaseRecipe,
    approach: str = "agent",
    settings: Settings | None = None,
) -> CaseArtifact:
    """Execute a single evaluation case recipe through the complete IncidentLab harness."""
    if settings is None:
        settings = Settings.from_env()

    run_id = f"eval-{uuid4().hex[:8]}"
    commit_sha = get_git_commit()
    t_start = time.monotonic()
    admin_headers = {"Authorization": f"Bearer {settings.il_sandbox_admin_token}"}

    # 1. Reset sandbox target state to healthy baseline
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r_reset = await ac.post(
            f"{settings.il_sandbox_base_url}/operator/reset",
            headers=admin_headers,
        )
        if r_reset.status_code != 200:
            raise RuntimeError(f"Sandbox reset failed: {r_reset.text}")

    # Clean active incidents in control DB so ix_one_active_incident is clean
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

    # 2. Run pre-fault checkouts per traffic recipe
    pre_fault_count = case.traffic_recipe.get("pre_fault_checkouts", 0)
    for _ in range(pre_fault_count):
        chk = await submit_checkout(uuid4(), settings=settings)
        await wait_for_order_fulfillment(chk.order_id, settings=settings, timeout_seconds=5.0)

    # 3. Inject fault family via sandbox operator endpoint
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r_inj = await ac.post(
            f"{settings.il_sandbox_base_url}/operator/inject",
            headers=admin_headers,
            json={"family": case.family, "variant_seed": case.seed},
        )
        if r_inj.status_code != 200:
            raise RuntimeError(f"Fault injection failed: {r_inj.text}")
        inj_data = r_inj.json()

    # Reconcile case.initial_conditions with sandbox.target_state if specified
    if case.initial_conditions:
        with get_sandbox_connection(settings.il_sandbox_database_url) as s_conn:
            with s_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sandbox.target_state
                    SET endpoint_key = COALESCE(%s, endpoint_key),
                        worker_running = COALESCE(%s, worker_running),
                        maintenance = COALESCE(%s, maintenance)
                    WHERE id = 1
                    """,
                    (
                        case.initial_conditions.get("endpoint_key"),
                        case.initial_conditions.get("worker_running"),
                        case.initial_conditions.get("maintenance"),
                    ),
                )
            s_conn.commit()

    # 4. Generate older misleading logs if requested by case variation
    if case.variation == "older_misleading_logs":
        older_ts = datetime.now(UTC) - timedelta(minutes=15)
        with get_sandbox_connection(settings.il_sandbox_database_url) as s_conn:
            record_log(
                s_conn,
                event_kind="dependency_alert",
                severity="WARN",
                message="Transient redis cache timeout detected; fallback activated",
                data={"component": "redis-cache", "latency_ms": 1250, "status": "recovering"},
                timestamp=older_ts,
            )
            record_log(
                s_conn,
                event_kind="dependency_resolved",
                severity="INFO",
                message="Redis cache reconnection successful; p99 latency normalized to 12ms",
                data={"component": "redis-cache", "latency_ms": 12, "status": "healthy"},
                timestamp=older_ts + timedelta(seconds=30),
            )
            s_conn.commit()

    # Generate background noise logs if requested
    noise_count = case.traffic_recipe.get("noise_logs", 0)
    if noise_count > 0:
        with get_sandbox_connection(settings.il_sandbox_database_url) as s_conn:
            for idx in range(noise_count):
                record_log(
                    s_conn,
                    event_kind="background_noise",
                    severity="INFO",
                    message=f"Routine system telemetry ping {idx + 1}/{noise_count}",
                    data={"noise_index": idx, "component": "metrics-agent"},
                )
            s_conn.commit()

    # 5. Trigger post-fault traffic to observe real failure and capture IDs
    failed_req_id = uuid4()
    failed_order_id = None
    symptom_text = case.symptom_template

    post_fault_count = max(1, case.traffic_recipe.get("post_fault_checkouts", 1))
    for p_idx in range(post_fault_count):
        if case.family == "worker_paused":
            try:
                acc = await submit_checkout(uuid4(), settings=settings)
                if p_idx == 0:
                    failed_req_id = acc.request_id
                    failed_order_id = acc.order_id
                try:
                    await wait_for_order_fulfillment(
                        acc.order_id, settings=settings, timeout_seconds=1.5
                    )
                except Exception:
                    pass
            except Exception:
                pass
        else:
            try:
                await submit_checkout(uuid4(), settings=settings)
            except CheckoutError as chk_err:
                if p_idx == 0 and chk_err.request_id:
                    try:
                        failed_req_id = UUID(chk_err.request_id)
                    except Exception:
                        pass
            except Exception:
                pass

    # Capture target state after fault and initial condition setup, before investigation
    target_state_before = {}
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r_conf = await ac.get(
            f"{settings.il_sandbox_base_url}/diagnostics/config",
            headers={"Authorization": f"Bearer {settings.il_sandbox_read_token}"},
        )
        if r_conf.status_code == 200:
            target_state_before = r_conf.json()

    # 6. Create incident in control DB
    incident_id = uuid4()
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.incidents (id, state, symptom, request_id, order_id)
                VALUES (%s, 'investigating', %s, %s, %s)
                """,
                (incident_id, symptom_text, failed_req_id, failed_order_id),
            )
        conn.commit()

    # 7. Run investigation
    exec_error = None
    state: dict[str, Any] = {}
    try:
        if approach == "agent":
            state = await run_investigation(
                incident_id,
                settings=settings,
                evidence_filter=case.evidence_filter,
            )
        elif approach == "fixed":
            state = await run_fixed_investigation(
                incident_id,
                settings=settings,
                evidence_filter=case.evidence_filter,
            )
        else:
            raise ValueError(f"Unknown approach: {approach}")
    except Exception as exc:
        logger.error("Investigation failed for case %s: %s", case.case_id, exc)
        exec_error = str(exc)
        state = {
            "error": str(exc),
            "proposal": None,
            "diagnosis": None,
            "recent_results": [],
            "retrieved_runbooks": [],
        }
        with get_control_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'failed', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (incident_id,),
                )
            conn.commit()

    if state.get("error") and not exec_error:
        exec_error = state["error"]

    # 8. Apply controlled state change if requested (e.g. conflicting state)
    if case.state_change and case.state_change.get("at") == "pre_approval":
        act = case.state_change.get("action")
        with get_sandbox_connection(settings.il_sandbox_database_url) as s_conn:
            with s_conn.cursor() as cur:
                if act == "advance_revision":
                    cur.execute(
                        "UPDATE sandbox.target_state SET revision = revision + 1 WHERE id = 1"
                    )
                elif act == "resume_worker":
                    cur.execute(
                        """
                        UPDATE sandbox.target_state
                        SET worker_running = TRUE, revision = revision + 1
                        WHERE id = 1
                        """
                    )
                elif act == "clear_maintenance":
                    cur.execute(
                        """
                        UPDATE sandbox.target_state
                        SET maintenance = FALSE, revision = revision + 1
                        WHERE id = 1
                        """
                    )
            s_conn.commit()

    # 9. Deterministic evaluation owner decision & execution driver
    authority_decision = None
    execution_claim_data = None
    execution_receipt_data = None
    verification_data = None

    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, outcome FROM control.incidents WHERE id = %s",
                (incident_id,),
            )
            inc_info = cur.fetchone()
            cur_inc_state = inc_info[0] if inc_info else "unknown"

    proposal_data = state.get("proposal")
    diagnosis_data = state.get("diagnosis")

    if cur_inc_state == "awaiting_approval" and proposal_data:
        proposal_id = UUID(proposal_data["proposal_id"])
        action = proposal_data.get("payload", {}).get("action")
        if action in ("set_inventory_endpoint", "resume_order_worker"):
            decision_payload = ApprovalDecision(
                proposal_id=proposal_id,
                payload_digest=proposal_data["payload_digest"],
                target_revision=proposal_data["target_revision"],
                decision="approve",
            )
            try:
                await decide_proposal(
                    incident_id=incident_id,
                    owner_id="eval-owner",
                    decision=decision_payload,
                    settings=settings,
                )
                authority_decision = "approved"
            except ApprovalError as ae:
                if (
                    ae.code in ("target_revision_stale", "stale_revision")
                    or ae.status_code == 409
                    or "revision" in str(ae).lower()
                    or "stale" in str(ae).lower()
                ):
                    authority_decision = "reject_stale"
                else:
                    raise
            except Exception as ve:
                ve_str = str(ve).lower()
                if "revision" in ve_str or "stale" in ve_str or "rejected" in ve_str:
                    authority_decision = "reject_stale"
                else:
                    raise

            if authority_decision == "approved":
                try:
                    claim = await claim_execution(
                        incident_id=incident_id,
                        proposal_id=proposal_id,
                        settings=settings,
                    )
                    execution_claim_data = claim.model_dump(mode="json")

                    receipt = await execute_claim(claim, settings=settings)
                    execution_receipt_data = receipt.model_dump(mode="json")

                    if receipt.result == "applied":
                        verif = await verify_checkout(
                            incident_id, request_key=uuid4(), settings=settings
                        )
                        verification_data = verif.model_dump(mode="json")
                except ApprovalError as ce:
                    ce_codes = ("target_revision_stale", "stale_revision", "precondition_failed")
                    if (
                        ce.code in ce_codes
                        or ce.status_code == 409
                        or "revision" in str(ce).lower()
                        or "stale" in str(ce).lower()
                    ):
                        authority_decision = "reject_stale"
                        logger.info("Claim refused as stale: %s", ce)
                    else:
                        raise
                except Exception as ce:
                    ce_str = str(ce).lower()
                    if "revision" in ce_str or "stale" in ce_str or "409" in str(ce):
                        authority_decision = "reject_stale"
                        logger.info("Claim refused as stale: %s", ce)
                    else:
                        raise

    # Fetch final incident state and target state after
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM control.incidents WHERE id = %s",
                (incident_id,),
            )
            final_inc_state = cur.fetchone()[0]

            # Fetch audit events for metrics
            cur.execute(
                """
                SELECT event_type, payload, duration_ms, token_count
                FROM control.events
                WHERE incident_id = %s
                ORDER BY seq ASC
                """,
                (incident_id,),
            )
            event_rows = cur.fetchall()

    tool_calls = []
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    for ev_name, ev_payload, ev_dur, ev_tokens in event_rows:
        if ev_name == "tool_executed":
            tool_calls.append(ev_payload)
        if ev_name == "model_called" and isinstance(ev_payload, dict):
            try:
                inp = int(ev_payload.get("input_tokens") or 0)
            except Exception:
                inp = 0
            try:
                out = int(ev_payload.get("output_tokens") or 0)
            except Exception:
                out = 0
            input_tokens += inp
            output_tokens += out
        if ev_tokens:
            try:
                total_tokens += int(ev_tokens)
            except Exception:
                pass

    if input_tokens == 0 and total_tokens > 0:
        input_tokens = total_tokens

    target_state_after = {}
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r_conf = await ac.get(
            f"{settings.il_sandbox_base_url}/diagnostics/config",
            headers={"Authorization": f"Bearer {settings.il_sandbox_read_token}"},
        )
        if r_conf.status_code == 200:
            target_state_after = r_conf.json()

    active_duration = time.monotonic() - t_start

    return CaseArtifact(
        case_id=case.case_id,
        split=case.split,
        family=case.family,
        approach=approach,
        run_id=run_id,
        timestamp=datetime.now(UTC).isoformat(),
        commit=commit_sha,
        model_name=settings.il_model,
        initial_conditions=case.initial_conditions,
        injected_fault=inj_data,
        failed_request_id=str(failed_req_id) if failed_req_id else None,
        failed_order_id=str(failed_order_id) if failed_order_id else None,
        investigation_evidence=state.get("recent_results", []),
        tool_calls=tool_calls,
        retrieved_runbooks=state.get("retrieved_runbooks", []),
        diagnosis=diagnosis_data,
        proposal=proposal_data,
        authority_decision=authority_decision,
        execution_claim=execution_claim_data,
        execution_receipt=execution_receipt_data,
        target_state_before=target_state_before,
        target_state_after=target_state_after,
        verification=verification_data,
        final_incident_state=final_inc_state,
        tokens={
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        durations={"active_seconds": round(active_duration, 3)},
        error=exec_error,
    )


def load_recipes(cases_path: Path) -> list[CaseRecipe]:
    """Load case recipes from jsonl file."""
    recipes: list[CaseRecipe] = []
    with cases_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                recipes.append(CaseRecipe.from_dict(data))
    return recipes


async def run_evaluation(
    split: str,
    approach: str,
    case_filter: str | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Execute evaluation split and emit case artifacts JSON."""
    cases_path = Path("evals") / "cases" / f"{split}.jsonl"
    if not cases_path.exists():
        raise FileNotFoundError(f"Cases file {cases_path} does not exist")

    all_recipes = load_recipes(cases_path)
    if case_filter:
        recipes = [r for r in all_recipes if r.case_id == case_filter]
        if not recipes:
            raise ValueError(f"No case matching filter '{case_filter}' in {split} split")
    else:
        recipes = all_recipes

    settings = Settings.from_env()
    artifacts: list[CaseArtifact] = []

    sys.stderr.write(
        f"Starting evaluation split='{split}' approach='{approach}' "
        f"cases={len(recipes)} model='{settings.il_model}'\n"
    )

    for idx, case in enumerate(recipes):
        sys.stderr.write(
            f"[{idx + 1}/{len(recipes)}] Running case {case.case_id} "
            f"({case.family}/{case.variation})...\n"
        )
        art = await run_case(case, approach=approach, settings=settings)
        sys.stderr.write(
            f"    -> outcome: state={art.final_incident_state} decision={art.authority_decision} "
            f"duration={art.durations.get('active_seconds')}s\n"
        )
        artifacts.append(art)

    run_output = {
        "split": split,
        "approach": approach,
        "model": settings.il_model,
        "timestamp": datetime.now(UTC).isoformat(),
        "total_cases": len(artifacts),
        "artifacts": [asdict(a) for a in artifacts],
    }

    if output_path is None:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        output_path = Path("artifacts") / "evals" / f"{split}_{approach}_{ts}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(run_output, indent=2), encoding="utf-8")
    sys.stderr.write(f"Run output successfully written to {output_path}\n")

    return run_output


def main():
    parser = argparse.ArgumentParser(description="IncidentLab Evaluation Runner")
    parser.add_argument(
        "--split",
        type=str,
        default="dev",
        choices=["dev", "heldout"],
        help="Evaluation split to run",
    )
    parser.add_argument(
        "--approach",
        type=str,
        default="agent",
        choices=["agent", "fixed"],
        help="Evaluation approach (agent or fixed baseline)",
    )
    parser.add_argument("--case", type=str, default=None, help="Filter to single case ID")
    parser.add_argument("--output", type=Path, default=None, help="Output run JSON file path")

    args = parser.parse_args()
    asyncio.run(
        run_evaluation(
            split=args.split,
            approach=args.approach,
            case_filter=args.case,
            output_path=args.output,
        )
    )


if __name__ == "__main__":
    main()
