"""Tests for sandbox fake checkout, order fulfillment, diagnostics, and owner demo routes."""

from __future__ import annotations

import asyncio
import time
from uuid import UUID, uuid4

import httpx
import pytest

from incidentlab.config import Settings
from incidentlab.db import get_sandbox_connection
from incidentlab.executor import submit_checkout, wait_for_order_fulfillment
from incidentlab.sandbox.schemas import CheckoutAccepted, OrderView
from incidentlab.sandbox.worker import worker_tick


@pytest.fixture
def live_settings():
    """Load settings from current environment (used in integration runs)."""
    return Settings.from_env()


@pytest.fixture
def sandbox_client(live_settings):
    """Async client configured for the sandbox service."""
    class SandboxTestClient:
        def __init__(self, base_url: str, write_token: str, read_token: str, admin_token: str):
            self.base_url = base_url
            self.write_token = write_token
            self.read_token = read_token
            self.admin_token = admin_token

        async def checkout(self, request_key: UUID) -> CheckoutAccepted:
            url = f"{self.base_url}/checkout"
            headers = {"Authorization": f"Bearer {self.write_token}"}
            async with httpx.AsyncClient(timeout=10.0) as ac:
                resp = await ac.post(
                    url,
                    headers=headers,
                    json={"request_key": str(request_key), "sku": "SKU-1", "quantity": 1},
                )
                assert resp.status_code == 202, f"Checkout failed: {resp.text}"
                data = resp.json()
                return CheckoutAccepted(
                    request_id=UUID(data["request_id"]),
                    order_id=UUID(data["order_id"]),
                    status=data["status"],
                )

        async def get_order(self, order_id: UUID) -> OrderView:
            url = f"{self.base_url}/orders/{order_id}"
            headers = {"Authorization": f"Bearer {self.read_token}"}
            async with httpx.AsyncClient(timeout=5.0) as ac:
                resp = await ac.get(url, headers=headers)
                assert resp.status_code == 200, f"Get order failed: {resp.text}"
                return OrderView.model_validate(resp.json())

        async def reset(self) -> int:
            url = f"{self.base_url}/operator/reset"
            headers = {"Authorization": f"Bearer {self.admin_token}"}
            async with httpx.AsyncClient(timeout=10.0) as ac:
                resp = await ac.post(url, headers=headers)
                assert resp.status_code == 200, f"Reset failed: {resp.text}"
                return resp.json()["revision"]

    return SandboxTestClient(
        base_url=live_settings.il_sandbox_base_url,
        write_token=live_settings.il_sandbox_write_token,
        read_token=live_settings.il_sandbox_read_token,
        admin_token=live_settings.il_sandbox_admin_token,
    )


@pytest.fixture
def wait_for_order(sandbox_client):
    async def _wait(order_id: UUID, timeout_seconds: float = 10.0) -> OrderView:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            order = await sandbox_client.get_order(order_id)
            if order.status == "fulfilled":
                return order
            await asyncio.sleep(0.25)
        raise TimeoutError(f"Order {order_id} did not reach fulfilled within {timeout_seconds}s")

    return _wait


# --- Integration Tests (require live sandbox & postgres) ---


@pytest.mark.integration
async def test_actual_worker_fulfills_order(sandbox_client, wait_for_order):
    """Verify healthy fake checkout creates an order that the background worker fulfills."""
    await sandbox_client.reset()
    key = uuid4()
    created = await sandbox_client.checkout(request_key=key)
    assert created.status == "pending"

    order = await wait_for_order(created.order_id, timeout_seconds=10)
    assert order.status == "fulfilled"
    assert order.request_id == created.request_id
    assert order.fulfilled_at is not None


@pytest.mark.integration
async def test_checkout_idempotency(sandbox_client):
    """Submitting checkout with identical request_key returns identical order."""
    await sandbox_client.reset()
    key = uuid4()
    first = await sandbox_client.checkout(request_key=key)
    second = await sandbox_client.checkout(request_key=key)

    assert first.order_id == second.order_id
    assert first.request_id == second.request_id


