"""Sandbox order fulfillment worker loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import psycopg

logger = logging.getLogger(__name__)


def worker_tick(conn: psycopg.Connection) -> int:
    """Execute a single atomic worker fulfillment pass.

    Returns the number of orders transitioned from pending to fulfilled.
    """
    with conn.cursor() as cur:
        # Check if worker is running according to current target state
        cur.execute("SELECT worker_running FROM sandbox.target_state WHERE id = 1")
        row = cur.fetchone()
        if not row or not row[0]:
            return 0

        # Lock pending orders
        cur.execute(
            """
            SELECT order_id, request_id
            FROM sandbox.orders
            WHERE status = 'pending'
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 20
            """
        )
        pending = cur.fetchall()
        if not pending:
            return 0

        fulfilled_count = 0
        for order_id, request_id in pending:
            cur.execute(
                """
                UPDATE sandbox.orders
                SET status = 'fulfilled', fulfilled_at = NOW()
                WHERE order_id = %s
                """,
                (order_id,),
            )
            cur.execute(
                """
                INSERT INTO sandbox.logs (
                    timestamp, request_id, order_id, event_kind, severity, message
                )
                VALUES (
                    NOW(), %s, %s, 'order_fulfilled', 'INFO',
                    'Order fulfilled successfully by worker'
                )
                """,
                (request_id, order_id),
            )
            fulfilled_count += 1

        conn.commit()
        return fulfilled_count


async def run_worker_loop(
    get_conn: Callable[[], psycopg.Connection],
    shutdown_event: asyncio.Event,
    interval_seconds: float = 0.250,
) -> None:
    """Continuously execute worker fulfillment ticks every interval until shutdown."""
    logger.info("Sandbox worker loop started (interval=%.3fs)", interval_seconds)
    while not shutdown_event.is_set():
        try:
            with get_conn() as conn:
                worker_tick(conn)
        except Exception as exc:
            logger.error("Error during worker tick: %s", exc)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
    logger.info("Sandbox worker loop stopped")
