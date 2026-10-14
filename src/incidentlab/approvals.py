"""Bound owner approvals, atomic execution claims, and authority validation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from incidentlab.config import Settings
from incidentlab.db import get_control_connection
from incidentlab.events import record_event
from incidentlab.graph import compute_payload_digest
from incidentlab.schemas import (
    ApprovalDecision,
    ApprovalView,
    ExecutionClaim,
    RepairPayload,
    ResumeOrderWorkerPayload,
    SetInventoryEndpointPayload,
)

logger = logging.getLogger("incidentlab.approvals")


class ApprovalError(Exception):
    """Base exception for approval and claim validation errors."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _parse_repair_payload(payload_data: dict[str, Any]) -> RepairPayload:
    """Parse raw JSON dict into typed RepairPayload discriminated union."""
    action = payload_data.get("action")
    if action == "set_inventory_endpoint":
        return SetInventoryEndpointPayload.model_validate(payload_data)
    elif action == "resume_order_worker":
        return ResumeOrderWorkerPayload.model_validate(payload_data)
    raise ValueError(f"Unknown repair action: {action}")


async def decide_proposal(
    incident_id: UUID,
    owner_id: str,
    decision: ApprovalDecision,
    settings: Settings,
    conn: psycopg.Connection | None = None,
) -> ApprovalView:
    """Atomically bind authenticated owner decision to stored immutable proposal.

    Enforces row lock on incident and proposal, verifies canonical payload digest
    against stored payload, checks target revision, enforces expiry and cancellation,
    allows identical duplicate readback, and rejects conflicting decisions.
    """
    if conn is not None:
        return _decide_proposal_tx(conn, incident_id, owner_id, decision)

    with get_control_connection(settings) as managed_conn:
        return _decide_proposal_tx(managed_conn, incident_id, owner_id, decision)


def _decide_proposal_tx(
    conn: psycopg.Connection,
    incident_id: UUID,
    owner_id: str,
    decision: ApprovalDecision,
) -> ApprovalView:
    with conn.cursor() as cur:
        # 1. Lock incident row
        cur.execute(
            """
            SELECT state, cancel_requested, active_ms
            FROM control.incidents
            WHERE id = %s
            FOR UPDATE
            """,
            (incident_id,),
        )
        inc_row = cur.fetchone()
        if not inc_row:
            raise ApprovalError("incident_not_found", f"Incident {incident_id} not found", 404)

        inc_state, cancel_requested, _active_ms = inc_row[0], inc_row[1], inc_row[2]

        if cancel_requested:
            raise ApprovalError(
                "incident_cancelled", "Incident cancellation has been requested", 409
            )

        # 2. Check if decision already recorded for this proposal
        cur.execute(
            """
            SELECT decision, owner_id, reviewed_digest, reviewed_revision, decided_at
            FROM control.approvals
            WHERE proposal_id = %s
            FOR UPDATE
            """,
            (decision.proposal_id,),
        )
        existing_approval = cur.fetchone()

        if existing_approval:
            ex_decision, ex_owner, ex_digest, ex_rev, ex_decided_at = (
                existing_approval[0],
                existing_approval[1],
                existing_approval[2],
                existing_approval[3],
                existing_approval[4],
            )
            # Idempotent readback if identical
            if (
                ex_decision == decision.decision
                and ex_digest == decision.payload_digest
                and ex_rev == decision.target_revision
            ):
                return ApprovalView(
                    proposal_id=decision.proposal_id,
                    decision=ex_decision,
                    owner_id=ex_owner,
                    reviewed_digest=ex_digest,
                    reviewed_revision=ex_rev,
                    decided_at=ex_decided_at,
                )
            # Conflicting decision error
            raise ApprovalError(
                "conflicting_decision_error",
                (
                    f"Proposal {decision.proposal_id} already has a "
                    f"conflicting decision '{ex_decision}'"
                ),
                409,
            )

        if inc_state != "awaiting_approval":
            raise ApprovalError(
                "invalid_incident_state",
                f"Incident {incident_id} is in state '{inc_state}', not 'awaiting_approval'",
                409,
            )

        # 3. Lock proposal row
        cur.execute(
            """
            SELECT id, incident_id, payload, payload_digest, target_revision, expires_at
            FROM control.proposals
            WHERE id = %s AND incident_id = %s
            FOR UPDATE
            """,
            (decision.proposal_id, incident_id),
        )
        prop_row = cur.fetchone()
        if not prop_row:
            raise ApprovalError(
                "proposal_not_found",
                f"Proposal {decision.proposal_id} not found for incident {incident_id}",
                404,
            )

        stored_payload = prop_row[2]
        stored_digest = prop_row[3]
        target_revision = prop_row[4]
        expires_at = prop_row[5]

        # 4. Check expiration
        now = datetime.now(UTC)
        if now > expires_at:
            cur.execute(
                """
                UPDATE control.incidents
                SET state = 'expired', updated_at = NOW()
                WHERE id = %s
                """,
                (incident_id,),
            )
            conn.commit()
            record_event(
                conn,
                incident_id,
                "proposal_expired",
                {
                    "proposal_id": str(decision.proposal_id),
                    "expires_at": expires_at.isoformat(),
                },
            )
            raise ApprovalError(
                "proposal_expired",
                f"Proposal {decision.proposal_id} expired at {expires_at.isoformat()}",
                409,
            )

        # 5. Verify recomputed canonical stored digest
        recomputed_digest = compute_payload_digest(stored_payload)
        if recomputed_digest != stored_digest:
            raise ApprovalError(
                "stored_digest_corrupted",
                "Stored proposal digest does not match canonical recomputed digest",
                500,
            )

        # 6. Check client payload_digest against immutable stored digest
        if decision.payload_digest != stored_digest:
            raise ApprovalError(
                "payload_digest_mismatch",
                (
                    f"Client digest {decision.payload_digest} does not match "
                    f"stored proposal digest {stored_digest}"
                ),
                400,
            )

        # 7. Check client target_revision against stored revision
        if decision.target_revision != target_revision:
            raise ApprovalError(
                "target_revision_mismatch",
                (
                    f"Client revision {decision.target_revision} does not match "
                    f"proposal revision {target_revision}"
                ),
                400,
            )

        # 8. Record approval
        decided_at = datetime.now(UTC)
        cur.execute(
            """
            INSERT INTO control.approvals (
                proposal_id, decision, owner_id, reviewed_digest, reviewed_revision, decided_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                decision.proposal_id,
                decision.decision,
                owner_id,
                decision.payload_digest,
                decision.target_revision,
                decided_at,
            ),
        )

        # 9. Update incident state
        new_state = "approved" if decision.decision == "approve" else "rejected"
        cur.execute(
            """
            UPDATE control.incidents
            SET state = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (new_state, incident_id),
        )

        event_name = "proposal_approved" if decision.decision == "approve" else "proposal_rejected"
        record_event(
            conn,
            incident_id,
            event_name,
            {
                "proposal_id": str(decision.proposal_id),
                "decision": decision.decision,
                "owner_id": owner_id,
                "reviewed_digest": decision.payload_digest,
                "reviewed_revision": decision.target_revision,
            },
        )
        conn.commit()

        return ApprovalView(
            proposal_id=decision.proposal_id,
            decision=decision.decision,
            owner_id=owner_id,
            reviewed_digest=decision.payload_digest,
            reviewed_revision=decision.target_revision,
            decided_at=decided_at,
        )


