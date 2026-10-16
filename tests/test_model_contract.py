
import pytest

from incidentlab.config import Settings, build_mcp_environment
from incidentlab.model import (
    ModelResult,
    ModelTimeoutError,
    ProviderUnavailableError,
    ValidatedToolCall,
    call_model,
    count_request_tokens,
)


@pytest.fixture
def valid_settings_mapping():
    return {
        "IL_BIND_HOST": "127.0.0.1",
        "IL_PORT": "8000",
        "IL_DATABASE_URL": "postgresql://incidentlab_control:secret1@postgres:5432/incidentlab",
        "IL_SANDBOX_DATABASE_URL": "postgresql://incidentlab_sandbox:secret2@postgres:5432/incidentlab",
        "IL_MIGRATION_DATABASE_URL": "postgresql://incidentlab_migrator:secret3@postgres:5432/incidentlab",
        "IL_OWNER_TOKEN": "a" * 64,
        "IL_SANDBOX_READ_TOKEN": "b" * 64,
        "IL_SANDBOX_WRITE_TOKEN": "c" * 64,
        "IL_SANDBOX_ADMIN_TOKEN": "d" * 64,
        "IL_SANDBOX_BASE_URL": "http://sandbox:8001",
        "IL_OLLAMA_BASE_URL": "http://ollama:11434",
        "IL_MODEL": "qwen3:8b",
        "IL_MODEL_TIMEOUT_SECONDS": "60",
        "IL_ACTIVE_DEADLINE_SECONDS": "240",
        "IL_MAX_MODEL_TURNS": "4",
        "IL_MAX_READ_CALLS": "6",
        "IL_MAX_INPUT_TOKENS": "6000",
        "IL_MAX_OUTPUT_TOKENS": "1024",
        "IL_APPROVAL_TTL_SECONDS": "600",
        "IL_LOG_LEVEL": "INFO",
    }


def test_required_owner_secret_is_not_defaulted(valid_settings_mapping):
    valid_settings_mapping["IL_OWNER_TOKEN"] = ""
    with pytest.raises(ValueError, match="IL_OWNER_TOKEN"):
        Settings.from_mapping(valid_settings_mapping)


def test_required_database_urls_cannot_be_empty(valid_settings_mapping):
    valid_settings_mapping["IL_DATABASE_URL"] = ""
    with pytest.raises(ValueError, match="IL_DATABASE_URL"):
        Settings.from_mapping(valid_settings_mapping)

    valid_settings_mapping["IL_DATABASE_URL"] = "postgresql://control@localhost/db"
    valid_settings_mapping["IL_SANDBOX_DATABASE_URL"] = ""
    with pytest.raises(ValueError, match="IL_SANDBOX_DATABASE_URL"):
        Settings.from_mapping(valid_settings_mapping)


def test_distinct_tokens_enforced(valid_settings_mapping):
    # Same read and write token must fail
    read_tok = valid_settings_mapping["IL_SANDBOX_READ_TOKEN"]
    valid_settings_mapping["IL_SANDBOX_WRITE_TOKEN"] = read_tok
    with pytest.raises(ValueError, match="distinct"):
        Settings.from_mapping(valid_settings_mapping)


def test_missing_secret_error_does_not_leak_other_secrets(valid_settings_mapping):
    valid_settings_mapping["IL_OWNER_TOKEN"] = ""
    secret_marker = "SUPER_SECRET_VALUE_12345"
    valid_settings_mapping["IL_SANDBOX_READ_TOKEN"] = secret_marker
    try:
        Settings.from_mapping(valid_settings_mapping)
        pytest.fail("Should have raised ValueError")
    except ValueError as exc:
        assert secret_marker not in str(exc)
        assert "IL_OWNER_TOKEN" in str(exc)


def test_mcp_environment_scoped_subset(valid_settings_mapping):
    settings = Settings.from_mapping(valid_settings_mapping)
    env = build_mcp_environment(settings)

    assert "IL_SANDBOX_READ_TOKEN" in env
    assert env["IL_SANDBOX_READ_TOKEN"] == settings.il_sandbox_read_token
    assert "IL_SANDBOX_BASE_URL" in env
    assert "IL_LOG_LEVEL" in env

    # Sensitive control/write credentials must be completely excluded
    disallowed = {
        "IL_SANDBOX_WRITE_TOKEN",
        "IL_SANDBOX_ADMIN_TOKEN",
        "IL_DATABASE_URL",
        "IL_SANDBOX_DATABASE_URL",
        "IL_MIGRATION_DATABASE_URL",
        "IL_OWNER_TOKEN",
    }
    for key in disallowed:
        assert key not in env, f"Forbidden key {key} leaked to MCP environment"


def test_model_result_schema():
    call = ValidatedToolCall(name="get_health", args={})
    assert call.name == "get_health"
    assert call.args == {}

    result = ModelResult(
        tool_calls=[call],
        diagnosis=None,
        input_tokens=150,
        output_tokens=42,
        duration_ms=250,
        raw_content=None,
    )
    assert len(result.tool_calls) == 1
    assert result.input_tokens == 150
    assert result.output_tokens == 42


def test_model_result_timing_fields():
    result = ModelResult(
        tool_calls=[],
        diagnosis=None,
        input_tokens=100,
        output_tokens=50,
        duration_ms=200,
        load_duration_ms=10,
        prompt_eval_duration_ms=40,
        eval_duration_ms=150,
        tokens_per_second=333.33,
    )
    assert result.tokens_per_second == 333.33
    assert result.eval_duration_ms == 150



def test_tokenizer_counting_under_budget():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello world!"},
    ]
    tokens = count_request_tokens(messages)
    assert isinstance(tokens, int)
    assert 0 < tokens < 6000


@pytest.mark.asyncio
async def test_call_model_timeout_or_error_handling(valid_settings_mapping):
    settings = Settings.from_mapping(valid_settings_mapping)
    # Point to an unreachable port with short timeout
    settings.il_ollama_base_url = "http://127.0.0.1:1"
    settings.il_model_timeout_seconds = 1

    messages = [{"role": "user", "content": "ping"}]
    with pytest.raises((ProviderUnavailableError, ModelTimeoutError)):
        await call_model(messages=messages, tools=[], final_schema=None, settings=settings)
