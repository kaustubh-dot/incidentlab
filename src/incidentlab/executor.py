"""Executor-owned fixed HTTP operations for sandbox checkout, order polling, and repairs."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

import httpx
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from incidentlab.config import Settings
from incidentlab.db import get_control_connection
from incidentlab.events import record_event
from incidentlab.sandbox.schemas import CheckoutAccepted, OrderView
from incidentlab.schemas import (
    ExecutionClaim,
    ExecutionView,
    RepairPayload,
    RepairReceipt,
)

_repair_payload_adapter = TypeAdapter(RepairPayload)


class CheckoutError(Exception):
    """Raised when sandbox checkout fails (e.g. dependency unavailable)."""

    def __init__(self, code: str, message: str, request_id: str | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.request_id = request_id


async def submit_checkout(
    request_key: UUID,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> CheckoutAccepted:
    """Submit a fake checkout request using the executor-owned write credential.

    Returns CheckoutAccepted on HTTP 202. Raises CheckoutError on HTTP 503/error.
    """
    if settings is None:
        settings = Settings.from_env()

    url = f"{settings.il_sandbox_base_url}/checkout"
    headers = {
        "Authorization": f"Bearer {settings.il_sandbox_write_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "request_key": str(request_key),
        "sku": "SKU-1",
        "quantity": 1,
    }

    try:
        if client is not None:
            resp = await client.post(url, headers=headers, json=payload, timeout=10.0)
        else:
            async with httpx.AsyncClient(timeout=10.0) as ac:
                resp = await ac.post(url, headers=headers, json=payload)

        if resp.status_code == 202:
            data = resp.json()
            return CheckoutAccepted(
                request_id=UUID(data["request_id"]),
                order_id=UUID(data["order_id"]),
                status=data["status"],
            )

        # Parse structured error
        try:
            err_body = resp.json()
            err_detail = err_body.get("error", err_body)
            code = err_detail.get("code", f"http_{resp.status_code}")
            message = err_detail.get("message", resp.text)
            req_id = err_detail.get("request_id")
        except Exception:
            code = f"http_{resp.status_code}"
            message = resp.text
            req_id = None

        raise CheckoutError(code=code, message=message, request_id=req_id)
    except httpx.HTTPError as exc:
        raise CheckoutError(
            code="network_error",
            message=f"Failed to communicate with sandbox: {exc}",
        ) from exc


async def get_order_status(
    order_id: UUID,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> OrderView:
    """Read back order details from sandbox using read token."""
    if settings is None:
        settings = Settings.from_env()

    url = f"{settings.il_sandbox_base_url}/orders/{order_id}"
    headers = {
        "Authorization": f"Bearer {settings.il_sandbox_read_token}",
    }

    if client is not None:
        resp = await client.get(url, headers=headers, timeout=5.0)
    else:
        async with httpx.AsyncClient(timeout=5.0) as ac:
            resp = await ac.get(url, headers=headers)

    if resp.status_code != 200:
        raise RuntimeError(f"Order lookup failed with status {resp.status_code}: {resp.text}")

    data = resp.json()
    return OrderView.model_validate(data)


async def wait_for_order_fulfillment(
    order_id: UUID,
    settings: Settings | None = None,
    timeout_seconds: float = 10.0,
    poll_interval: float = 0.25,
    client: httpx.AsyncClient | None = None,
) -> OrderView:
    """Poll order status until fulfilled or timeout expires."""
    deadline = time.monotonic() + timeout_seconds
    last_order: OrderView | None = None

    while time.monotonic() < deadline:
        try:
            order = await get_order_status(order_id, settings=settings, client=client)
            last_order = order
            if order.status == "fulfilled":
                return order
        except Exception:
            pass
        await asyncio.sleep(poll_interval)

    if last_order is not None:
        return last_order
    raise TimeoutError(f"Timed out waiting for order {order_id} after {timeout_seconds}s")


async def execute_claim(
    claim: ExecutionClaim,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> RepairReceipt:
    """Dispatch the stored finite repair once without automatic retries.

    Validates stored payload, action allowlist, digest, revision, and cancellation.
    Persists 'dispatching' before dispatching the HTTP call.
    """
    if settings is None:
        settings = Settings.from_env()

    # 1. Action allowlist check: exactly the two allowed actions
    if claim.payload.action not in ("set_inventory_endpoint", "resume_order_worker"):
        raise ValueError(
            f"Action '{claim.payload.action}' is not supported"
        )

    # 2. Check cancellation and state under control DB lock
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cancel_requested, state
                FROM control.incidents
                WHERE id = %s
                FOR UPDATE
                """,
                (claim.incident_id,),
            )
            inc_row = cur.fetchone()
            if not inc_row:
                raise RuntimeError(f"Incident {claim.incident_id} not found")
            cancel_requested, _inc_state = inc_row[0], inc_row[1]
            if cancel_requested:
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'cancelled', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (claim.incident_id,),
                )
                record_event(
                    conn,
                    claim.incident_id,
                    "cancel.requested",
                    {"reason": "cancellation requested before dispatch"},
                )
                conn.commit()
                raise RuntimeError("Execution cancelled before dispatch")

            # Check proposal expiry
            cur.execute(
                """
                SELECT expires_at
                FROM control.proposals
                WHERE id = %s
                """,
                (claim.proposal_id,),
            )
            prop_row = cur.fetchone()
            if not prop_row:
                raise RuntimeError(f"Proposal {claim.proposal_id} not found")
            expires_at = prop_row[0]
            if datetime.now(UTC) > expires_at:
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'expired', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (claim.incident_id,),
                )
                record_event(
                    conn,
                    claim.incident_id,
                    "error",
                    {
                        "code": "approval_expired",
                        "message": "Proposal expired before dispatch",
                    },
                )
                conn.commit()
                raise RuntimeError("Proposal expired before dispatch")

            # Persist state 'dispatching' before the HTTP call
            cur.execute(
                """
                UPDATE control.executions
                SET state = 'dispatching', dispatched_at = NOW()
                WHERE execution_key = %s
                """,
                (claim.execution_key,),
            )
            record_event(
                conn,
                claim.incident_id,
                "execution.dispatched",
                {
                    "execution_key": str(claim.execution_key),
                    "action": claim.payload.action,
                    "expected_revision": claim.expected_revision,
                },
            )
            conn.commit()

    # 3. Dispatch the single RepairRequest POST (never automatic HTTP retries)
    url = f"{settings.il_sandbox_base_url}/repairs"
    headers = {
        "Authorization": f"Bearer {settings.il_sandbox_write_token}",
        "Content-Type": "application/json",
    }
    req_body = {
        "execution_key": str(claim.execution_key),
        "incident_id": str(claim.incident_id),
        "proposal_id": str(claim.proposal_id),
        "payload": claim.payload.model_dump(mode="json"),
        "payload_digest": claim.payload_digest,
        "expected_revision": claim.expected_revision,
    }

    try:
        if client is not None:
            resp = await client.post(url, headers=headers, json=req_body, timeout=10.0)
        else:
            async with httpx.AsyncClient(timeout=10.0) as ac:
                resp = await ac.post(url, headers=headers, json=req_body)
    except Exception as net_err:
        # Mark unknown and attempt receipt reconciliation
        with get_control_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE control.executions
                    SET state = 'unknown'
                    WHERE execution_key = %s
                    """,
                    (claim.execution_key,),
                )
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'execution_unknown', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (claim.incident_id,),
                )
                record_event(
                    conn,
                    claim.incident_id,
                    "error",
                    {
                        "code": "execution_outcome_unknown",
                        "message": f"Timeout or network error during dispatch: {net_err}",
                        "execution_key": str(claim.execution_key),
                    },
                )
                conn.commit()

        # Reconcile receipt via read-only lookup
        reconcile_res = await reconcile_execution(
            claim.execution_key, settings=settings, client=client
        )
        if reconcile_res.receipt is not None:
            return reconcile_res.receipt
        raise RuntimeError(
            f"Dispatch failed with network error and receipt could not be reconciled: {net_err}"
        ) from net_err

    if resp.status_code == 409:
        # Mismatched execution key conflict
        with get_control_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE control.executions
                    SET state = 'unknown'
                    WHERE execution_key = %s
                    """,
                    (claim.execution_key,),
                )
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'failed', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (claim.incident_id,),
                )
                record_event(
                    conn,
                    claim.incident_id,
                    "error",
                    {
                        "code": "execution_key_conflict",
                        "message": "Key conflict in sandbox",
                    },
                )
                conn.commit()
        raise RuntimeError("Execution key conflict reported by sandbox")

    if resp.status_code != 200:
        raise RuntimeError(
            f"Sandbox repair failed with HTTP {resp.status_code}: {resp.text}"
        )

    receipt_data = resp.json()
    receipt = RepairReceipt.model_validate(receipt_data)

    # 4. Store returned receipt and update incident state
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE control.executions
                SET state = %s, receipt = %s
                WHERE execution_key = %s
                """,
                (
                    receipt.result,
                    Jsonb(receipt.model_dump(mode="json")),
                    claim.execution_key,
                ),
            )
            record_event(
                conn,
                claim.incident_id,
                "execution.receipt",
                receipt.model_dump(mode="json"),
            )
            if receipt.result == "applied":
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'verifying', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (claim.incident_id,),
                )
            else:
                # Precondition failed -> stale/failed
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'failed', updated_at = NOW(),
                        outcome = %s
                    WHERE id = %s
                    """,
                    (
                        Jsonb(
                            {
                                "error": "precondition_failed",
                                "receipt": receipt.model_dump(mode="json"),
                            }
                        ),
                        claim.incident_id,
                    ),
                )
            conn.commit()

    return receipt