async def check_target_revision(settings: Settings) -> int:
    """Precheck current sandbox target revision via read-only diagnostics."""
    url = f"{settings.il_sandbox_base_url}/diagnostics/health"
    headers = {"Authorization": f"Bearer {settings.il_sandbox_read_token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Sandbox diagnostics unreachable: HTTP {resp.status_code} - {resp.text}"
            )
        data = resp.json()
        return int(data["target_revision"])


async def claim_execution(
    incident_id: UUID,
    proposal_id: UUID,
    settings: Settings,
    conn: psycopg.Connection | None = None,
    verify_precheck: bool = True,
) -> ExecutionClaim:
    """Atomically create one execution claim after authority and precheck validation.

    Ensures incident is approved, proposal is valid and unexpired, checks target revision,
    allocates exactly one execution key per proposal, and transitions incident to executing.
    """
    current_rev: int | None = None
    if verify_precheck:
        try:
            current_rev = await check_target_revision(settings)
        except Exception as exc:
            logger.warning("Target revision precheck failed: %s", exc)
            raise ApprovalError(
                "sandbox_precheck_failed",
                f"Failed to verify sandbox target revision: {exc}",
                503,
            ) from exc

    if conn is not None:
        return _claim_execution_tx(conn, incident_id, proposal_id, settings, current_rev)

    with get_control_connection(settings) as managed_conn:
        return _claim_execution_tx(managed_conn, incident_id, proposal_id, settings, current_rev)


