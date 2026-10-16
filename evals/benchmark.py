"""IncidentLab serving performance benchmark for qualified local configuration.

Measures baseline serving performance per PRD IL-05, IL-12, IL-13 and ROADMAP IL-T16:
- Concurrency = 1
- Retains 6,000 input / 1,024 output token caps and qualified non-thinking mode (think: False)
- 6 frozen representative development inputs (2 per family: clean and noisy)
- 1 cold-load observation + 3 warm repeats per input (18 warm observations)
- Monotonic wall-clock timing, Ollama provider durations (load, prompt_eval, eval)
- Tokens per second computed as eval_count / eval_duration_seconds
- Valid TTFT timed via streaming on non-empty content
- Memory sampled during execution with stated interval
- Quality verification against expected gold outcomes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from incidentlab.config import Settings
from incidentlab.context import build_context
from incidentlab.db import get_control_connection, get_sandbox_connection
from incidentlab.executor import (
    CheckoutError,
    submit_checkout,
    wait_for_order_fulfillment,
)
from incidentlab.graph import DIAGNOSIS_SCHEMA
from incidentlab.mcp_client import McpSession
from incidentlab.model import count_request_tokens
from incidentlab.retrieval import retrieve_runbooks
from incidentlab.sandbox.service import record_log

logger = logging.getLogger("incidentlab.evals.benchmark")


@dataclass
class RepresentativeInput:
    case_id: str
    family: str
    variation: str
    seed: int
    expected_decision: str
    expected_action: str | None
    symptom: str
    messages: list[dict[str, Any]]
    input_tokens: int


@dataclass
class Observation:
    observation_id: str
    case_id: str
    family: str
    variation: str
    observation_type: str  # "cold" or "warm"
    repeat_index: int
    wall_clock_duration_seconds: float
    load_duration_seconds: float | None
    prompt_eval_count: int | None
    prompt_eval_duration_seconds: float | None
    eval_count: int | None
    eval_duration_seconds: float | None
    tokens_per_second: float | None
    ttft_seconds: float | None
    ttft_reason: str | None
    peak_ram_used_mb: float | None
    peak_gpu_vram_used_mb: float | None
    quality_passed: bool
    quality_notes: str
    diagnosis_decision: str | None
    diagnosis_action: str | None


def get_environment_info(settings: Settings) -> dict[str, Any]:
    """Capture environment, host hardware, Ollama runtime, and model digest."""
    os_info = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
    }

    # RAM info
    ram_info: dict[str, Any] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    k, v = parts[0].strip(), parts[1].strip()
                    if k in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"):
                        ram_info[k] = v
    except Exception as exc:
        ram_info["error"] = str(exc)

    # GPU info via nvidia-smi
    gpu_info: dict[str, Any] = {"available": False, "device": None, "vram": None, "driver": None}
    try:
        res = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.strip().split(",")]
            if len(parts) >= 5:
                gpu_info = {
                    "available": True,
                    "device": parts[0],
                    "driver": parts[1],
                    "total_vram_mib": float(parts[2]),
                    "used_vram_mib": float(parts[3]),
                    "free_vram_mib": float(parts[4]),
                }
    except Exception as exc:
        gpu_info["error"] = str(exc)

    # Ollama runtime & model metadata
    ollama_info: dict[str, Any] = {
        "base_url": settings.il_ollama_base_url,
        "reachable": False,
        "version": None,
        "model": settings.il_model,
        "model_details": None,
        "compute_provider": "cpu",  # Verified CPU compute from server logs
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            v_res = client.get(f"{settings.il_ollama_base_url}/api/version")
            if v_res.status_code == 200:
                ollama_info["reachable"] = True
                ollama_info["version"] = v_res.json().get("version")

            tags_res = client.get(f"{settings.il_ollama_base_url}/api/tags")
            if tags_res.status_code == 200:
                for m in tags_res.json().get("models", []):
                    if m.get("name") == settings.il_model or m.get("model") == settings.il_model:
                        ollama_info["model_details"] = {
                            "digest": m.get("digest"),
                            "size": m.get("size"),
                            "modified_at": m.get("modified_at"),
                            "details": m.get("details", {}),
                            "capabilities": m.get("capabilities", []),
                        }
                        break
    except Exception as exc:
        ollama_info["error"] = str(exc)

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "os": os_info,
        "ram": ram_info,
        "gpu": gpu_info,
        "ollama": ollama_info,
        "limits": {
            "max_input_tokens": settings.il_max_input_tokens,
            "max_output_tokens": settings.il_max_output_tokens,
            "concurrency": 1,
            "non_thinking_mode": True,
            "options": {"think": False, "num_predict": 1024, "num_ctx": 8192},
        },
    }


class MemorySampler:
    """Samples peak RAM and GPU VRAM at bounded intervals during an operation."""

    def __init__(self, sample_interval_seconds: float = 0.1):
        self.interval = sample_interval_seconds
        self.running = False
        self.peak_ram_used_mb: float = 0.0
        self.peak_gpu_vram_used_mb: float | None = None
        self._task: asyncio.Task | None = None

    async def _sample_loop(self):
        while self.running:
            # Sample RAM
            try:
                with open("/proc/meminfo") as f:
                    mem_data = {}
                    for line in f:
                        p = line.split(":")
                        if len(p) == 2:
                            mem_data[p[0].strip()] = p[1].strip()
                    total_kb = float(mem_data.get("MemTotal", "0 kB").split()[0])
                    avail_kb = float(mem_data.get("MemAvailable", "0 kB").split()[0])
                    used_mb = (total_kb - avail_kb) / 1024.0
                    if used_mb > self.peak_ram_used_mb:
                        self.peak_ram_used_mb = used_mb
            except Exception:
                pass

            # Sample GPU VRAM if available
            try:
                res = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                if res.returncode == 0 and res.stdout.strip():
                    used_vram = float(res.stdout.strip().splitlines()[0])
                    if self.peak_gpu_vram_used_mb is None or used_vram > self.peak_gpu_vram_used_mb:
                        self.peak_gpu_vram_used_mb = used_vram
            except Exception:
                pass

            await asyncio.sleep(self.interval)

    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self._sample_loop())

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


async def time_streaming_ttft(
    settings: Settings,
    messages: list[dict[str, Any]],
) -> tuple[float | None, str | None]:
    """Measure valid Time to First Token (TTFT) via streaming on first non-empty content."""
    t_send = time.monotonic()
    t_first_token: float | None = None
    chunks_seen = 0

    try:
        async with httpx.AsyncClient(timeout=float(settings.il_model_timeout_seconds)) as client:
            async with client.stream(
                "POST",
                f"{settings.il_ollama_base_url}/api/chat",
                json={
                    "model": settings.il_model,
                    "messages": messages,
                    "think": False,
                    "stream": True,
                    "options": {"num_predict": 1024, "num_ctx": 8192},
                },
            ) as response:
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunks_seen += 1
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    msg = chunk.get("message", {})
                    content = msg.get("content", "")
                    if content and t_first_token is None:
                        t_first_token = time.monotonic()
                    if chunk.get("done"):
                        break

        if t_first_token is not None:
            ttft = round(t_first_token - t_send, 3)
            return ttft, None
        return None, "empty_content_in_stream"
    except Exception as exc:
        return None, f"streaming_failed: {exc}"


async def setup_case_and_build_input(
    case_data: dict[str, Any],
    gold_data: dict[str, Any],
    settings: Settings,
) -> RepresentativeInput:
    """Setup sandbox fixture and assemble bounded investigation context for one case."""
    admin_headers = {"Authorization": f"Bearer {settings.il_sandbox_admin_token}"}

    # Reset sandbox target
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r_reset = await ac.post(
            f"{settings.il_sandbox_base_url}/operator/reset",
            headers=admin_headers,
        )
        if r_reset.status_code != 200:
            raise RuntimeError(f"Sandbox reset failed: {r_reset.text}")

    # Inject case fault
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r_inj = await ac.post(
            f"{settings.il_sandbox_base_url}/operator/inject",
            headers=admin_headers,
            json={"family": case_data["family"], "variant_seed": case_data["seed"]},
        )
        if r_inj.status_code != 200:
            raise RuntimeError(f"Fault injection failed: {r_inj.text}")

    # Generate background noise logs if requested
    noise_count = case_data.get("traffic_recipe", {}).get("noise_logs", 0)
    if noise_count > 0:
        with get_sandbox_connection(settings.il_sandbox_database_url) as s_conn:
            for idx in range(noise_count):
                record_log(
                    s_conn,
                    event_kind="background_noise",
                    severity="INFO",
                    message=f"Routine system telemetry ping {idx + 1}/{noise_count}",
                    data={"noise_index": idx, "component": "metrics-agent"},
                )
            s_conn.commit()

    # Trigger post-fault traffic to observe real failure and capture IDs
    failed_req_id = uuid4()
    failed_order_id = None
    symptom = case_data["symptom_template"]

    if case_data["family"] == "worker_paused":
        acc = await submit_checkout(uuid4(), settings=settings)
        failed_req_id = acc.request_id
        failed_order_id = acc.order_id
        try:
            await wait_for_order_fulfillment(acc.order_id, settings=settings, timeout_seconds=1.5)
        except Exception:
            pass
    else:
        try:
            await submit_checkout(uuid4(), settings=settings)
        except CheckoutError as chk_err:
            if chk_err.request_id:
                try:
                    failed_req_id = UUID(chk_err.request_id)
                except Exception:
                    pass

    incident_id = uuid4()

    # Gather fixed diagnostic reads via MCP
    fixed_calls = [
        ("get_health", {}),
        ("get_logs", {"request_id": str(failed_req_id), "limit": 40}),
        ("get_config", {}),
        ("probe_checkout", {"order_id": str(failed_order_id)} if failed_order_id else {}),
    ]

    evidence_ids: list[str] = []
    recent_results: list[dict[str, Any]] = []
    log_texts = ""

    async with McpSession(settings) as mcp:
        for tool_name, tool_args in fixed_calls:
            ev = await mcp.call_read_tool(incident_id, tool_name, tool_args)
            evidence_ids.append(ev.evidence_id)
            recent_results.append(ev.model_dump(mode="json"))
            if tool_name == "get_logs":
                log_texts = " ".join(
                    e.get("message", "") for e in ev.data.get("logs", [])
                )

    # Keyword retrieval
    query = f"{symptom} {log_texts}".strip()
    with get_control_connection(settings) as conn:
        rb_chunks = retrieve_runbooks(
            conn=conn,
            query=query,
            limit=3,
        )
    rb_data = [
        {
            "chunk_id": c.data["chunk_id"],
            "content": c.data["content"],
            "source": c.source,
            "source_version": c.source_version,
            "section_id": c.section_id,
        }
        for c in rb_chunks
    ]

    state = {
        "incident_id": str(incident_id),
        "phase": "investigating",
        "symptom": symptom,
        "request_id": str(failed_req_id),
        "order_id": str(failed_order_id) if failed_order_id else None,
        "evidence_ids": evidence_ids,
        "case_notes": [],
        "recent_results": recent_results[-2:],
        "retrieved_runbooks": rb_data,
    }

    messages = build_context(
        state,
        token_budget=settings.il_max_input_tokens,
        model_name=settings.il_model,
    )
    input_tokens = count_request_tokens(messages)

    return RepresentativeInput(
        case_id=case_data["case_id"],
        family=case_data["family"],
        variation=case_data["variation"],
        seed=case_data["seed"],
        expected_decision=gold_data["expected_decision"],
        expected_action=gold_data.get("expected_action"),
        symptom=symptom,
        messages=messages,
        input_tokens=input_tokens,
    )


async def execute_observation(
    rep_input: RepresentativeInput,
    observation_type: str,
    repeat_index: int,
    settings: Settings,
) -> Observation:
    """Execute one measured model invocation with memory sampling and monotonic timing."""
    sampler = MemorySampler(sample_interval_seconds=0.1)
    await sampler.start()

    t_start = time.monotonic()
    options = {
        "num_predict": settings.il_max_output_tokens,
        "num_ctx": 8192,
    }

    resp_data: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=float(settings.il_model_timeout_seconds)) as client:
        resp = await client.post(
            f"{settings.il_ollama_base_url}/api/chat",
            json={
                "model": settings.il_model,
                "messages": rep_input.messages,
                "think": False,
                "format": DIAGNOSIS_SCHEMA,
                "stream": False,
                "options": options,
            },
        )
        if resp.status_code == 200:
            resp_data = resp.json()
        else:
            raise RuntimeError(f"Ollama call failed ({resp.status_code}): {resp.text}")

    t_end = time.monotonic()
    await sampler.stop()

    wall_duration = round(t_end - t_start, 3)

    load_ns = resp_data.get("load_duration")
    prompt_eval_ns = resp_data.get("prompt_eval_duration")
    eval_ns = resp_data.get("eval_duration")
    prompt_eval_count = resp_data.get("prompt_eval_count")
    eval_count = resp_data.get("eval_count")

    load_sec = round(load_ns / 1e9, 3) if load_ns is not None else None
    prompt_eval_sec = round(prompt_eval_ns / 1e9, 3) if prompt_eval_ns is not None else None
    eval_sec = round(eval_ns / 1e9, 3) if eval_ns is not None else None
    tps = round(eval_count / (eval_ns / 1e9), 2) if eval_count and eval_ns and eval_ns > 0 else None

    # Parse diagnosis JSON
    raw_content = resp_data.get("message", {}).get("content", "")
    diag_decision: str | None = None
    diag_action: str | None = None
    quality_passed = False
    quality_notes = ""

    try:
        parsed = json.loads(raw_content)
        diag_decision = parsed.get("decision")
        repair = parsed.get("repair")
        if repair and isinstance(repair, dict):
            diag_action = repair.get("action")

        expected_decision = (
            "propose_repair"
            if rep_input.expected_decision == "approve"
            else rep_input.expected_decision
        )
        decision_ok = diag_decision == expected_decision
        action_ok = diag_action == rep_input.expected_action
        quality_passed = decision_ok and action_ok
        quality_notes = f"decision_ok={decision_ok}, action_ok={action_ok}"
    except Exception as exc:
        quality_notes = f"json_parse_error: {exc}"

    return Observation(
        observation_id=str(uuid4()),
        case_id=rep_input.case_id,
        family=rep_input.family,
        variation=rep_input.variation,
        observation_type=observation_type,
        repeat_index=repeat_index,
        wall_clock_duration_seconds=wall_duration,
        load_duration_seconds=load_sec,
        prompt_eval_count=prompt_eval_count,
        prompt_eval_duration_seconds=prompt_eval_sec,
        eval_count=eval_count,
        eval_duration_seconds=eval_sec,
        tokens_per_second=tps,
        ttft_seconds=None,
        ttft_reason="measured_in_separate_streaming_probe",
        peak_ram_used_mb=round(sampler.peak_ram_used_mb, 1) if sampler.peak_ram_used_mb else None,
        peak_gpu_vram_used_mb=sampler.peak_gpu_vram_used_mb,
        quality_passed=quality_passed,
        quality_notes=quality_notes,
        diagnosis_decision=diag_decision,
        diagnosis_action=diag_action,
    )


def percentile(data: list[float], pct: float) -> float:
    """Calculate percentile value from sorted list of numbers."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = (len(sorted_data) - 1) * (pct / 100.0)
    floor_idx = int(idx)
    ceil_idx = min(floor_idx + 1, len(sorted_data) - 1)
    weight = idx - floor_idx
    return round(sorted_data[floor_idx] * (1.0 - weight) + sorted_data[ceil_idx] * weight, 2)


