"""Configuration settings and credential scoping for IncidentLab."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, Field


class Settings(BaseModel):
    il_bind_host: str = Field(default="127.0.0.1")
    il_port: int = Field(default=8000)
    il_database_url: str
    il_sandbox_database_url: str
    il_migration_database_url: str | None = Field(default=None)
    il_owner_token: str
    il_sandbox_read_token: str
    il_sandbox_write_token: str
    il_sandbox_admin_token: str
    il_sandbox_base_url: str = Field(default="http://sandbox:8001")
    il_ollama_base_url: str = Field(default="http://ollama:11434")
    il_model: str = Field(default="qwen3:8b")
    il_model_timeout_seconds: int = Field(default=60)
    il_active_deadline_seconds: int = Field(default=240)
    il_max_model_turns: int = Field(default=4)
    il_max_read_calls: int = Field(default=6)
    il_max_input_tokens: int = Field(default=6000)
    il_max_output_tokens: int = Field(default=1024)
    il_approval_ttl_seconds: int = Field(default=600)
    il_log_level: str = Field(default="INFO")

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> Settings:
        required_keys = [
            "IL_DATABASE_URL",
            "IL_SANDBOX_DATABASE_URL",
            "IL_OWNER_TOKEN",
            "IL_SANDBOX_READ_TOKEN",
            "IL_SANDBOX_WRITE_TOKEN",
            "IL_SANDBOX_ADMIN_TOKEN",
        ]
        missing = [k for k in required_keys if not values.get(k)]
        if missing:
            raise ValueError(f"Missing required configuration variables: {', '.join(missing)}")

        read_token = values.get("IL_SANDBOX_READ_TOKEN", "")
        write_token = values.get("IL_SANDBOX_WRITE_TOKEN", "")
        admin_token = values.get("IL_SANDBOX_ADMIN_TOKEN", "")
        if len({read_token, write_token, admin_token}) < 3:
            raise ValueError("Sandbox tokens (read, write, admin) must be distinct")

        return cls(
            il_bind_host=values.get("IL_BIND_HOST", "127.0.0.1"),
            il_port=int(values.get("IL_PORT", "8000")),
            il_database_url=values["IL_DATABASE_URL"],
            il_sandbox_database_url=values["IL_SANDBOX_DATABASE_URL"],
            il_migration_database_url=values.get("IL_MIGRATION_DATABASE_URL") or None,
            il_owner_token=values["IL_OWNER_TOKEN"],
            il_sandbox_read_token=read_token,
            il_sandbox_write_token=write_token,
            il_sandbox_admin_token=admin_token,
            il_sandbox_base_url=values.get("IL_SANDBOX_BASE_URL", "http://sandbox:8001"),
            il_ollama_base_url=values.get("IL_OLLAMA_BASE_URL", "http://ollama:11434"),
            il_model=values.get("IL_MODEL", "qwen3:8b"),
            il_model_timeout_seconds=int(values.get("IL_MODEL_TIMEOUT_SECONDS", "60")),
            il_active_deadline_seconds=int(values.get("IL_ACTIVE_DEADLINE_SECONDS", "240")),
            il_max_model_turns=int(values.get("IL_MAX_MODEL_TURNS", "4")),
            il_max_read_calls=int(values.get("IL_MAX_READ_CALLS", "6")),
            il_max_input_tokens=int(values.get("IL_MAX_INPUT_TOKENS", "6000")),
            il_max_output_tokens=int(values.get("IL_MAX_OUTPUT_TOKENS", "1024")),
            il_approval_ttl_seconds=int(values.get("IL_APPROVAL_TTL_SECONDS", "600")),
            il_log_level=values.get("IL_LOG_LEVEL", "INFO"),
        )

    @classmethod
    def from_env(cls) -> Settings:
        return cls.from_mapping(os.environ)


def build_mcp_environment(settings: Settings) -> dict[str, str]:
    """Construct minimal explicit environment for the read-only MCP stdio child process."""
    env = {
        "IL_SANDBOX_READ_TOKEN": settings.il_sandbox_read_token,
        "IL_SANDBOX_BASE_URL": settings.il_sandbox_base_url,
        "IL_LOG_LEVEL": settings.il_log_level,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return env
