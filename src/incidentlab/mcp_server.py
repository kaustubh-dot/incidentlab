"""Read-only Model Context Protocol (MCP) server for sandbox observation."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

logger = logging.getLogger("incidentlab.mcp_server")

server = MCPServer("incidentlab-reader")


async def _sandbox_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Execute authenticated GET request to sandbox diagnostics with retry on transient errors."""
    base_url = os.environ.get("IL_SANDBOX_BASE_URL", "http://sandbox:8001").rstrip("/")
    token = os.environ.get("IL_SANDBOX_READ_TOKEN", "")
    url = f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {token}"}

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == 0 and (
                status in (502, 503, 504)
                or isinstance(exc, (httpx.ConnectError, httpx.TimeoutException))
            ):
                logger.warning("Transient error on %s (attempt 1): %s, retrying...", path, exc)
                await asyncio.sleep(0.1)
                continue
            logger.error("Request to %s failed: %s", path, exc)
            raise


@server.tool()
async def get_health() -> dict[str, Any]:
    """Retrieve validated health evidence and target revision from sandbox."""
    res = await _sandbox_get("/diagnostics/health")
    return res if isinstance(res, dict) else {"status": "ok", "raw": res}


@server.tool()
async def get_logs(request_id: str | None = None, limit: int = 40) -> dict[str, Any]:
    """Retrieve bounded diagnostic logs from sandbox up to 40 entries."""
    bounded_limit = max(1, min(limit, 40))
    params: dict[str, Any] = {"limit": bounded_limit}
    if request_id:
        params["request_id"] = str(request_id)
    res = await _sandbox_get("/diagnostics/logs", params=params)
    return {"logs": res} if isinstance(res, list) else res


@server.tool()
async def get_config() -> dict[str, Any]:
    """Retrieve sanitized endpoint and worker configuration from sandbox."""
    res = await _sandbox_get("/diagnostics/config")
    return res if isinstance(res, dict) else {"config": res}


@server.tool()
async def probe_checkout(order_id: str | None = None) -> dict[str, Any]:
    """Perform read-only dependency probe and order inspection."""
    params: dict[str, Any] = {}
    if order_id:
        params["order_id"] = str(order_id)
    res = await _sandbox_get("/diagnostics/probe", params=params)
    return res if isinstance(res, dict) else {"probe": res}


def list_registered_tools() -> dict[str, Any]:
    """Return dictionary of registered tools for schema and security verification."""
    return {t.name: t for t in server._tool_manager.list_tools()}


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("IL_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(server.run_stdio_async())