async def reconcile_execution(
    execution_key: UUID,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> ExecutionView:
    """Read a durable sandbox receipt via GET /repairs/{key}; never dispatches."""
    if settings is None:
        settings = Settings.from_env()

    # Read current execution record from control DB
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.execution_key, e.proposal_id, e.incident_id, e.state,
                       e.payload_digest, e.expected_revision, e.claimed_at,
                       e.dispatched_at, e.receipt, p.payload
                FROM control.executions e
                JOIN control.proposals p ON e.proposal_id = p.id
                WHERE e.execution_key = %s
                """,
                (execution_key,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Execution {execution_key} not found")

    (
        exec_key,
        prop_id,
        inc_id,
        exec_state,
        digest,
        expected_rev,
        claimed_at,
        dispatched_at,
        stored_receipt,
        payload_data,
    ) = row

    payload = _repair_payload_adapter.validate_python(payload_data)
    receipt_obj = (
        RepairReceipt.model_validate(stored_receipt) if stored_receipt else None
    )

    # Query sandbox read-only GET /repairs/{execution_key}
    url = f"{settings.il_sandbox_base_url}/repairs/{execution_key}"
    headers = {"Authorization": f"Bearer {settings.il_sandbox_write_token}"}

    try:
        if client is not None:
            resp = await client.get(url, headers=headers, timeout=5.0)
        else:
            async with httpx.AsyncClient(timeout=5.0) as ac:
                resp = await ac.get(url, headers=headers)

        if resp.status_code == 200:
            receipt = RepairReceipt.model_validate(resp.json())
            # Update control DB if not already saved
            with get_control_connection(settings) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE control.executions
                        SET state = %s, receipt = %s
                        WHERE execution_key = %s
                        """,
                        (
                            receipt.result,
                            Jsonb(receipt.model_dump(mode="json")),
                            execution_key,
                        ),
                    )
                    if receipt.result == "applied":
                        cur.execute(
                            """
                            UPDATE control.incidents
                            SET state = 'verifying', updated_at = NOW()
                            WHERE id = %s AND state = 'execution_unknown'
                            """,
                            (inc_id,),
                        )
                    conn.commit()

            return ExecutionView(
                execution_key=exec_key,
                proposal_id=prop_id,
                incident_id=inc_id,
                state=receipt.result,
                payload_digest=digest,
                expected_revision=expected_rev,
                payload=payload,
                claimed_at=claimed_at,
                dispatched_at=dispatched_at,
                receipt=receipt,
                reconciled=True,
                error=None,
            )
        elif resp.status_code == 404:
            # Receipt not found in sandbox
            return ExecutionView(
                execution_key=exec_key,
                proposal_id=prop_id,
                incident_id=inc_id,
                state=exec_state,
                payload_digest=digest,
                expected_revision=expected_rev,
                payload=payload,
                claimed_at=claimed_at,
                dispatched_at=dispatched_at,
                receipt=receipt_obj,
                reconciled=False,
                error="Receipt not found in sandbox",
            )
        else:
            return ExecutionView(
                execution_key=exec_key,
                proposal_id=prop_id,
                incident_id=inc_id,
                state=exec_state,
                payload_digest=digest,
                expected_revision=expected_rev,
                payload=payload,
                claimed_at=claimed_at,
                dispatched_at=dispatched_at,
                receipt=receipt_obj,
                reconciled=False,
                error=f"Sandbox returned status {resp.status_code}: {resp.text}",
            )
    except Exception as exc:
        return ExecutionView(
            execution_key=exec_key,
            proposal_id=prop_id,
            incident_id=inc_id,
            state=exec_state,
            payload_digest=digest,
            expected_revision=expected_rev,
            payload=payload,
            claimed_at=claimed_at,
            dispatched_at=dispatched_at,
            receipt=receipt_obj,
            reconciled=False,
            error=str(exc),
        )
