"""Tests for read-only MCP server, client transport, and security boundaries."""

from __future__ import annotations

import pytest

from incidentlab.config import Settings, build_mcp_environment


def test_mcp_child_has_read_authority_only():
    """Verify child environment receives read token only and excludes write/admin/db secrets."""
    settings = Settings.from_mapping(
        {
            "IL_DATABASE_URL": "postgresql://incidentlab_control:secret1@127.0.0.1:5432/incidentlab",
            "IL_SANDBOX_DATABASE_URL": "postgresql://incidentlab_sandbox:secret2@127.0.0.1:5432/incidentlab",
            "IL_OWNER_TOKEN": "a" * 64,
            "IL_SANDBOX_READ_TOKEN": "b" * 64,
            "IL_SANDBOX_WRITE_TOKEN": "c" * 64,
            "IL_SANDBOX_ADMIN_TOKEN": "d" * 64,
        }
    )
    child_env = build_mcp_environment(settings)
    assert "IL_SANDBOX_READ_TOKEN" in child_env
    assert "IL_SANDBOX_BASE_URL" in child_env
    # Critical PRD IL-04 / IL-11 assertion: child receives read token only
    forbidden = {
        "IL_SANDBOX_WRITE_TOKEN",
        "IL_SANDBOX_ADMIN_TOKEN",
        "IL_DATABASE_URL",
        "IL_OWNER_TOKEN",
    }
    leaked = forbidden & child_env.keys()
    assert not leaked, f"Forbidden keys leaked to MCP child: {leaked}"


def test_mcp_server_exact_four_tools():
    """Verify MCP server registers exactly the four allowable architecture tools."""
    from incidentlab.mcp_server import list_registered_tools

    tools = list_registered_tools()
    expected = {"get_health", "get_logs", "get_config", "probe_checkout"}
    assert set(tools.keys()) == expected, f"Tools mismatch: got {set(tools.keys())}"


@pytest.mark.integration
async def test_mcp_stdio_client_live_session(live_settings):
    """Verify genuine stdio MCP client session connects and queries sandbox tools."""
    from uuid import uuid4

    from incidentlab.mcp_client import McpSession

    incident_id = uuid4()
    async with McpSession(live_settings) as session:
        # Check tools list
        tools = await session.list_tools()
        assert set(tools) == {"get_health", "get_logs", "get_config", "probe_checkout"}

        # Call get_health
        health_ev = await session.call_read_tool(
            incident_id=incident_id,
            name="get_health",
            args={},
        )
        assert health_ev.kind == "health"
        assert health_ev.source == "get_health"
        assert health_ev.data["status"] == "ok"

        # Call get_config
        config_ev = await session.call_read_tool(
            incident_id=incident_id,
            name="get_config",
            args={},
        )
        assert config_ev.kind == "config"
        assert "endpoint_key" in config_ev.data

        # Reject invalid tool name
        with pytest.raises(ValueError, match="Disallowed tool"):
            await session.call_read_tool(
                incident_id=incident_id,
                name="arbitrary_bash",  # type: ignore[arg-type]
                args={},
            )


def test_mcp_tools_prevent_arbitrary_path_or_command_selection():
    """Verify tool parameter schemas cannot select arbitrary URLs, paths, or commands."""
    from incidentlab.mcp_server import list_registered_tools

    tools = list_registered_tools()
    for name, tool in tools.items():
        schema = tool.parameters if hasattr(tool, "parameters") else {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        forbidden = {"url", "path", "file", "cmd", "command", "exec", "script", "host"}
        assert not (forbidden & set(props.keys())), f"Tool {name} has forbidden parameters"