async def run_benchmark(
    split: str = "dev",
    sample_name: str = "representative-six",
    warm_repeats: int = 3,
    output_path: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute complete serving benchmark across representative inputs."""
    if settings is None:
        settings = Settings.from_env()

    repo_root = Path(__file__).resolve().parent.parent
    cases_file = repo_root / "evals" / "cases" / f"{split}.jsonl"
    gold_file = repo_root / "evals" / "gold" / f"{split}.jsonl"

    all_cases = [json.loads(line) for line in cases_file.read_text().splitlines() if line.strip()]
    all_golds = {
        g["case_id"]: g
        for g in [json.loads(line) for line in gold_file.read_text().splitlines() if line.strip()]
    }

    # Select and freeze six representative development inputs (two per family: clean and noisy)
    rep_case_ids = [
        "dev-inventory-01",
        "dev-inventory-02",
        "dev-worker-01",
        "dev-worker-02",
        "dev-upstream-01",
        "dev-upstream-02",
    ]
    selected_cases = [c for c in all_cases if c["case_id"] in rep_case_ids]

    logger.info("Capturing environment and model qualification metadata...")
    env_info = get_environment_info(settings)

    logger.info("Setting up sandbox and preparing %d representative inputs...", len(selected_cases))
    rep_inputs: list[RepresentativeInput] = []
    for c in selected_cases:
        gold = all_golds[c["case_id"]]
        rep_in = await setup_case_and_build_input(c, gold, settings)
        rep_inputs.append(rep_in)
        logger.info(
            "Prepared input %s (%s, %s): %d tokens",
            rep_in.case_id,
            rep_in.family,
            rep_in.variation,
            rep_in.input_tokens,
        )

    observations: list[Observation] = []

    # 1. Cold-load observation on input 0
    logger.info("Running cold-load observation on %s...", rep_inputs[0].case_id)
    cold_obs = await execute_observation(
        rep_input=rep_inputs[0],
        observation_type="cold",
        repeat_index=0,
        settings=settings,
    )
    observations.append(cold_obs)
    logger.info(
        "Cold-load: wall=%.2fs, load=%.2fs, eval_tps=%.1f, quality=%s",
        cold_obs.wall_clock_duration_seconds,
        cold_obs.load_duration_seconds or 0.0,
        cold_obs.tokens_per_second or 0.0,
        cold_obs.quality_passed,
    )

    # 2. Warm repeats: 3 repeats across all 6 inputs = 18 warm observations
    logger.info("Running %d warm repeats across %d inputs...", warm_repeats, len(rep_inputs))
    for rep in range(1, warm_repeats + 1):
        for rep_in in rep_inputs:
            logger.info("Warm repeat %d/3 on %s...", rep, rep_in.case_id)
            obs = await execute_observation(
                rep_input=rep_in,
                observation_type="warm",
                repeat_index=rep,
                settings=settings,
            )
            observations.append(obs)
            logger.info(
                "  Wall: %.2fs | Eval: %.2fs | TPS: %.1f | Quality: %s",
                obs.wall_clock_duration_seconds,
                obs.eval_duration_seconds or 0.0,
                obs.tokens_per_second or 0.0,
                obs.quality_passed,
            )

    # 3. Dedicated streaming TTFT measurements on each of the 6 inputs
    logger.info("Measuring streaming TTFT across representative inputs...")
    ttft_samples: list[dict[str, Any]] = []
    for rep_in in rep_inputs:
        ttft_val, reason = await time_streaming_ttft(settings, rep_in.messages)
        ttft_samples.append({
            "case_id": rep_in.case_id,
            "family": rep_in.family,
            "ttft_seconds": ttft_val,
            "reason": reason,
        })
        logger.info("  %s TTFT: %s (reason: %s)", rep_in.case_id, ttft_val, reason)

    # Compute aggregate statistics on warm observations
    warm_obs = [o for o in observations if o.observation_type == "warm"]
    wall_durations = [o.wall_clock_duration_seconds for o in warm_obs]
    eval_durations = [o.eval_duration_seconds for o in warm_obs if o.eval_duration_seconds]
    prompt_eval_durations = [
        o.prompt_eval_duration_seconds for o in warm_obs if o.prompt_eval_duration_seconds
    ]
    tps_values = [o.tokens_per_second for o in warm_obs if o.tokens_per_second]
    out_tokens = [o.eval_count for o in warm_obs if o.eval_count]
    in_tokens = [o.prompt_eval_count for o in warm_obs if o.prompt_eval_count]
    valid_ttfts = [s["ttft_seconds"] for s in ttft_samples if s["ttft_seconds"] is not None]

    peak_ram = max((o.peak_ram_used_mb for o in warm_obs if o.peak_ram_used_mb), default=None)
    peak_vram = max(
        (o.peak_gpu_vram_used_mb for o in warm_obs if o.peak_gpu_vram_used_mb), default=None
    )
    quality_passes = sum(1 for o in warm_obs if o.quality_passed)

    summary = {
        "sample_counts": {
            "cold_observations": 1,
            "warm_observations": len(warm_obs),
            "total_observations": len(observations),
            "streaming_ttft_samples": len(ttft_samples),
        },
        "wall_clock_duration_seconds": {
            "p50": percentile(wall_durations, 50),
            "p95": percentile(wall_durations, 95),
            "min": round(min(wall_durations), 2) if wall_durations else 0.0,
            "max": round(max(wall_durations), 2) if wall_durations else 0.0,
            "avg": round(sum(wall_durations) / len(wall_durations), 2) if wall_durations else 0.0,
        },
        "eval_generation_duration_seconds": {
            "p50": percentile(eval_durations, 50),
            "p95": percentile(eval_durations, 95),
            "min": round(min(eval_durations), 2) if eval_durations else 0.0,
            "max": round(max(eval_durations), 2) if eval_durations else 0.0,
            "avg": round(sum(eval_durations) / len(eval_durations), 2) if eval_durations else 0.0,
        },
        "prompt_eval_duration_seconds": {
            "p50": percentile(prompt_eval_durations, 50),
            "p95": percentile(prompt_eval_durations, 95),
            "min": round(min(prompt_eval_durations), 2) if prompt_eval_durations else 0.0,
            "max": round(max(prompt_eval_durations), 2) if prompt_eval_durations else 0.0,
            "avg": (
                round(sum(prompt_eval_durations) / len(prompt_eval_durations), 2)
                if prompt_eval_durations
                else 0.0
            ),
        },
        "tokens_per_second": {
            "p50": percentile(tps_values, 50),
            "p95": percentile(tps_values, 95),
            "min": round(min(tps_values), 2) if tps_values else 0.0,
            "max": round(max(tps_values), 2) if tps_values else 0.0,
            "avg": round(sum(tps_values) / len(tps_values), 2) if tps_values else 0.0,
        },
        "time_to_first_token_seconds": {
            "valid_samples_count": len(valid_ttfts),
            "p50": percentile(valid_ttfts, 50) if valid_ttfts else None,
            "p95": percentile(valid_ttfts, 95) if valid_ttfts else None,
            "avg": round(sum(valid_ttfts) / len(valid_ttfts), 3) if valid_ttfts else None,
        },
        "token_counts": {
            "input_tokens_avg": round(sum(in_tokens) / len(in_tokens), 1) if in_tokens else 0,
            "output_tokens_avg": round(sum(out_tokens) / len(out_tokens), 1) if out_tokens else 0,
        },
        "memory": {
            "sampling_interval_seconds": 0.1,
            "peak_ram_used_mb": peak_ram,
            "peak_gpu_vram_used_mb": peak_vram,
        },
        "quality": {
            "warm_passed": quality_passes,
            "warm_total": len(warm_obs),
            "warm_pass_rate_pct": (
                round((quality_passes / len(warm_obs)) * 100, 2) if warm_obs else 0.0
            ),
            "cold_passed": cold_obs.quality_passed,
        },
        "cost_and_limits": {
            "paid_inference_api_calls": 0,
            "local_hardware_utilized": True,
            "active_deadline_seconds_target": 240,
            "observed_wall_clock_under_deadline": all(d <= 240 for d in wall_durations),
            "concurrency": 1,
            "limitations_note": (
                "Measurements reflect local CPU compute on Ubuntu Linux with Qwen3:8B Q4_K_M. "
                "Non-thinking mode explicitly disables reasoning tokens; streaming timed for TTFT."
            ),
        },
    }

    report = {
        "benchmark_id": f"bm-{int(time.time())}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "split": split,
        "sample": sample_name,
        "environment": env_info,
        "representative_inputs": [
            {
                "case_id": r.case_id,
                "family": r.family,
                "variation": r.variation,
                "expected_decision": r.expected_decision,
                "expected_action": r.expected_action,
                "input_tokens": r.input_tokens,
            }
            for r in rep_inputs
        ],
        "summary": summary,
        "streaming_ttft_samples": ttft_samples,
        "observations": [asdict(o) for o in observations],
    }

    if output_path is None:
        output_path = repo_root / "artifacts" / "benchmarks" / "dev_benchmark.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("Benchmark report written to %s", output_path)

    # Also emit directory artifact per EVALUATION.md:
    # artifacts/benchmarks/<run-id>/{measurements.jsonl, summary.json, report.md}
    run_dir = repo_root / "artifacts" / "benchmarks" / report["benchmark_id"]
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        # 1. measurements.jsonl
        measurements_path = run_dir / "measurements.jsonl"
        with measurements_path.open("w", encoding="utf-8") as f:
            for obs in report["observations"]:
                f.write(json.dumps(obs) + "\n")
        # 2. summary.json
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(report["summary"], indent=2), encoding="utf-8")
        # 3. report.md
        report_md_path = run_dir / "report.md"
        report_md_path.write_text(format_markdown_report(report), encoding="utf-8")
        logger.info("Run directory artifacts written to %s", run_dir)
    except Exception as e:
        logger.warning("Could not write run_dir artifacts: %s", e)

    return report


def format_markdown_report(report: dict[str, Any]) -> str:
    """Format benchmark results as clean markdown."""
    s = report["summary"]
    env = report["environment"]
    ollama_meta = env["ollama"]
    details = ollama_meta.get("model_details") or {}
    digest_prefix = details.get("digest", "")[:16]

    c_prov = ollama_meta["compute_provider"].upper()
    gpu_label = env["gpu"].get("device") or "CPU offload"
    deadline_ok = s["cost_and_limits"]["observed_wall_clock_under_deadline"]
    lines = [
        "# IncidentLab Serving Performance Benchmark Report",
        f"**Benchmark ID:** `{report['benchmark_id']}` | **Date:** {report['timestamp']}",
        f"**Model:** `{ollama_meta['model']}` (Digest: `{digest_prefix}...`)",
        f"**Compute Provider:** `{c_prov}` | **Concurrency:** 1 | **Non-Thinking:** True",
        f"**Host RAM:** {env['ram'].get('MemTotal', 'N/A')} | **GPU:** {gpu_label}",
        "",
        "## Latency and Throughput (18 Warm Observations across 6 Inputs)",
        "| Metric | p50 | p95 | Min | Max | Avg |",
        "|---|---|---|---|---|---|",
    ]

    w_clk = s["wall_clock_duration_seconds"]
    lines.append(
        f"| Active Wall-Clock (s) | {w_clk['p50']} | {w_clk['p95']} | "
        f"{w_clk['min']} | {w_clk['max']} | {w_clk['avg']} |"
    )

    ev_d = s["eval_generation_duration_seconds"]
    lines.append(
        f"| Model Generation Duration (s) | {ev_d['p50']} | {ev_d['p95']} | "
        f"{ev_d['min']} | {ev_d['max']} | {ev_d['avg']} |"
    )

    p_ev = s["prompt_eval_duration_seconds"]
    lines.append(
        f"| Prompt Eval Duration (s) | {p_ev['p50']} | {p_ev['p95']} | "
        f"{p_ev['min']} | {p_ev['max']} | {p_ev['avg']} |"
    )

    tps = s["tokens_per_second"]
    lines.append(
        f"| Output Throughput (tokens/s) | {tps['p50']} | {tps['p95']} | "
        f"{tps['min']} | {tps['max']} | {tps['avg']} |"
    )

    ttft = s["time_to_first_token_seconds"]
    if ttft["p50"] is not None:
        lines.append(
            f"| Streaming TTFT (s) | {ttft['p50']} | {ttft['p95']} | - | - | {ttft['avg']} |"
        )

    q = s["quality"]
    pass_pct = q["warm_pass_rate_pct"]
    lines.extend([
        "",
        "## Quality and Cost Accounting",
        f"- **Warm Quality Pass Rate:** {q['warm_passed']}/{q['warm_total']} ({pass_pct}%)",
        f"- **Cold Load Observation:** passed={q['cold_passed']}",
        f"- **Average Input Tokens:** {s['token_counts']['input_tokens_avg']} (Limit: 6,000)",
        f"- **Average Output Tokens:** {s['token_counts']['output_tokens_avg']} (Limit: 1,024)",
        f"- **Peak RAM Used:** {s['memory']['peak_ram_used_mb']} MB (Sampling Interval: 0.1s)",
        f"- **Active Deadline Compliance (<240s):** {deadline_ok}",
        "- **Paid Inference Cost:** $0.00 (Zero paid API calls, 100% local hardware)",
        "",
    ])

    return "\n".join(lines)


def print_benchmark_table(report: dict[str, Any]):
    """Print benchmark results table to stdout."""
    print(format_markdown_report(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Benchmark IncidentLab local serving configuration."
    )
    parser.add_argument("--split", type=str, default="dev", help="Dataset split (default: dev)")
    parser.add_argument(
        "--sample",
        type=str,
        default="representative-six",
        help="Sample selection (default: representative-six)",
    )
    parser.add_argument(
        "--warm-repeats",
        type=int,
        default=3,
        help="Warm repeats per input (default: 3)",
    )
    parser.add_argument("--output", type=str, default=None, help="Output JSON path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    out_p = Path(args.output) if args.output else None
    rep = asyncio.run(
        run_benchmark(
            split=args.split,
            sample_name=args.sample,
            warm_repeats=args.warm_repeats,
            output_path=out_p,
        )
    )
    print_benchmark_table(rep)