@pytest.mark.integration
async def test_diagnostics_endpoints(sandbox_client, live_settings):
    """Verify diagnostics endpoints return valid data without secrets or scenario labels."""
    headers = {"Authorization": f"Bearer {live_settings.il_sandbox_read_token}"}
    base = live_settings.il_sandbox_base_url

    async with httpx.AsyncClient(timeout=5.0) as ac:
        # Health
        resp = await ac.get(f"{base}/diagnostics/health", headers=headers)
        assert resp.status_code == 200
        health = resp.json()
        assert health["status"] == "ok"
        assert health["database_reachable"] is True

        # Config
        resp = await ac.get(f"{base}/diagnostics/config", headers=headers)
        assert resp.status_code == 200
        cfg = resp.json()
        assert cfg["endpoint_key"] == "inventory-local"
        assert cfg["worker_id"] == "checkout-worker"
        assert cfg["worker_running"] is True
        assert "password" not in str(cfg).lower()
        assert "token" not in str(cfg).lower()

        # Logs
        resp = await ac.get(f"{base}/diagnostics/logs", headers=headers)
        assert resp.status_code == 200
        logs = resp.json()
        assert isinstance(logs, list)

        # Probe
        resp = await ac.get(f"{base}/diagnostics/probe", headers=headers)
        assert resp.status_code == 200
        probe = resp.json()
        assert probe["inventory_status"] == 200
        assert probe["inventory_reachable"] is True


@pytest.mark.integration
async def test_sandbox_auth_boundaries(live_settings):
    """Verify unauthenticated or invalid tokens are rejected with 401."""
    base = live_settings.il_sandbox_base_url

    async with httpx.AsyncClient(timeout=5.0) as ac:
        # Missing auth
        resp = await ac.post(f"{base}/checkout", json={"request_key": str(uuid4())})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

        # Wrong token on write route
        resp = await ac.post(
            f"{base}/checkout",
            headers={"Authorization": f"Bearer {live_settings.il_sandbox_read_token}"},
            json={"request_key": str(uuid4())},
        )
        assert resp.status_code == 401

        # Wrong token on admin route
        resp = await ac.post(
            f"{base}/operator/reset",
            headers={"Authorization": f"Bearer {live_settings.il_sandbox_read_token}"},
        )
        assert resp.status_code == 401


@pytest.mark.integration
async def test_owner_api_demo_checkout(live_settings):
    """Verify owner control API /api/v1/demo/checkout forwards to executor and fulfills."""
    from httpx import ASGITransport

    from incidentlab.api import create_app

    app = create_app(live_settings)
    headers = {"Authorization": f"Bearer {live_settings.il_owner_token}"}
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        reset_resp = await ac.post("/api/v1/demo/reset", headers=headers)
        assert reset_resp.status_code == 200
        assert reset_resp.json()["status"] == "reset"

        checkout_resp = await ac.post(
            "/api/v1/demo/checkout",
            headers=headers,
            json={"request_key": str(uuid4())},
        )
        assert checkout_resp.status_code == 200
        data = checkout_resp.json()
        assert data["status"] == "fulfilled"
        assert data["order_id"] is not None


@pytest.mark.integration
async def test_executor_helper_direct(live_settings):
    """Verify executor submit_checkout and wait_for_order_fulfillment functions directly."""
    key = uuid4()
    accepted = await submit_checkout(key, settings=live_settings)
    assert accepted.status == "pending"

    order = await wait_for_order_fulfillment(
        accepted.order_id, settings=live_settings, timeout_seconds=10.0
    )
    assert order.status == "fulfilled"
    assert order.request_id == accepted.request_id


# --- Unit / DB Level Tests ---


def test_worker_tick_paused(live_settings):
    """When worker_running is false in target_state, worker_tick fulfills 0 orders."""
    dsn = live_settings.il_sandbox_database_url
    with get_sandbox_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Temporarily pause worker
            cur.execute("UPDATE sandbox.target_state SET worker_running = FALSE WHERE id = 1")
            conn.commit()

            # Insert a pending order
            oid = uuid4()
            req_id = uuid4()
            rkey = uuid4()
            cur.execute(
                """
                INSERT INTO sandbox.orders (
                    order_id, request_key, request_id, sku, quantity, status, created_at
                )
                VALUES (%s, %s, %s, 'SKU-1', 1, 'pending', NOW())
                """,
                (oid, rkey, req_id),
            )
            conn.commit()

            # Execute tick
            fulfilled = worker_tick(conn)
            assert fulfilled == 0

            # Verify order is still pending
            cur.execute("SELECT status FROM sandbox.orders WHERE order_id = %s", (oid,))
            assert cur.fetchone()[0] == "pending"

            # Resume worker
            cur.execute("UPDATE sandbox.target_state SET worker_running = TRUE WHERE id = 1")
            conn.commit()

            # Execute tick again
            fulfilled_after = worker_tick(conn)
            assert fulfilled_after >= 1

            # Verify order is fulfilled
            cur.execute("SELECT status FROM sandbox.orders WHERE order_id = %s", (oid,))
            assert cur.fetchone()[0] == "fulfilled"
