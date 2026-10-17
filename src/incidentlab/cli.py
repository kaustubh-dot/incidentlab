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


async def resolve_unknown_cli(
    incident_id_str: str,
    note: str | None = None,
    acknowledge: str | None = None,
    confirm_quiesced: bool = False,
    settings: Settings | None = None,
    client: Any | None = None,
) -> int:
    """Implement operator procedure for narrow unknown-outcome resolution per ARCHITECTURE.md.

    1. Requires locally configured owner credential and control DB authority.
    2. Prints incident/proposal/execution binding and last dispatch state.
    3. Performs fresh read-only target and receipt lookup; records observations as evidence.
    4. If durable receipt found: routes to existing outcome (never closes as unknown).
    5. If target or receipt lookup fails/unreachable: fails visibly and leaves block.
    6. For reachable target with unresolved history:
       Requires owner confirmation of quiesce, nonempty inspection note,
       and exact typed acknowledgment 'ACKNOWLEDGE UNKNOWN <execution-key>'.
       Commits 'closed_unknown' under row lock atomically with event 'execution.closed_unknown'.
       Execution row stays 'unknown' for audit; terminal incident releases one-active-incident lock.
       Zero repair POST replay, zero mutation, zero verification-success declaration.
    """
    from uuid import UUID, uuid4

    import httpx
    from psycopg.types.json import Jsonb

    from incidentlab.db import get_control_connection
    from incidentlab.events import record_event
    from incidentlab.executor import reconcile_execution

    if settings is None:
        settings = Settings.from_env()

    try:
        incident_id = UUID(incident_id_str)
    except Exception as exc:
        sys.stderr.write(f"Error: Invalid incident UUID '{incident_id_str}': {exc}\n")
        return 1

    # 1. Inspect control DB under row lock
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, state, symptom
                FROM control.incidents
                WHERE id = %s
                FOR UPDATE
                """,
                (incident_id,),
            )
            inc_row = cur.fetchone()
            if not inc_row:
                sys.stderr.write(f"Error: Incident {incident_id} not found.\n")
                return 1

            inc_state, symptom = inc_row[1], inc_row[2]
            terminal_states = {
                "verified_recovered",
                "verification_failed",
                "escalated",
                "failed",
                "rejected",
                "expired",
                "cancelled",
                "closed_unknown",
            }
            if inc_state in terminal_states:
                sys.stderr.write(
                    f"Error: Incident {incident_id} is already in terminal state '{inc_state}'.\n"
                )
                return 1

            # Get latest execution
            cur.execute(
                """
                SELECT e.execution_key, e.proposal_id, e.state, e.payload_digest,
                       e.expected_revision, e.claimed_at, e.dispatched_at, e.receipt,
                       p.payload, p.target_revision
                FROM control.executions e
                JOIN control.proposals p ON e.proposal_id = p.id
                WHERE e.incident_id = %s
                ORDER BY e.claimed_at DESC
                LIMIT 1
                """,
                (incident_id,),
            )
            exec_row = cur.fetchone()
            if not exec_row:
                sys.stderr.write(
                    f"Error: No execution found for incident {incident_id}.\n"
                )
                return 1

            (
                exec_key,
                prop_id,
                exec_state,
                payload_digest,
                expected_rev,
                claimed_at,
                dispatched_at,
                receipt_data,
                payload_data,
                prop_rev,
            ) = exec_row

    # 2. Print binding and dispatch state
    print("=" * 60)
    print("INCIDENTLAB OPERATOR UNKNOWN RESOLUTION")
    print("=" * 60)
    print(f"Incident ID:       {incident_id} (state: {inc_state})")
    print(f"Symptom:           {symptom}")
    print(f"Proposal ID:       {prop_id}")
    print(f"Execution Key:     {exec_key}")
    print(f"Execution State:   {exec_state}")
    print(f"Expected Revision: {expected_rev}")
    print(f"Payload Digest:    {payload_digest}")
    print(f"Payload:           {json.dumps(payload_data)}")
    print(f"Claimed At:        {claimed_at}")
    print(f"Dispatched At:     {dispatched_at}")
    print("=" * 60)

    # 3. Fresh read-only inspection of sandbox target
    health_url = f"{settings.il_sandbox_base_url}/diagnostics/health"
    config_url = f"{settings.il_sandbox_base_url}/diagnostics/config"
    diag_headers = {"Authorization": f"Bearer {settings.il_sandbox_read_token}"}

    try:
        if client is not None:
            h_resp = await client.get(health_url, headers=diag_headers, timeout=5.0)
            c_resp = await client.get(config_url, headers=diag_headers, timeout=5.0)
        else:
            async with httpx.AsyncClient(timeout=5.0) as ac:
                h_resp = await ac.get(health_url, headers=diag_headers)
                c_resp = await ac.get(config_url, headers=diag_headers)

        if h_resp.status_code != 200 or c_resp.status_code != 200:
            sys.stderr.write(
                f"ERROR: Sandbox diagnostics unavailable (health: {h_resp.status_code}, "
                f"config: {c_resp.status_code}). Leaving incident unresolved and blocking.\n"
            )
            return 1
        health_data = h_resp.json()
        config_data = c_resp.json()
    except Exception as exc:
        sys.stderr.write(
            f"ERROR: Sandbox target unreachable: {exc}. "
            "Leaving incident unresolved and blocking new incidents.\n"
        )
        return 1

    print("Current Sandbox Target State:")
    print(f"  Target Revision:    {health_data.get('target_revision')}")
    print(f"  Inventory Endpoint: {config_data.get('inventory_endpoint_key')}")
    print(f"  Worker Running:     {config_data.get('worker_running')}")

    # 4. Fresh read-only receipt lookup
    repair_url = f"{settings.il_sandbox_base_url}/repairs/{exec_key}"
    repair_headers = {"Authorization": f"Bearer {settings.il_sandbox_write_token}"}
    try:
        if client is not None:
            r_resp = await client.get(repair_url, headers=repair_headers, timeout=5.0)
        else:
            async with httpx.AsyncClient(timeout=5.0) as ac:
                r_resp = await ac.get(repair_url, headers=repair_headers)
    except Exception as exc:
        sys.stderr.write(
            f"ERROR: Sandbox receipt lookup unreachable: {exc}. "
            "Leaving incident unresolved and blocking new incidents.\n"
        )
        return 1

    if r_resp.status_code not in (200, 404):
        sys.stderr.write(
            f"ERROR: Sandbox receipt lookup failed with HTTP {r_resp.status_code}: {r_resp.text}. "
            "Leaving incident unresolved and blocking new incidents.\n"
        )
        return 1

    # Record observation as evidence
    ev_id = f"ev:unknown_inspect:{uuid4().hex[:8]}"
    obs_data = {
        "target_health": health_data,
        "target_config": config_data,
        "receipt_lookup_status": r_resp.status_code,
    }
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control.evidence (
                    incident_id, evidence_id, kind, source, source_version,
                    section_id, observed_at, data
                ) VALUES (
                    %s, %s, 'target_observation', 'diagnostics', 'v1',
                    'resolve-unknown', NOW(), %s
                )
                """,
                (str(incident_id), ev_id, Jsonb(obs_data)),
            )
        conn.commit()

    # 5. Route durable receipt if found
    if r_resp.status_code == 200:
        print(f"\nDurable receipt found in sandbox for {exec_key}: {r_resp.text}")
        print("Routing to existing reconciliation outcome (not closing as unknown)...")
        recon_view = await reconcile_execution(exec_key, settings=settings, client=client)
        print(f"Execution reconciled to state: {recon_view.state}")
        return 0

    # 6. Unresolved history: require operator confirmations
    print(f"\nNo durable receipt found in sandbox for execution key {exec_key} (HTTP 404).")
    print("Target is reachable, but historical effect remains unresolved.")

    if not confirm_quiesced:
        try:
            q_prompt = "Confirm that control/sandbox were quiesced per ARCHITECTURE.md (yes/no): "
            quiesce_in = input(q_prompt).strip()
        except (EOFError, OSError):
            quiesce_in = "no"
        if quiesce_in.lower() != "yes":
            sys.stderr.write("Aborted: Operator did not confirm completed quiesce procedure.\n")
            return 1

    if note is not None and not note.strip():
        sys.stderr.write("Aborted: Nonempty inspection note is required.\n")
        return 1

    if not note or not note.strip():
        try:
            note = input("Enter operator inspection note (nonempty): ").strip()
        except (EOFError, OSError):
            note = ""
        if not note or not note.strip():
            sys.stderr.write("Aborted: Nonempty inspection note is required.\n")
            return 1

    expected_ack = f"ACKNOWLEDGE UNKNOWN {exec_key}"
    if not acknowledge:
        try:
            acknowledge = input(f"Type exactly '{expected_ack}' to confirm: ").strip()
        except (EOFError, OSError):
            acknowledge = ""

    if acknowledge != expected_ack:
        sys.stderr.write(
            f"Aborted: Acknowledgment '{acknowledge}' did not match required '{expected_ack}'.\n"
        )
        return 1

    # 7. Commit closed_unknown atomically under row lock
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            # Recheck under lock
            cur.execute(
                "SELECT state FROM control.incidents WHERE id = %s FOR UPDATE",
                (incident_id,),
            )
            cur_state = cur.fetchone()[0]
            if cur_state in terminal_states:
                sys.stderr.write(
                    f"Error: Incident {incident_id} changed to terminal state '{cur_state}'.\n"
                )
                return 1

            outcome_data = {
                "effect_unknown": True,
                "note": note,
                "owner_id": "owner",
                "timestamp": datetime.now(UTC).isoformat(),
                "evidence_ids": [ev_id],
                "execution_key": str(exec_key),
            }
            cur.execute(
                """
                UPDATE control.incidents
                SET state = 'closed_unknown',
                    updated_at = NOW(),
                    outcome = %s
                WHERE id = %s
                """,
                (Jsonb(outcome_data), incident_id),
            )
            # Execution row state stays 'unknown' for audit
            cur.execute(
                """
                UPDATE control.executions
                SET state = 'unknown'
                WHERE execution_key = %s
                """,
                (exec_key,),
            )
            record_event(
                conn,
                incident_id,
                "execution.closed_unknown",
                {
                    "execution_key": str(exec_key),
                    "owner_id": "owner",
                    "note": note,
                    "evidence_ids": [ev_id],
                    "effect_unknown": True,
                },
            )
        conn.commit()

    print(f"\nSUCCESS: Incident {incident_id} closed as closed_unknown.")
    print("Outcome recorded with effect_unknown=true and audit event 'execution.closed_unknown'.")
    print("One-active-incident constraint released.")
    return 0


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
    smoke_demo_parser = subparsers.add_parser(
        "smoke-demo", help="Run interactive demo recovery (P2)"
    )
    smoke_demo_parser.add_argument(
        "--family",
        type=str,
        default="inventory_config",
        choices=["inventory_config", "worker_paused", "upstream_maintenance"],
        help="Fault family to demonstrate",
    )
    smoke_demo_parser.add_argument(
        "--approval",
        type=str,
        default="interactive",
        choices=["interactive", "approve", "reject"],
        help="Approval mode: interactive, approve, or reject",
    )
    resolve_unknown_parser = subparsers.add_parser(
        "resolve-unknown", help="Resolve unknown execution state (P3)"
    )
    resolve_unknown_parser.add_argument(
        "--incident",
        type=str,
        required=True,
        help="Incident UUID to resolve",
    )
    resolve_unknown_parser.add_argument(
        "--note",
        type=str,
        default=None,
        help="Nonempty inspection note",
    )
    resolve_unknown_parser.add_argument(
        "--acknowledge",
        type=str,
        default=None,
        help="Exact confirmation string: ACKNOWLEDGE UNKNOWN <execution-key>",
    )
    resolve_unknown_parser.add_argument(
        "--confirm-quiesced",
        action="store_true",
        default=False,
        help="Confirm that control/sandbox were stopped/restarted per ARCHITECTURE.md",
    )

    args = parser.parse_args()

    future_cmds: tuple[str, ...] = ()
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
        if not control_pw and os.environ.get("IL_DATABASE_URL"):
            from urllib.parse import urlparse

            control_pw = urlparse(os.environ["IL_DATABASE_URL"]).password
        if not sandbox_pw and os.environ.get("IL_SANDBOX_DATABASE_URL"):
            from urllib.parse import urlparse

            sandbox_pw = urlparse(os.environ["IL_SANDBOX_DATABASE_URL"]).password


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
    elif args.command == "smoke-demo":
        from uuid import UUID, uuid4

        import httpx

        from incidentlab.approvals import claim_execution, decide_proposal
        from incidentlab.db import get_control_connection
        from incidentlab.executor import (
            CheckoutError,
            execute_claim,
            submit_checkout,
            wait_for_order_fulfillment,
        )
        from incidentlab.graph import run_investigation
        from incidentlab.schemas import ApprovalDecision
        from incidentlab.verifier import verify_checkout

        async def run_smoke_demo():
            settings = Settings.from_env()
            admin_headers = {"Authorization": f"Bearer {settings.il_sandbox_admin_token}"}

            # 1. Reset target to clean state
            sys.stderr.write("1. Resetting sandbox to healthy baseline...\n")
            async with httpx.AsyncClient(timeout=10.0) as ac:
                r = await ac.post(
                    f"{settings.il_sandbox_base_url}/operator/reset",
                    headers=admin_headers,
                )
                if r.status_code != 200:
                    sys.stderr.write(f"Reset failed: {r.text}\n")
                    return 1

            # Check for active nonterminal incidents in control DB (IL-AUD-004)
            with get_control_connection(settings) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, state
                        FROM control.incidents
                        WHERE state NOT IN (
                            'verified_recovered', 'verification_failed', 'escalated',
                            'failed', 'rejected', 'expired', 'cancelled', 'closed_unknown', 'stale'
                        )
                        LIMIT 1
                        """
                    )
                    active_inc = cur.fetchone()
                    if active_inc:
                        sys.stderr.write(
                            f"Active non-terminal incident exists "
                            f"({active_inc[0]}, state='{active_inc[1]}'). "
                            "smoke-demo fails closed. Resolve or close active incident first.\n"
                        )
                        return 1

            # 2. Inject fault family
            sys.stderr.write(f"2. Injecting fault family '{args.family}'...\n")
            async with httpx.AsyncClient(timeout=10.0) as ac:
                inj_resp = await ac.post(
                    f"{settings.il_sandbox_base_url}/operator/inject",
                    headers=admin_headers,
                    json={"family": args.family, "variant_seed": 1},
                )
                if inj_resp.status_code != 200:
                    sys.stderr.write(f"Inject failed: {inj_resp.text}\n")
                    return 1

            # 3. Trigger failing checkout or worker stall to observe real failure
            failing_req_id = uuid4()
            failing_order_id = None
            if args.family == "worker_paused":
                sys.stderr.write("3. Probing checkout and worker fulfillment...\n")
                accepted = await submit_checkout(uuid4(), settings=settings)
                failing_req_id = accepted.request_id
                failing_order_id = accepted.order_id
                order_view = await wait_for_order_fulfillment(
                    accepted.order_id, settings=settings, timeout_seconds=1.5
                )
                if order_view.status == "fulfilled":
                    sys.stderr.write(
                        "ERROR: Worker was expected to be paused, but order was fulfilled!\n"
                    )
                    return 1
                sys.stderr.write(
                    f"Observed worker stall: order {failing_order_id} "
                    f"remains in status '{order_view.status}'\n"
                )
                symptom_text = (
                    "Checkout accepted order but fulfillment timed out: order remains pending"
                )
            else:
                sys.stderr.write("3. Probing checkout to observe genuine failure...\n")
                try:
                    await submit_checkout(uuid4(), settings=settings)
                    sys.stderr.write("ERROR: Checkout unexpectedly succeeded!\n")
                    return 1
                except CheckoutError as chk_err:
                    sys.stderr.write(
                        f"Observed checkout failure: {chk_err.code} - {chk_err.message}\n"
                    )
                    if chk_err.request_id:
                        try:
                            failing_req_id = UUID(chk_err.request_id)
                        except Exception:
                            pass
                    symptom_text = f"Checkout failed: {chk_err.message}"

            # 4. Start incident and run investigation
            sys.stderr.write("4. Launching bounded agent investigation...\n")
            incident_id = uuid4()
            with get_control_connection(settings) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO control.incidents (id, state, symptom, request_id, order_id)
                        VALUES (%s, 'investigating', %s, %s, %s)
                        """,
                        (
                            incident_id,
                            symptom_text,
                            failing_req_id,
                            failing_order_id,
                        ),
                    )
                conn.commit()

            investigation_state = await run_investigation(
                incident_id, settings=settings
            )
            sys.stderr.write(
                f"Investigation complete. Phase: {investigation_state.get('phase')}\n"
            )

            # Check if investigation led to escalation (e.g. upstream maintenance)
            if investigation_state.get("phase") == "escalated":
                diag = investigation_state.get("diagnosis", {})
                print("\n================== INCIDENT ESCALATED ==================")
                print(f"Incident ID:      {incident_id}")
                print("State:            escalated")
                print(f"Summary:          {diag.get('summary')}")
                print(f"Uncertainty:      {diag.get('uncertainty')}")
                print("Repair:           None (External upstream condition - zero repair receipts)")
                print("========================================================\n")
                print(
                    f"SUCCESS: Incident {incident_id} escalated without repair action "
                    "(zero repair receipts)."
                )
                return 0

            # 5. Read proposed action
            with get_control_connection(settings) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, payload, payload_digest, target_revision, expires_at
                        FROM control.proposals
                        WHERE incident_id = %s
                        """,
                        (incident_id,),
                    )
                    prop_row = cur.fetchone()
                    if not prop_row:
                        sys.stderr.write(
                            "No proposal created during investigation.\n"
                        )
                        return 1

            prop_id, payload, digest, target_rev, expires_at = prop_row
            if not isinstance(payload, dict):
                payload = json.loads(payload)

            print("\n================== PROPOSED REPAIR ==================")
            print(f"Incident ID:      {incident_id}")
            print(f"Proposal ID:      {prop_id}")
            print(f"Action:           {payload.get('action')}")
            if "endpoint_key" in payload:
                print(f"Endpoint Key:     {payload.get('endpoint_key')}")
            if "worker_id" in payload:
                print(f"Worker ID:        {payload.get('worker_id')}")
            print(f"Target Revision:  {target_rev}")
            print(f"Payload Digest:   {digest}")
            print(f"Expires At:       {expires_at}")
            print("=====================================================\n")

            # 6. Approval decision
            decision_val = args.approval
            if decision_val == "interactive":
                prompt_ans = (
                    input("Approve repair proposal? [y/N]: ").strip().lower()
                )
                decision_val = "approve" if prompt_ans in ("y", "yes") else "reject"

            sys.stderr.write(f"5. Submitting decision: '{decision_val}'...\n")
            decision_payload = ApprovalDecision(
                proposal_id=prop_id,
                payload_digest=digest,
                target_revision=target_rev,
                decision=decision_val,
            )
            approval_view = await decide_proposal(
                incident_id=incident_id,
                owner_id="owner",
                decision=decision_payload,
                settings=settings,
            )
            print(f"Approval recorded: {approval_view.decision}")

            if decision_val == "reject":
                print(
                    f"Proposal rejected. Incident {incident_id} state is 'rejected'. "
                    "Zero repair claims created."
                )
                return 0

            # 7. Claim execution
            sys.stderr.write("6. Claiming atomic execution...\n")
            claim = await claim_execution(
                incident_id=incident_id,
                proposal_id=prop_id,
                settings=settings,
            )
            print(f"Execution claimed: key={claim.execution_key}")

            # 8. Execute claim
            sys.stderr.write("7. Dispatching finite repair to sandbox...\n")
            receipt = await execute_claim(claim, settings=settings)
            print(
                f"Repair receipt: result={receipt.result}, "
                f"before_revision={receipt.before_revision}, "
                f"after_revision={receipt.after_revision}"
            )

            if receipt.result != "applied":
                sys.stderr.write(
                    f"Repair precondition failed: {receipt.result}\n"
                )
                return 1

            # 9. Independent verification
            sys.stderr.write("8. Running independent verification checkout...\n")
            verif = await verify_checkout(
                incident_id, uuid4(), settings=settings
            )
            print(
                f"Verification: verdict={verif.verdict}, "
                f"order_id={verif.order_id}, "
                f"status={verif.observed_status}"
            )

            if verif.verdict == "pass":
                print(
                    f"\nSUCCESS: Incident {incident_id} recovered to verified_recovered!"
                )
                return 0
            else:
                print(
                    f"\nFAILURE: Incident verification failed: {verif.observed_status}"
                )
                return 1

        sys.exit(asyncio.run(run_smoke_demo()))
    elif args.command == "resolve-unknown":
        rc = asyncio.run(
            resolve_unknown_cli(
                incident_id_str=args.incident,
                note=args.note,
                acknowledge=args.acknowledge,
                confirm_quiesced=args.confirm_quiesced,
            )
        )
        sys.exit(rc)
    elif args.command in future_cmds:
        sys.stderr.write(
            f"Command '{args.command}' belongs to a later task/phase and is not yet implemented.\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
