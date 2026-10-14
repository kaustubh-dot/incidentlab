"""Sandbox core operational logic, inventory checks, order management, and logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg

from incidentlab.sandbox.schemas import (
    CheckoutAccepted,
    DiagnosticsConfig,
    DiagnosticsHealth,
    LogEntry,
    OrderView,
)
from incidentlab.schemas import (
    ApiErrorDetail,
    ApiErrorResponse,
    RepairReceipt,
    RepairRequest,
)


def record_log(
    conn: psycopg.Connection,
    event_kind: str,
    message: str,
    request_id: UUID | None = None,
    order_id: UUID | None = None,
    severity: str = "INFO",
    data: dict[str, Any] | None = None,
) -> int:
    """Record a structured log entry into sandbox.logs with bounded message length."""
    truncated = False
    if len(message) > 512:
        message = message[:512]
        truncated = True

    log_data = dict(data) if data is not None else {}
    if truncated:
        log_data["truncated"] = True

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sandbox.logs (
                timestamp, request_id, order_id, event_kind, severity, message, data
            )
            VALUES (NOW(), %s, %s, %s, %s, %s, %s)
            RETURNING seq
            """,
            (
                request_id,
                order_id,
                event_kind,
                severity,
                message,
                json.dumps(log_data) if log_data else None,
            ),
        )
        row = cur.fetchone()
        return row[0] if row else 0


def get_target_state(conn: psycopg.Connection) -> dict[str, Any]:
    """Retrieve the current sandbox target state."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT revision, endpoint_key, worker_running, worker_id, maintenance
            FROM sandbox.target_state
            WHERE id = 1
            """
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("sandbox.target_state row 1 not found")
        return {
            "revision": row[0],
            "endpoint_key": row[1],
            "worker_running": row[2],
            "worker_id": row[3],
            "maintenance": row[4],
        }


async def check_inventory_endpoint(
    endpoint_key: str,
    port: int = 8001,
    client: httpx.AsyncClient | None = None,
) -> tuple[int, dict[str, Any]]:
    """Perform fixed HTTP request to inventory dependency route."""
    if endpoint_key == "inventory-local":
        path = "/dependency/inventory/SKU-1"
    else:
        path = "/dependency/unavailable/SKU-1"

    url = f"http://127.0.0.1:{port}{path}"

    try:
        if client is not None:
            resp = await client.get(url, timeout=5.0)
        else:
            async with httpx.AsyncClient(timeout=5.0) as ac:
                resp = await ac.get(url)

        try:
            data = resp.json()
        except Exception:
            data = {"detail": resp.text}
        return resp.status_code, data
    except Exception as exc:
        return 503, {"error": "inventory_unavailable", "detail": str(exc)}


