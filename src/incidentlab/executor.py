"""Executor-owned fixed HTTP operations for sandbox checkout, order polling, and repairs."""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

import httpx

from incidentlab.config import Settings
from incidentlab.sandbox.schemas import CheckoutAccepted, OrderView


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
