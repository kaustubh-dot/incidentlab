"""IncidentLab command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incidentlab.config import Settings
from incidentlab.model import count_request_tokens


def run_command_probe(cmd: list[str]) -> dict[str, Any]:
    """Execute a system probe command safely and return status and output."""
    binary_path = shutil.which(cmd[0])
    if binary_path is None:
        return {
            "available": False,
            "exit_code": -1,
            "error": f"Command not found: {cmd[0]}",
        }
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {
            "available": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as exc:
        return {
            "available": False,
            "exit_code": -1,
            "error": str(exc),
        }


def probe_memory() -> dict[str, Any]:
    """Read system memory information."""
    mem_info: dict[str, Any] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    if key in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"):
                        mem_info[key] = val
    except Exception as exc:
        mem_info["error"] = str(exc)
    return mem_info


async def probe_ollama(settings: Settings) -> dict[str, Any]:
    """Probe Ollama server and candidate model capabilities."""
    import httpx

    cli_path = shutil.which("ollama") or str(Path.home() / ".local" / "bin" / "ollama")
    cli_exists = Path(cli_path).exists() if cli_path else False

    ollama_info: dict[str, Any] = {
        "configured_base_url": settings.il_ollama_base_url,
        "cli_binary": cli_path if cli_exists else None,
        "cli_available": cli_exists,
        "reachable": False,
        "active_url": None,
        "version": None,
        "candidate_model": settings.il_model,
        "model_found": False,
        "model_details": None,
        "native_tools_supported": False,
        "schema_mode_supported": False,
        "thinking_mode_disabled_supported": False,
        "error": None,
    }

    # Try configured URL, then localhost if configured URL hostname fails
    candidate_urls = [settings.il_ollama_base_url]
    if (
        "127.0.0.1:11434" not in settings.il_ollama_base_url
        and "localhost:11434" not in settings.il_ollama_base_url
    ):
        candidate_urls.append("http://127.0.0.1:11434")

    for url in candidate_urls:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(f"{url}/api/version")
                if res.status_code == 200:
                    ollama_info["reachable"] = True
                    ollama_info["active_url"] = url
                    ollama_info["version"] = res.json().get("version")

                    # Check tags
                    tags_res = await client.get(f"{url}/api/tags")
                    if tags_res.status_code == 200:
                        models = tags_res.json().get("models", [])
                        for m in models:
                            match_name = m.get("name") == settings.il_model
                            match_model = m.get("model") == settings.il_model
                            if match_name or match_model:
                                ollama_info["model_found"] = True
                                ollama_info["model_details"] = m
                                break
                    break
        except Exception as exc:
            if ollama_info["error"] is None:
                ollama_info["error"] = str(exc)

    if ollama_info["reachable"] and ollama_info["model_found"]:
        from incidentlab.model import call_model

        active_settings = Settings.from_mapping({
            "IL_DATABASE_URL": settings.il_database_url,
            "IL_SANDBOX_DATABASE_URL": settings.il_sandbox_database_url,
            "IL_OWNER_TOKEN": settings.il_owner_token,
            "IL_SANDBOX_READ_TOKEN": settings.il_sandbox_read_token,
            "IL_SANDBOX_WRITE_TOKEN": settings.il_sandbox_write_token,
            "IL_SANDBOX_ADMIN_TOKEN": settings.il_sandbox_admin_token,
            "IL_OLLAMA_BASE_URL": ollama_info["active_url"] or "http://127.0.0.1:11434",
            "IL_MODEL": settings.il_model,
        })
        test_tool = {
            "type": "function",
            "function": {
                "name": "get_health",
                "description": "Probe service health and revision",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        messages = [
            {"role": "user", "content": "Please check the health using get_health tool."}
        ]
        try:
            result = await call_model(
                messages=messages,
                tools=[test_tool],
                final_schema=None,
                settings=active_settings,
            )
            ollama_info["native_tools_supported"] = len(result.tool_calls) > 0
            ollama_info["thinking_mode_disabled_supported"] = True
            ollama_info["sample_tool_calls"] = [
                {"name": tc.name, "args": tc.args} for tc in result.tool_calls
            ]

            # Probe structured schema mode
            schema = {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "decision": {"type": "string", "enum": ["escalate", "propose_repair"]},
                },
                "required": ["summary", "decision"],
            }
            schema_messages = [
                {
                    "role": "user",
                    "content": "Return a JSON object with summary 'OK' and decision 'escalate'.",
                }
            ]
            schema_res = await call_model(
                messages=schema_messages,
                tools=[],
                final_schema=schema,
                settings=active_settings,
            )
            ollama_info["schema_mode_supported"] = (
                schema_res.diagnosis is not None
                and schema_res.diagnosis.get("decision") in ("escalate", "propose_repair")
            )
            ollama_info["sample_diagnosis"] = schema_res.diagnosis
        except Exception as exc:
            ollama_info["tool_probe_error"] = str(exc)

    return ollama_info


def qualify_runtime(output_path: Path | None = None) -> dict[str, Any]:
    """Collect qualification facts and output qualification artifact."""
    sys.stderr.write("Probing runtime and hardware environment...\n")

    # Host & OS
    os_info = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python_version": sys.version,
    }

    # Memory
    mem_info = probe_memory()

    # Docker & Docker Compose
    docker_info = run_command_probe(["docker", "version"])
    compose_info = run_command_probe(["docker", "compose", "version"])

    # GPU & nvidia-smi
    gpu_info = run_command_probe(["nvidia-smi"])

    # Settings & Ollama
    try:
        settings = Settings.from_env()
    except Exception:
        # Fallback with dummy settings for pure probe without secrets
        settings = Settings.from_mapping({
            "IL_DATABASE_URL": "postgresql://incidentlab_control:dummy@postgres:5432/incidentlab",
            "IL_SANDBOX_DATABASE_URL": "postgresql://incidentlab_sandbox:dummy@postgres:5432/incidentlab",
            "IL_OWNER_TOKEN": "0" * 64,
            "IL_SANDBOX_READ_TOKEN": "1" * 64,
            "IL_SANDBOX_WRITE_TOKEN": "2" * 64,
            "IL_SANDBOX_ADMIN_TOKEN": "3" * 64,
        })

    ollama_info = asyncio.run(probe_ollama(settings))

    # Tokenizer qualification
    tokenizer_facts = {
        "model_name": settings.il_model,
        "sample_tokens": count_request_tokens([{"role": "user", "content": "Hello world"}]),
    }

    qualification = {
        "timestamp": datetime.now(UTC).isoformat(),
        "os_info": os_info,
        "memory": mem_info,
        "docker": docker_info,
        "docker_compose": compose_info,
        "gpu": gpu_info,
        "ollama": ollama_info,
        "tokenizer": tokenizer_facts,
    }

    if output_path is None:
        output_path = Path("artifacts/qualification.json")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(qualification, f, indent=2)

    sys.stderr.write(f"Qualification facts written to {output_path}\n")
    return qualification


def main():
    parser = argparse.ArgumentParser(description="IncidentLab CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # qualify command
    qualify_parser = subparsers.add_parser(
        "qualify", help="Probe and qualify hardware, runtime, and model"
    )
    qualify_parser.add_argument("--output", type=Path, default=Path("artifacts/qualification.json"))

    # Stubs and commands for subsequent tasks/phases
    migrate_parser = subparsers.add_parser("migrate", help="Run database migrations (P0)")
    migrate_parser.add_argument("--dsn", type=str, default=None, help="Migration database DSN")

    smoke_parser = subparsers.add_parser("smoke-checkout", help="Run healthy checkout check (P0)")
    smoke_parser.add_argument("--key", type=str, default=None, help="Explicit request key UUID")
    smoke_parser.add_argument(
        "--timeout", type=float, default=10.0, help="Fulfillment timeout in seconds"
    )

    seed_parser = subparsers.add_parser("seed", help="Seed a fault scenario (P1)")
    seed_parser.add_argument(
        "--family",
        type=str,
        default="inventory_config",
        choices=["inventory_config", "worker_paused", "upstream_maintenance"],
        help="Fault family to inject",
    )
    seed_parser.add_argument("--seed", type=int, default=1, help="Variant seed")

    subparsers.add_parser("index-runbooks", help="Index versioned runbooks (P1)")
    subparsers.add_parser("smoke-demo", help="Run interactive demo recovery (P2)")
    subparsers.add_parser("resolve-unknown", help="Resolve unknown execution state (P3)")

    args = parser.parse_args()

    future_cmds = ("smoke-demo", "resolve-unknown")
    if args.command == "qualify":
        result = qualify_runtime(args.output)
        print(json.dumps(result, indent=2))
    elif args.command == "migrate":
        from incidentlab.db import run_migrations

        migration_dsn = args.dsn or os.environ.get("IL_MIGRATION_DATABASE_URL")
        if not migration_dsn:
            sys.stderr.write("Error: IL_MIGRATION_DATABASE_URL environment variable is required.\n")
            sys.exit(1)

        control_pw = os.environ.get("IL_CONTROL_DB_PASSWORD")
        sandbox_pw = os.environ.get("IL_SANDBOX_DB_PASSWORD")

        try:
            sys.stderr.write("Waiting for PostgreSQL database readiness...\n")
            applied = run_migrations(
                migration_dsn=migration_dsn,
                control_password=control_pw,
                sandbox_password=sandbox_pw,
            )
            sys.stderr.write(f"Migrations applied successfully: {applied}\n")
            print(json.dumps({"status": "ok", "applied": applied}, indent=2))
        except Exception as exc:
            sys.stderr.write(f"Migration failed: {exc}\n")
            sys.exit(1)
    elif args.command == "smoke-checkout":
        import time
        from uuid import UUID, uuid4

        from incidentlab.executor import submit_checkout, wait_for_order_fulfillment

        async def run_smoke():
            settings = Settings.from_env()
            req_key = UUID(args.key) if args.key else uuid4()
            sys.stderr.write(f"Submitting checkout with request_key={req_key}...\n")
            t0 = time.monotonic()
            try:
                accepted = await submit_checkout(req_key, settings=settings)
                sys.stderr.write(
                    f"Checkout accepted: request_id={accepted.request_id}, "
                    f"order_id={accepted.order_id}, status={accepted.status}\n"
                )
            except Exception as exc:
                sys.stderr.write(f"Checkout submission failed: {exc}\n")
                return 1

            sys.stderr.write(
                f"Polling order {accepted.order_id} for fulfillment (timeout={args.timeout}s)...\n"
            )
            try:
                order = await wait_for_order_fulfillment(
                    accepted.order_id, settings=settings, timeout_seconds=args.timeout
                )
                elapsed = time.monotonic() - t0
                sys.stderr.write(
                    f"Order {order.order_id} status: '{order.status}' in {elapsed:.2f}s "
                    f"(fulfilled_at={order.fulfilled_at})\n"
                )
                if order.status == "fulfilled":
                    print(
                        json.dumps(
                            {
                                "status": "fulfilled",
                                "request_id": str(accepted.request_id),
                                "order_id": str(order.order_id),
                                "elapsed_seconds": round(elapsed, 3),
                            },
                            indent=2,
                        )
                    )
                    return 0
                else:
                    sys.stderr.write(f"FAILURE: Order status is {order.status}\n")
                    return 1
            except Exception as exc:
                sys.stderr.write(f"Order polling failed: {exc}\n")
                return 1

        sys.exit(asyncio.run(run_smoke()))
    elif args.command == "seed":
        from uuid import uuid4

        import httpx

        from incidentlab.executor import submit_checkout, wait_for_order_fulfillment

        async def run_seed():
            settings = Settings.from_env()
            admin_headers = {"Authorization": f"Bearer {settings.il_sandbox_admin_token}"}

            # 1. Reset sandbox to healthy baseline first
            sys.stderr.write("Resetting sandbox to healthy baseline...\n")
            reset_url = f"{settings.il_sandbox_base_url}/operator/reset"
            async with httpx.AsyncClient(timeout=10.0) as ac:
                reset_resp = await ac.post(reset_url, headers=admin_headers)
                if reset_resp.status_code != 200:
                    sys.stderr.write(f"Pre-seed reset failed: {reset_resp.text}\n")
                    return 1

            # 2. Generate unrelated genuine healthy traffic before injection
            sys.stderr.write("Generating pre-injection healthy checkout traffic...\n")
            healthy_accepted = await submit_checkout(uuid4(), settings=settings)
            await wait_for_order_fulfillment(
                healthy_accepted.order_id, settings=settings, timeout_seconds=10.0
            )
            sys.stderr.write(
                f"Healthy pre-injection order fulfilled: {healthy_accepted.order_id}\n"
            )

            # 3. Inject fault family via operator endpoint
            sys.stderr.write(f"Injecting fault family '{args.family}' (seed={args.seed})...\n")
            inject_url = f"{settings.il_sandbox_base_url}/operator/inject"
            payload = {"family": args.family, "variant_seed": args.seed}
            async with httpx.AsyncClient(timeout=10.0) as ac:
                resp = await ac.post(inject_url, headers=admin_headers, json=payload)
                if resp.status_code != 200:
                    sys.stderr.write(f"Fault injection failed: {resp.status_code} {resp.text}\n")
                    return 1
                data = resp.json()
                print(json.dumps(data, indent=2))
                return 0

        sys.exit(asyncio.run(run_seed()))
    elif args.command == "index-runbooks":
        from incidentlab.db import get_control_connection
        from incidentlab.retrieval import index_runbooks

        settings = Settings.from_env()
        with get_control_connection(settings) as conn:
            runbooks_path = Path("data/runbooks")
            count = index_runbooks(conn, runbooks_dir=runbooks_path)
            sys.stderr.write(f"Indexed {count} runbook sections successfully.\n")
            print(json.dumps({"status": "indexed", "sections_indexed": count}, indent=2))
            sys.exit(0)
    elif args.command in future_cmds:
        sys.stderr.write(
            f"Command '{args.command}' belongs to a later task/phase and is not yet implemented.\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