async def create_checkout(
    conn: psycopg.Connection,
    request_key: UUID,
    sku: str = "SKU-1",
    quantity: int = 1,
    port: int = 8001,
    client: httpx.AsyncClient | None = None,
) -> tuple[CheckoutAccepted | None, ApiErrorResponse | None, int]:
    """Process a checkout request idempotently with live inventory dependency check."""
    with conn.cursor() as cur:
        # Check for existing order with this unique request_key
        cur.execute(
            """
            SELECT order_id, request_id, status
            FROM sandbox.orders
            WHERE request_key = %s
            """,
            (request_key,),
        )
        row = cur.fetchone()
        if row:
            return (
                CheckoutAccepted(
                    request_id=row[1],
                    order_id=row[0],
                    status=row[2],
                ),
                None,
                202,
            )

        # Read current target state
        cur.execute(
            "SELECT endpoint_key FROM sandbox.target_state WHERE id = 1"
        )
        state_row = cur.fetchone()
        endpoint_key = state_row[0] if state_row else "inventory-local"

    # Make the actual inventory HTTP request
    status_code, resp_data = await check_inventory_endpoint(
        endpoint_key, port=port, client=client
    )

    if status_code != 200:
        req_id = uuid4()
        detail = resp_data.get("detail", "Inventory service unavailable")
        record_log(
            conn,
            event_kind="inventory_failed",
            message=f"Inventory check failed for {endpoint_key}: {detail}",
            request_id=req_id,
            severity="ERROR",
            data=resp_data,
        )
        conn.commit()
        error_resp = ApiErrorResponse(
            error=ApiErrorDetail(
                code="inventory_unavailable",
                message=f"Inventory check failed: {detail}",
                request_id=str(req_id),
                retryable=False,
            )
        )
        return None, error_resp, 503

    # Success: create pending order
    req_id = uuid4()
    ord_id = uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sandbox.orders (
                order_id, request_key, request_id, sku, quantity, status, created_at
            )
            VALUES (%s, %s, %s, %s, %s, 'pending', NOW())
            """,
            (ord_id, request_key, req_id, sku, quantity),
        )
        record_log(
            conn,
            event_kind="checkout_accepted",
            message=f"Order accepted and placed in pending state (key={request_key})",
            request_id=req_id,
            order_id=ord_id,
            severity="INFO",
        )
        conn.commit()

    return (
        CheckoutAccepted(
            request_id=req_id,
            order_id=ord_id,
            status="pending",
        ),
        None,
        202,
    )


def get_order(conn: psycopg.Connection, order_id: UUID) -> OrderView | None:
    """Read back order details from sandbox.orders."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT order_id, request_id, status, created_at, fulfilled_at
            FROM sandbox.orders
            WHERE order_id = %s
            """,
            (order_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return OrderView(
            order_id=row[0],
            request_id=row[1],
            status=row[2],
            created_at=row[3],
            fulfilled_at=row[4],
        )


def get_diagnostics_health(conn: psycopg.Connection) -> DiagnosticsHealth:
    """Check process/database reachability and target revision."""
    with conn.cursor() as cur:
        cur.execute("SELECT revision FROM sandbox.target_state WHERE id = 1")
        row = cur.fetchone()
        rev = row[0] if row else 1
    return DiagnosticsHealth(
        status="ok",
        timestamp=datetime.now(UTC),
        target_revision=rev,
        database_reachable=True,
    )


def get_diagnostics_config(conn: psycopg.Connection) -> DiagnosticsConfig:
    """Return sanitized operational configuration without secrets or scenario identifiers."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT endpoint_key, worker_id, worker_running, revision
            FROM sandbox.target_state
            WHERE id = 1
            """
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("Target state row not found")
        return DiagnosticsConfig(
            endpoint_key=row[0],
            worker_id=row[1],
            worker_running=row[2],
            target_revision=row[3],
        )


def get_diagnostics_logs(
    conn: psycopg.Connection,
    request_id: UUID | None = None,
    limit: int = 40,
) -> list[LogEntry]:
    """Retrieve ordered recent structured logs up to limit."""
    limit = max(1, min(limit, 40))
    with conn.cursor() as cur:
        if request_id is not None:
            cur.execute(
                """
                SELECT seq, timestamp, request_id, order_id, event_kind, severity, message, data
                FROM sandbox.logs
                WHERE request_id = %s
                ORDER BY seq ASC
                LIMIT %s
                """,
                (request_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT seq, timestamp, request_id, order_id, event_kind, severity, message, data
                FROM sandbox.logs
                ORDER BY seq ASC
                LIMIT %s
                """,
                (limit,),
            )
        rows = cur.fetchall()

    entries: list[LogEntry] = []
    for r in rows:
        data_dict = r[7] if isinstance(r[7], dict) else (json.loads(r[7]) if r[7] else None)
        entries.append(
            LogEntry(
                seq=r[0],
                timestamp=r[1],
                request_id=r[2],
                order_id=r[3],
                event_kind=r[4],
                severity=r[5],
                message=r[6],
                data=data_dict,
                truncated=bool(data_dict.get("truncated", False)) if data_dict else False,
            )
        )
    return entries