def _claim_execution_tx(
    conn: psycopg.Connection,
    incident_id: UUID,
    proposal_id: UUID,
    settings: Settings,
    current_rev: int | None = None,
) -> ExecutionClaim:
    with conn.cursor() as cur:
        # 1. Lock incident
        cur.execute(
            """
            SELECT state, cancel_requested
            FROM control.incidents
            WHERE id = %s
            FOR UPDATE
            """,
            (incident_id,),
        )
        inc_row = cur.fetchone()
        if not inc_row:
            raise ApprovalError("incident_not_found", f"Incident {incident_id} not found", 404)

        inc_state, cancel_requested = inc_row[0], inc_row[1]
        if cancel_requested:
            raise ApprovalError(
                "incident_cancelled", "Incident cancellation has been requested", 409
            )

        # 2. Check if claim already exists (idempotent / exactly one execution key)
        cur.execute(
            """
            SELECT execution_key, state, payload_digest, expected_revision,
                   claimed_at, dispatched_at, receipt
            FROM control.executions
            WHERE proposal_id = %s
            FOR UPDATE
            """,
            (proposal_id,),
        )
        existing_claim = cur.fetchone()
        if existing_claim:
            cur.execute(
                "SELECT payload FROM control.proposals WHERE id = %s",
                (proposal_id,),
            )
            p_row = cur.fetchone()
            parsed_payload = _parse_repair_payload(p_row[0]) if p_row else None
            return ExecutionClaim(
                execution_key=existing_claim[0],
                proposal_id=proposal_id,
                incident_id=incident_id,
                state=existing_claim[1],
                payload_digest=existing_claim[2],
                expected_revision=existing_claim[3],
                payload=parsed_payload,
                claimed_at=existing_claim[4],
                dispatched_at=existing_claim[5],
                receipt=existing_claim[6],
            )

        if inc_state not in ("approved", "executing"):
            raise ApprovalError(
                "incident_not_approved",
                f"Incident {incident_id} is in state '{inc_state}', not 'approved'",
                409,
            )

        # 3. Lock proposal
        cur.execute(
            """
            SELECT id, payload, payload_digest, target_revision, expires_at
            FROM control.proposals
            WHERE id = %s AND incident_id = %s
            FOR UPDATE
            """,
            (proposal_id, incident_id),
        )
        prop_row = cur.fetchone()
        if not prop_row:
            raise ApprovalError(
                "proposal_not_found", f"Proposal {proposal_id} not found for incident", 404
            )

        stored_payload = prop_row[1]
        stored_digest = prop_row[2]
        expected_revision = prop_row[3]
        expires_at = prop_row[4]

        # 3. Expiry check
        if datetime.now(UTC) > expires_at:
            cur.execute(
                "UPDATE control.incidents SET state = 'expired', updated_at = NOW() WHERE id = %s",
                (incident_id,),
            )
            conn.commit()
            record_event(
                conn,
                incident_id,
                "proposal_expired",
                {"proposal_id": str(proposal_id), "expires_at": expires_at.isoformat()},
            )
            raise ApprovalError("proposal_expired", f"Proposal {proposal_id} has expired", 409)

        # 4. Check approval decision
        cur.execute(
            """
            SELECT decision
            FROM control.approvals
            WHERE proposal_id = %s
            FOR UPDATE
            """,
            (proposal_id,),
        )
        app_row = cur.fetchone()
        if not app_row or app_row[0] != "approve":
            dec_str = app_row[0] if app_row else "none"
            raise ApprovalError(
                "proposal_not_approved",
                f"Proposal {proposal_id} is not approved (decision: {dec_str})",
                409,
            )

        # 5. Current revision precheck
        if current_rev is not None:
            if current_rev != expected_revision:
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'stale', updated_at = NOW(),
                        outcome = %s
                    WHERE id = %s
                    """,
                    (
                        Jsonb(
                            {
                                "error": "target_revision_stale",
                                "expected_revision": expected_revision,
                                "current_revision": current_rev,
                            }
                        ),
                        incident_id,
                    ),
                )
                conn.commit()
                record_event(
                    conn,
                    incident_id,
                    "execution_stale",
                    {
                        "proposal_id": str(proposal_id),
                        "expected_revision": expected_revision,
                        "current_revision": current_rev,
                    },
                )
                raise ApprovalError(
                    "target_revision_stale",
                    (
                        f"Target revision changed: expected {expected_revision}, "
                        f"current {current_rev}"
                    ),
                    409,
                )

        # 7. Create unique execution claim
        execution_key = uuid4()
        claimed_at = datetime.now(UTC)
        cur.execute(
            """
            INSERT INTO control.executions (
                execution_key, proposal_id, incident_id, state,
                payload_digest, expected_revision, claimed_at
            )
            VALUES (%s, %s, %s, 'claimed', %s, %s, %s)
            """,
            (
                execution_key,
                proposal_id,
                incident_id,
                stored_digest,
                expected_revision,
                claimed_at,
            ),
        )

        # 8. Transition incident to executing
        cur.execute(
            """
            UPDATE control.incidents
            SET state = 'executing', updated_at = NOW()
            WHERE id = %s
            """,
            (incident_id,),
        )

        record_event(
            conn,
            incident_id,
            "execution_claimed",
            {
                "execution_key": str(execution_key),
                "proposal_id": str(proposal_id),
                "expected_revision": expected_revision,
                "payload_digest": stored_digest,
            },
        )
        conn.commit()

        parsed_payload = _parse_repair_payload(stored_payload)
        return ExecutionClaim(
            execution_key=execution_key,
            proposal_id=proposal_id,
            incident_id=incident_id,
            state="claimed",
            payload_digest=stored_digest,
            expected_revision=expected_revision,
            payload=parsed_payload,
            claimed_at=claimed_at,
        )
