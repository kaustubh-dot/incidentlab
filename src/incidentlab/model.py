"""Ollama native API adapter, token accounting, and structured output handling."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import ollama

from incidentlab.config import Settings


class ModelError(Exception):
    """Base error for model invocations."""


class ProviderUnavailableError(ModelError):
    """Raised when the Ollama provider is unreachable or returns connection failure."""


class ModelTimeoutError(ModelError):
    """Raised when the model call exceeds its configured timeout deadline."""


class InvalidModelOutputError(ModelError):
    """Raised when the model output fails schema validation."""


@dataclass(frozen=True)
class ValidatedToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class ModelResult:
    tool_calls: list[ValidatedToolCall]
    diagnosis: dict[str, Any] | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int
    raw_content: str | None = None
    load_duration_ms: int | None = None
    prompt_eval_duration_ms: int | None = None
    eval_duration_ms: int | None = None
    tokens_per_second: float | None = None



# Lazy CPU-only tokenizer cache
_TOKENIZER_CACHE: dict[str, Any] = {}


def get_cpu_tokenizer(model_name: str = "qwen3:8b"):
    """Get or initialize CPU-only tokenizer for request accounting."""
    if model_name not in _TOKENIZER_CACHE:
        try:
            from transformers import AutoTokenizer

            # Use Qwen/Qwen2.5-7B-Instruct or fallback tokenizer if offline/unavailable
            tokenizer = AutoTokenizer.from_pretrained(
                "Qwen/Qwen2.5-7B-Instruct", local_files_only=True
            )
            _TOKENIZER_CACHE[model_name] = tokenizer
        except Exception:
            _TOKENIZER_CACHE[model_name] = None
    return _TOKENIZER_CACHE[model_name]


def count_request_tokens(messages: list[dict[str, Any]], model_name: str = "qwen3:8b") -> int:
    """Accurately count tokens for the assembled messages using tokenizer or byte-level estimate."""
    tokenizer = get_cpu_tokenizer(model_name)
    if tokenizer is not None:
        try:
            text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            return len(tokenizer.encode(text))
        except Exception:
            pass

    # Fallback accounting: formatted JSON length / 3.5 (~4 chars per token plus chat overhead)
    serialized = json.dumps(messages, ensure_ascii=False)
    # Chat format markers add ~4 tokens per message
    overhead = len(messages) * 4 + 3
    estimated = max(1, int(len(serialized) / 3.5) + overhead)
    return estimated


async def call_model(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    final_schema: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> ModelResult:
    """Execute native Ollama chat request with explicit timeout and accounting."""
    if settings is None:
        settings = Settings.from_env()

    client = ollama.AsyncClient(
        host=settings.il_ollama_base_url,
        timeout=float(settings.il_model_timeout_seconds),
    )

    # Qualified bounded options: think=false disables Qwen thinking; bound output tokens
    options = {
        "num_predict": settings.il_max_output_tokens,
        "num_ctx": 8192,
        "think": False,
    }

    start_time = time.monotonic()
    try:
        kwargs: dict[str, Any] = {
            "model": settings.il_model,
            "messages": messages,
            "options": options,
            "think": False,
        }
        if tools and not final_schema:
            kwargs["tools"] = tools
        if final_schema:
            kwargs["format"] = final_schema

        response = await client.chat(**kwargs)
    except httpx.TimeoutException as exc:
        msg = f"Model call timed out after {settings.il_model_timeout_seconds}s"
        raise ModelTimeoutError(msg) from exc
    except (
        httpx.ConnectError,
        httpx.HTTPError,
        ollama.ResponseError,
        ConnectionRefusedError,
    ) as exc:
        msg = f"Ollama provider unavailable at {settings.il_ollama_base_url}: {exc}"
        raise ProviderUnavailableError(msg) from exc
    except Exception as exc:
        # Check for timeout or connect errors wrapped in other exceptions
        err_msg = str(exc).lower()
        if "timeout" in err_msg:
            raise ModelTimeoutError(f"Model call timed out: {exc}") from exc
        raise ProviderUnavailableError(f"Model invocation failed: {exc}") from exc

    duration_ms = int((time.monotonic() - start_time) * 1000)

    # Extract tool calls
    validated_calls: list[ValidatedToolCall] = []
    message = response.get("message", {})
    raw_tool_calls = message.get("tool_calls") or []
    for call in raw_tool_calls:
        fn = call.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        validated_calls.append(ValidatedToolCall(name=name, args=args))

    # Extract structured diagnosis if schema mode
    diagnosis: dict[str, Any] | None = None
    raw_content = message.get("content")
    if final_schema and raw_content:
        clean_content = raw_content.strip()
        if clean_content.startswith("```"):
            clean_content = re.sub(r"^```[a-zA-Z]*\n?", "", clean_content)
            clean_content = re.sub(r"\n?```$", "", clean_content).strip()
        try:
            diagnosis = json.loads(clean_content)
        except Exception:
            start_idx = clean_content.find("{")
            end_idx = clean_content.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                try:
                    diagnosis = json.loads(clean_content[start_idx : end_idx + 1])
                except Exception as exc:
                    msg = f"Failed to parse structured model output as JSON: {exc}"
                    raise InvalidModelOutputError(msg) from exc
            else:
                msg = f"Failed to parse structured model output as JSON: {clean_content[:200]}"
                raise InvalidModelOutputError(msg)

    input_tokens = response.get("prompt_eval_count")
    output_tokens = response.get("eval_count")

    load_duration_ns = response.get("load_duration")
    prompt_eval_duration_ns = response.get("prompt_eval_duration")
    eval_duration_ns = response.get("eval_duration")

    load_duration_ms = int(load_duration_ns / 1_000_000) if load_duration_ns is not None else None
    prompt_eval_duration_ms = (
        int(prompt_eval_duration_ns / 1_000_000) if prompt_eval_duration_ns is not None else None
    )
    eval_duration_ms = int(eval_duration_ns / 1_000_000) if eval_duration_ns is not None else None
    tokens_per_second = (
        round(output_tokens / (eval_duration_ns / 1e9), 2)
        if output_tokens and eval_duration_ns and eval_duration_ns > 0
        else None
    )

    return ModelResult(
        tool_calls=validated_calls,
        diagnosis=diagnosis,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
        raw_content=raw_content,
        load_duration_ms=load_duration_ms,
        prompt_eval_duration_ms=prompt_eval_duration_ms,
        eval_duration_ms=eval_duration_ms,
        tokens_per_second=tokens_per_second,
    )
