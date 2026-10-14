"""MCP client transport and secure tool caller for IncidentLab."""

from __future__ import annotations

import json
import logging
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from mcp import ClientSession, StdioServerParameters, stdio_client
from psycopg.types.json import Jsonb

from incidentlab.config import Settings, build_mcp_environment
from incidentlab.db import get_control_connection
from incidentlab.schemas import EvidenceItem, EvidenceKind

logger = logging.getLogger("incidentlab.mcp_client")

ALLOWED_READ_TOOLS = {"get_health", "get_logs", "get_config", "probe_checkout"}

TOOL_KIND_MAP: dict[str, EvidenceKind] = {
    "get_health": "health",
    "get_logs": "logs",
    "get_config": "config",
    "probe_checkout": "probe",
}


class McpSession:
    """Manages the lifecycle of a dedicated stdio MCP child process session."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: dict[str, Any] = {}

    async def __aenter__(self) -> McpSession:
        self._exit_stack = AsyncExitStack()
        try:
            child_env = build_mcp_environment(self.settings)
            server_params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "incidentlab.mcp_server"],
                env=child_env,
            )
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()

            tools_res = await self._session.list_tools()
            self._tools = {t.name: t for t in tools_res.tools}
            if set(self._tools.keys()) != ALLOWED_READ_TOOLS:
                got = set(self._tools.keys())
                raise RuntimeError(
                    f"MCP server tools mismatch: got {got}, expected {ALLOWED_READ_TOOLS}"
                )
            return self
        except Exception:
            if self._exit_stack:
                await self._exit_stack.aclose()
            raise

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    async def list_tools(self) -> list[str]:
        """Return list of discovered allowable tool names."""
        return list(self._tools.keys())

    async def call_read_tool(
        self,
        incident_id: UUID,
        name: str,
        args: dict[str, Any],
    ) -> EvidenceItem:
        """Validate scope, invoke read-only MCP tool, sanitize and persist evidence."""
        if name not in ALLOWED_READ_TOOLS:
            raise ValueError(f"Disallowed tool: {name}")

        if not self._session:
            raise RuntimeError("McpSession is not active")

        # Sanitize arguments
        sanitized_args: dict[str, Any] = {}
        if name == "get_logs":
            if "request_id" in args and args["request_id"] is not None:
                sanitized_args["request_id"] = str(args["request_id"])
            if "limit" in args and args["limit"] is not None:
                sanitized_args["limit"] = max(1, min(int(args["limit"]), 40))
        elif name == "probe_checkout":
            if "order_id" in args and args["order_id"] is not None:
                sanitized_args["order_id"] = str(args["order_id"])

        res = await self._session.call_tool(name, sanitized_args)
        if getattr(res, "is_error", False):
            raise RuntimeError(f"MCP tool {name} invocation failed: {res.content}")

        # Parse text content
        parsed_data: dict[str, Any] = {}
        for content_item in res.content:
            if getattr(content_item, "type", "") == "text" or hasattr(content_item, "text"):
                text = getattr(content_item, "text", "")
                try:
                    val = json.loads(text)
                    if isinstance(val, dict):
                        parsed_data.update(val)
                    else:
                        parsed_data["result"] = val
                except Exception:
                    parsed_data["raw"] = text

        kind = TOOL_KIND_MAP[name]
        ev_id = f"ev_{name}_{uuid4().hex[:8]}"
        observed_at = datetime.now(UTC)

        evidence = EvidenceItem(
            evidence_id=ev_id,
            kind=kind,
            observed_at=observed_at,
            source=name,
            source_version=None,
            section_id=None,
            data=parsed_data,
        )

        # Persist to control.evidence if incident exists in DB
        try:
            with get_control_connection(self.settings) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM control.incidents WHERE id = %s", (incident_id,))
                    if cur.fetchone():
                        cur.execute(
                            """
                            INSERT INTO control.evidence (
                                incident_id, evidence_id, kind, source,
                                source_version, section_id, observed_at, data
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (incident_id, evidence_id) DO NOTHING
                            """,
                            (
                                incident_id,
                                evidence.evidence_id,
                                evidence.kind,
                                evidence.source,
                                evidence.source_version,
                                evidence.section_id,
                                evidence.observed_at,
                                Jsonb(evidence.data),
                            ),
                        )
                        conn.commit()
        except Exception as exc:
            logger.debug("Evidence not persisted to control DB (safe to ignore if test): %s", exc)

        return evidence