def apply_repair(
    conn: psycopg.Connection,
    repair_req: RepairRequest,
) -> tuple[RepairReceipt, int]:
    """Atomically evaluate expected revision, execute repair mutation, and persist receipt."""
    with conn.cursor() as cur:
        # Lock target state row
        cur.execute(
            """
            SELECT revision, endpoint_key, worker_running
            FROM sandbox.target_state
            WHERE id = 1
            FOR UPDATE
            """
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("Target state row 1 missing")
        current_rev = row[0]

        # Check existing receipt for idempotency
        cur.execute(
            """
            SELECT payload_digest, result, before_revision, after_revision, recorded_at
            FROM sandbox.repair_receipts
            WHERE execution_key = %s
            """,
            (repair_req.execution_key,),
        )
        receipt_row = cur.fetchone()
        if receipt_row:
            if receipt_row[0] != repair_req.payload_digest:
                # 409 conflict
                raise ValueError("execution_key_conflict")
            return (
                RepairReceipt(
                    execution_key=repair_req.execution_key,
                    payload_digest=receipt_row[0],
                    result=receipt_row[1],
                    before_revision=receipt_row[2],
                    after_revision=receipt_row[3],
                    recorded_at=receipt_row[4],
                ),
                200,
            )

        # Evaluate precondition
        if current_rev == repair_req.expected_revision:
            if repair_req.payload.action == "set_inventory_endpoint":
                cur.execute(
                    """
                    UPDATE sandbox.target_state
                    SET endpoint_key = 'inventory-local', revision = revision + 1
                    WHERE id = 1
                    """
                )
            elif repair_req.payload.action == "resume_order_worker":
                cur.execute(
                    """
                    UPDATE sandbox.target_state
                    SET worker_running = TRUE, revision = revision + 1
                    WHERE id = 1
                    """
                )
            result = "applied"
            after_rev = current_rev + 1
        else:
            result = "precondition_failed"
            after_rev = current_rev

        recorded_at = datetime.now(UTC)
        cur.execute(
            """
            INSERT INTO sandbox.repair_receipts (
                execution_key, payload_digest, result, before_revision, after_revision, recorded_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                repair_req.execution_key,
                repair_req.payload_digest,
                result,
                current_rev,
                after_rev,
                recorded_at,
            ),
        )
        record_log(
            conn,
            event_kind="repair_executed",
            message=f"Repair action {repair_req.payload.action} executed with result {result}",
            severity="INFO",
            data={
                "action": repair_req.payload.action,
                "result": result,
                "before_revision": current_rev,
                "after_revision": after_rev,
            },
        )
        conn.commit()

        receipt = RepairReceipt(
            execution_key=repair_req.execution_key,
            payload_digest=repair_req.payload_digest,
            result=result,
            before_revision=current_rev,
            after_revision=after_rev,
            recorded_at=recorded_at,
        )
        return receipt, 200


def get_repair_receipt(
    conn: psycopg.Connection,
    execution_key: UUID,
) -> RepairReceipt | None:
    """Retrieve durable repair receipt by execution key."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload_digest, result, before_revision, after_revision, recorded_at
            FROM sandbox.repair_receipts
            WHERE execution_key = %s
            """,
            (execution_key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return RepairReceipt(
            execution_key=execution_key,
            payload_digest=row[0],
            result=row[1],
            before_revision=row[2],
            after_revision=row[3],
            recorded_at=row[4],
        )


def reset_sandbox(conn: psycopg.Connection) -> int:
    """Reset sandbox to healthy baseline.

    Sets default config, advances revision, clears orders/logs, and keeps receipts.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sandbox.target_state
            SET endpoint_key = 'inventory-local',
                worker_running = TRUE,
                worker_id = 'checkout-worker',
                maintenance = FALSE,
                revision = revision + 1
            WHERE id = 1
            RETURNING revision
            """
        )
        new_rev = cur.fetchone()[0]
        cur.execute("DELETE FROM sandbox.orders")
        cur.execute("DELETE FROM sandbox.logs")
        conn.commit()
        return new_rev


def inject_fault(
    conn: psycopg.Connection,
    family: str,
    variant_seed: int = 1,
) -> int:
    """Apply one selected fault family and advance target revision."""
    with conn.cursor() as cur:
        if family == "inventory_config":
            cur.execute(
                """
                UPDATE sandbox.target_state
                SET endpoint_key = 'inventory-legacy', revision = revision + 1
                WHERE id = 1
                RETURNING revision
                """
            )
        elif family == "worker_paused":
            cur.execute(
                """
                UPDATE sandbox.target_state
                SET worker_running = FALSE, revision = revision + 1
                WHERE id = 1
                RETURNING revision
                """
            )
        elif family == "upstream_maintenance":
            cur.execute(
                """
                UPDATE sandbox.target_state
                SET maintenance = TRUE, revision = revision + 1
                WHERE id = 1
                RETURNING revision
                """
            )
        else:
            raise ValueError(f"Unknown fault family: {family}")

        new_rev = cur.fetchone()[0]
        conn.commit()
        return new_rev
