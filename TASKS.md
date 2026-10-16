# IncidentLab execution ledger

Last updated: 2026-10-13. Current state: documentation-only planning baseline. Application implementation has not started. No model/GPU qualification, passing application test, benchmark, recovery demo, evaluation score, or implementation-phase acceptance is claimed.

`PRD.md` is the v1 scope with the approved pre-lock retrieval simplification recorded below. `ARCHITECTURE.md` owns interfaces, `EVALUATION.md` owns measurements, and `ROADMAP.md` owns execution steps. This file alone owns statuses, gates, decisions, deviations, blockers, evidence, and exact next action.

## Status and exact next action

Task states: `pending`, `in_progress`, `blocked`, `verified_complete`. Only mark verified complete after the task deliverable and minimum verification actually pass with evidence. A plan, directory, stub, or installed dependency is not a completed feature. Phase acceptance and authorization are separate from task verification.

**Batch Implementation Authorization Recorded (2026-10-13):** The user explicitly authorized phases P0, P1, P2, P3, P4, P5, and P6, in sequential order, with an automated fresh review-and-fix subagent gate after each phase before advancing. This authorization does not permit scope changes, weaker acceptance criteria, paid services, destructive host operations, public deployment, or fabricated user acceptance. Engineering-phase authorization does not remove the application's human-approval boundary for repairs. Final product acceptance remains with the user.

**Next action:** Execute IL-T16: measure one qualified serving configuration.

**Current blockers:** None. Docker and Compose installed; PostgreSQL container, sandbox, control, and Ollama running and verified in `incidentlab-test`.

**Execution constraint:** Follow `AGENTS.md` on simplicity during coding. Stop once mapped acceptance and minimum verification pass. Do not add a framework, service, test expansion, repeated review, or optimization without an unmet active requirement or observed issue.

## Planning deliverables

| Artifact | Status | Truth boundary |
|---|---|---|
| `PRD.md` | Written | Vision, scope and acceptance; not implemented product. |
| `ARCHITECTURE.md` | Written | Planned interfaces and safety contracts. |
| `EVALUATION.md` | Written | Protocol and targets; no claimed results. |
| `ROADMAP.md` | Written | Seven sequential phases and 21 pending tasks. |
| `TASKS.md` | Written | Initial status and approval ledger. |

The scaffold owner records verified initial Git/private-remote outcomes separately; this ledger does not infer them from planned actions. Implementation checkpoint evidence is empty at this baseline.

Planning audit, 2026-10-13: verified seven phases, 21 roadmap tasks represented in this ledger, all 14 PRD IDs mapped, required files present, local document links resolving, and configuration names documented. Reviewed approval/execution/verification contracts and phase dependencies. Credential-pattern and file-allowlist checks found no unexpected content. Application tests were not run because no application has been implemented. Simplicity during coding and proportionate verification are binding in AGENTS.

## Canonical task register

| Task | Deliverable | PRD IDs | Dependencies | Status | Evidence |
|---|---|---|---|---|---|
| IL-T01 | Qualified/pinned runtime and model probe | IL-05, IL-11, IL-13, IL-14 | P0 start approval | verified_complete | `artifacts/qualification.json`; 8 passed in `tests/test_model_contract.py` |
| IL-T02 | Storage, roles, owner API boundary | IL-07, IL-10, IL-11, IL-14 | IL-T01 | verified_complete | `migrations/001_initial.sql` applied; 16 passed in `tests/test_api.py` and `tests/test_model_contract.py` |
| IL-T03 | Actual healthy checkout and worker fulfillment | IL-01, IL-09, IL-14 | IL-T02 | verified_complete | 23 passed in `tests/`; smoke-checkout fulfilled in 0.33s |
| IL-T04 | Observable inventory fault and runbook | IL-02, IL-03, IL-06 | P0 accepted; P1 approved; IL-T03 | verified_complete | `data/runbooks/inventory.md`; 26 passed in tests/; `seed` and failing `smoke-checkout` verified |
| IL-T05 | Read-only MCP and keyword retrieval | IL-03, IL-04, IL-06, IL-11 | IL-T04 | verified_complete | 15 runbook chunks indexed; 5 passed in `tests/test_mcp_boundary.py` and `tests/test_retrieval.py` |
| IL-T06 | Bounded agent and durable proposal | IL-03, IL-05, IL-06, IL-07, IL-10, IL-11 | IL-T05 | verified_complete | `tests/test_agent_contract.py` passed; live model test produced immutable proposal in `awaiting_approval` with zero sandbox effect |
| IL-T07 | Bound approval and atomic claim | IL-07, IL-08, IL-11 | P1 accepted; P2 approved; IL-T06 | verified_complete | `tests/test_approval.py` passed; 9 focused tests, duplicate idempotency, reject 0 claims, stale revision rejected |
| IL-T08 | Repair, receipt and functional verification | IL-08, IL-09, IL-11 | IL-T07 | verified_complete | `tests/test_execution.py` passed (8 passed); receipt reconciliation, independent checkout verifier, green health is not recovery |
| IL-T09 | Thin UI and first complete live E2E | IL-01, IL-03, IL-07, IL-08, IL-09, IL-10, IL-14 | IL-T08 | verified_complete | Built React 18 UI; live model E2E `tests/test_end_to_end.py` verified recovery and rejection (685s) |
| IL-T10 | Worker recovery and maintenance escalation | IL-02, IL-03, IL-08, IL-09 | P2 accepted; P3 approved; IL-T09 | verified_complete | Commit e9abe58; resume_order_worker added to allowlist, worker_paused verified live, upstream_maintenance escalated with repair=None |
| IL-T11 | Runbook grounding and bounded context | IL-03, IL-06, IL-12 | IL-T10 | verified_complete | Commit 76afdb3; 12 dev cases frozen in evals/cases/dev.jsonl, context budget test (6000 tokens) passed, chunk bounds enforced (<200 tokens) |
| IL-T12 | Restart/cancel/unknown outcome integrity | IL-07, IL-08, IL-09, IL-11 | IL-T10, IL-T11 | verified_complete | Commit 3b75e50; tests/test_recovery.py (7 passed), ack loss crash harness verified repair_post_count==1, resolve-unknown CLI closes unresolved with effect_unknown=true, releases one-active constraint |
| IL-T13 | Frozen cases, isolated runner and scoring | IL-11, IL-12 | P3 accepted; P4 approved; IL-T12 | verified_complete | Commit `f1c1827`; 12 dev & 18 heldout cases frozen in `evals/manifest.json` (SHA-256 verified), heldout `not_run`, deterministic scorer `evals/score.py`, `tests/test_evaluation.py` (8 passed) |
| IL-T14 | Fixed-versus-agent development comparison | IL-03, IL-05, IL-06, IL-12 | IL-T13 | verified_complete | Commit `804a527`; `evals/baseline.py` executes exact 4 reads -> retrieval -> 1 schema call; paired dev comparison on 12 cases (`dev_report.json`), 100% relevant coverage, 0 unauthorized repairs, 0 fabricated verifications |
| IL-T15 | Essential security regressions and offline CI | IL-04, IL-07, IL-08, IL-09, IL-11, IL-14 | IL-T14 | verified_complete | Commit `1f535ee`; `.github/workflows/ci.yml` lean offline CI workflow (Ruff, pytest, typecheck, build), security regressions (secret/tool boundary, prompt injection in logs, unauthorized repair refusal), Docker COPY boundaries verified |
| IL-T16 | Actual local baseline serving benchmark | IL-05, IL-12, IL-13 | P4 accepted; P5 approved; IL-T15 | pending | Not run |
| IL-T17 | Clear evidence, authority, failure UI | IL-03, IL-07, IL-09, IL-10, IL-11 | IL-T16 | pending | Not run |
| IL-T18 | Clean-clone rehearsal and accurate guide | IL-01, IL-10, IL-13, IL-14 | IL-T16, IL-T17 | pending | Not run |
| IL-T19 | Frozen paired 18-case held-out report | IL-03, IL-05, IL-06, IL-07, IL-08, IL-09, IL-11, IL-12, IL-13 | P5 accepted; P6 approved; IL-T18 | pending | Not run |
| IL-T20 | Requirement evidence and interview review | IL-01–IL-14 | IL-T19 release gates pass | pending | Not run |
| IL-T21 | Final live demo and accepted handoff | IL-01–IL-14 | IL-T20 | pending | Not run |

## Phase gate ledger

Record actual approving user statement/chat reference, date, checkpoint commit, evidence, and approved limitations. An agent's judgment is not user approval. Starting a phase does not pre-accept its result; explicit approval of a named phase batch may be recorded without inventing consent for other phases.

| Gate | Required review | Start approval | Completion evidence | User acceptance / next phase approval |
|---|---|---|---|---|
| Planning → P0 | Scope/contracts/evaluation/plan | Authorized in batch (2026-10-13) | In progress | Authorized in batch (P1 authorized) |
| P0 → P1 | Model/lab qualification and healthy checkout | Authorized in batch (2026-10-13) | Verified complete (qualification.json, 23 passed tests, smoke-checkout fulfilled) | Authorized in batch (P2 authorized) |
| P1 → P2 | Live evidence to proposal, zero effect | Authorized in batch (2026-10-13) | Verified complete (48f25cd; 34 passed tests, 15 chunks indexed, smoke-checkout fulfilled, subagent audit PASS) | Authorized in batch (P3 authorized) |
| P2 → P3 | Full inventory recovery and rejection | Authorized in batch (2026-10-13) | Verified complete (8becaab; 51 offline tests pass, npm ci/typecheck/build pass, live E2E pass, subagent audit PASS) | Authorized in batch (P3 authorized) |
| P3 → P4 | Three families, retrieval, crash safety | Authorized in batch (2026-10-13) | Verified complete (3cb2a0a; 66 offline tests pass, live worker recovery & maintenance escalation verified, ack loss crash safety proven, resolve-unknown CLI operational, subagent audit PASS) | Authorized in batch (P4 authorized) |
| P4 → P5 | Dev comparison, frozen held-out, offline CI | Authorized in batch (2026-10-13) | Verified complete (329b8dc; 77 offline tests pass, 12-case dev report generated with 0 unauthorized repairs / 0 fabricated verifications, held-out frozen and not_run, lean offline CI configured, subagent audit PASS) | Authorized in batch (P5 authorized) |
| P5 → P6 | Measured serving, usable UI, clean setup | Authorized in batch (2026-10-13) | Pending | User final acceptance required |
| P6 → Complete | ≥15/18 plus hard-zero gates and final demo | Authorized in batch (2026-10-13) | Pending | User final acceptance required |

## Design decisions

These are baseline design decisions, not proof of implementation or separate permission to change scope.

| ID | Decision | Reason |
|---|---|---|
| IL-D01 | One control app and isolated tiny sandbox | Real target effects with separate authority; no service platform. |
| IL-D02 | One PostgreSQL instance with separated schemas/roles | Durable graph, approval and receipts without queues or caches. |
| IL-D03 | Ollama native client and initial `qwen3:8b` candidate | Local inference; actual licence/digest/tools/schema/tokenizer/fit qualification remains pending. |
| IL-D04 | Read-only stdio MCP and server-only finite executor | Useful diagnostic tools with repair authority independent of model text. |
| IL-D05 | Immutable proposal, bound owner decision, claim and receipt | Exact payload/revision and one effect per key; no distributed exactly-once claim. |
| IL-D06 | Fresh checkout fulfillment establishes recovery | Health, acknowledgment, or confidence alone cannot prove recovery. |
| IL-D07 | Polling `/api/v1` and token in UI memory | Enough for one private owner, without SSE/auth-platform scope. |
| IL-D08 | Keyword runbook retrieval, checked in existing dev runs | Approved simplification: preserve cited grounding without duplicating MoneyTrail's deep RAG experiments. No embedding/vector dependency or alternate-mode framework. |
| IL-D09 | 12 dev / 18 held-out with evidence variations | Actual outcomes and uncertainty, with gold absent from runtime context. |
| IL-D10 | Transient evaluation image, same app contracts | Essential integration tests/harness without an extra running service. |
| IL-D11 | One offline CI job and focused checks | Protect required behavior without model calls per PR or coverage quotas. |
| IL-D12 | Seven reviews and stop-at-acceptance execution | Preserve scope and avoid incremental overbuilding. |
| IL-D13 | One baseline serving measurement | Six representative inputs, cold observation, three warm repeats; second config only for a measured issue. |
| IL-D14 | Narrow owner closure for unresolved execution | Quiesce, inspect and acknowledge uncertainty; never replay or invent no-effect/recovery. |

## Deviations

2026-10-13, approved pre-lock amendment (IL-06; IL-T05/IL-T11 and dependent documentation): use versioned keyword runbook retrieval and existing development cases to measure grounding. Remove mandatory dense/hybrid experiments, embedding/vector dependencies, mode settings, and comparison commands. The simpler option is sufficient for the three-runbook scope; MoneyTrail retains the portfolio's deep RAG work, without becoming a runtime dependency. Dense/hybrid remains deferred and requires an observed retrieval failure plus separate approval. Investigation, MCP, context bounds, approval, repairs, verification, serving, outcome thresholds, and existing essential regression groups are unchanged.

Approval reference: the user approved the three pre-lock amendments across both projects with “Approved but ensure you dont overengineer , overdo and drift apart from the original intended vision”. This authorizes documentation alignment only, not P0 implementation or any phase acceptance. PRD, ARCHITECTURE, EVALUATION, ROADMAP, README, and `.env.example` are synchronized with this decision.

Future material changes require the reason, alternatives, affected requirement/interface, acceptance/security/cost impact, and explicit approval. Until approval preserve this scope and pause only dependent work. Routine compatible dependency pins are documented choices; adding a provider/service/action/fault family or lowering an acceptance gate is a scope change.

## Checkpoints and requirement evidence

Approved-amendment audit, 2026-10-13: `git diff --check` passed. Read-only document checks confirmed only the six intended Markdown files and `.env.example` changed, all requirement rows except IL-06 remained identical, all 21 task IDs remain represented, and local links/configuration references resolve. Removed dependency/settings/interface/command names no longer appear in the active contracts. Approval, repair, verification, budgets, evaluation sizes/thresholds, and essential regression coverage are unchanged. No application tests were added or run; implementation remains unstarted.

### Checkpoints and requirement evidence

- **IL-T01 (2026-10-13):**
  - **Commit:** `chore: qualify and pin IncidentLab runtime`
  - **Files:** `pyproject.toml`, `uv.lock`, `Dockerfile`, `compose.yaml`, `.dockerignore`, `src/incidentlab/__init__.py`, `src/incidentlab/config.py`, `src/incidentlab/model.py`, `src/incidentlab/cli.py`, `tests/test_model_contract.py`.
  - **Commands executed:**
    - `uv lock` (exit 0)
    - `uv sync --locked --extra dev` (exit 0)
    - `uv run pytest tests/test_model_contract.py -m 'not live_model'` (exit 0, 8 passed)
    - `uv run ruff check .` (exit 0, all checks passed)
    - `uv run python -m incidentlab.cli qualify` (exit 0, generated `artifacts/qualification.json`)
  - **Observed qualification facts:**
    - Python 3.12.3 on Ubuntu 24.04 (Linux 6.17.0-1032-oem x86_64).
    - Memory: 131,313,820 kB (~128 GB) total, 122,143,600 kB available.
    - Ollama: v0.40.3 active on `127.0.0.1:11434`.
    - Model: `qwen3:8b`, digest `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`, 8.2B params, Q4_K_M quantization, 40960 context length.
    - Model capabilities verified: native tool calling verified (`get_health` called), structured schema mode verified (`sample_diagnosis: {"summary": "OK", "decision": "escalate"}`), non-thinking mode verified (`think=false`).
    - Environment gaps / blockers observed:
      - Docker / Docker Compose is not installed on the host (`Command not found: docker`).
      - Passwordless sudo is not enabled (`sudo -n true` requires password).
      - GPU `nvidia-smi` failed with driver/library mismatch (NVRM 595.91.07 vs NVML 595.99.02).
  - **Next action:** IL-T02 completed. Next action: IL-T03 (healthy fake checkout and worker fulfillment).

- **IL-T02 (2026-10-14):**
  - **Commit:** `feat: add durable storage and owner API boundary`
  - **Files:** `migrations/001_initial.sql`, `src/incidentlab/schemas.py`, `src/incidentlab/db.py`, `src/incidentlab/events.py`, `src/incidentlab/api.py`, `src/incidentlab/cli.py`, `tests/conftest.py`, `tests/test_api.py`, `Dockerfile`.
  - **Commands executed:**
    - `docker compose -p incidentlab-test up -d postgres` (exit 0, PostgreSQL 17 active on port 5432)
    - `incidentlab.cli migrate` (exit 0, created schemas control, sandbox, checkpoints; set role grants)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_api.py tests/test_model_contract.py` (exit 0, 16 passed)
    - `uv run ruff check .` (exit 0, all checks passed)
  - **Observed facts:**
    - Control and sandbox roles isolated: control role has zero privileges on sandbox tables; sandbox role has zero privileges on control tables.
    - Single active incident constraint verified: unique partial index prevents second active incident.
    - Audit event log verified: strictly monotonic seq, correct SHA-256 prev_hash chain, sensitive key redaction.
    - API authentication boundary verified: 401 on missing/invalid bearer token, 200 on valid token.
  - **Next action:** IL-T03 completed. Next action: P0 Review-and-Fix gate subagent.

- **IL-T03 (2026-10-14):**
  - **Commit:** `feat: complete healthy sandbox checkout flow`
  - **Files:** `src/incidentlab/sandbox/__init__.py`, `src/incidentlab/sandbox/schemas.py`, `src/incidentlab/sandbox/service.py`, `src/incidentlab/sandbox/worker.py`, `src/incidentlab/sandbox/api.py`, `src/incidentlab/executor.py`, `tests/test_sandbox_flow.py`, `src/incidentlab/api.py`, `src/incidentlab/cli.py`, `src/incidentlab/db.py`, `src/incidentlab/schemas.py`, `compose.yaml`, `Dockerfile`.
  - **Commands executed:**
    - `docker build --target evaluation -t incidentlab-eval:local .` (exit 0)
    - `docker compose -p incidentlab-test up --build -d sandbox` (exit 0, Uvicorn on 0.0.0.0:8001 with 250ms worker loop)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_sandbox_flow.py -m integration` (exit 0, 6 passed, 1 deselected)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/` (exit 0, 23 passed)
    - `docker compose -p incidentlab-test up --build -d control` (exit 0, Uvicorn on 0.0.0.0:8000)
    - `docker compose -p incidentlab-test exec -T control python -m incidentlab.cli smoke-checkout` (first run: exit 0, fulfilled in 0.335s)
    - `docker compose -p incidentlab-test exec -T control python -m incidentlab.cli smoke-checkout` (second run: exit 0, fulfilled in 0.334s)
    - `uv run ruff check .` (exit 0, all checks passed)
  - **Observed facts:**
    - Fake checkout performs real HTTP inventory check to `http://127.0.0.1:8001/dependency/inventory/SKU-1`.
    - 250ms worker loop fulfills pending orders transactionally and sets `fulfilled_at`.
    - Checkout idempotency verified: repeated `request_key` returns the identical order.
    - Token boundaries enforced: write, read, and admin tokens strictly validated with constant-time comparison.
    - Owner demo routes `/api/v1/demo/reset` and `/api/v1/demo/checkout` functional and reject resets during nonterminal incidents.
    - Two distinct smoke-checkout CLI runs succeeded with separate keys and logged structured entries with zero secret leaks.
  - **Next action:** P0 Review-and-Fix gate subagent (completed, verdict PASS in commit `04c7c7a`).

- **IL-T04 (2026-10-14):**
  - **Commit:** `feat: add observable inventory configuration fault`
  - **Files:** `data/runbooks/inventory.md`, `src/incidentlab/api.py`, `src/incidentlab/cli.py`, `src/incidentlab/schemas.py`, `tests/test_sandbox_flow.py`.
  - **Commands executed:**
    - `uv run pytest tests/test_sandbox_flow.py -m 'not integration'` (exit 0, 1 passed, 9 deselected)
    - `docker compose -p incidentlab-test exec -T control python -m incidentlab.cli seed --family inventory_config --seed 1` (exit 0, pre-injection checkout fulfilled, injected family `inventory_config`, revision 33)
    - `docker compose -p incidentlab-test exec -T control python -m incidentlab.cli smoke-checkout` (exit 1, failed with `[inventory_unavailable]`)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/` (exit 0, 26 passed)
    - `uv run ruff check .` (exit 0, all checks passed)
  - **Observed facts:**
    - Operational runbook `data/runbooks/inventory.md` created with required sections (`symptoms`, `diagnostics`, `repair`, `verification`, `escalation`), under 200 tokens each, and free of scenario gold labels.
    - Fault injection `inventory_config` sets target `endpoint_key` to `inventory-legacy` under row lock and advances revision.
    - Subsequent checkout fails with 503 `inventory_unavailable` without inserting an order into `sandbox.orders`.
    - Diagnostics (`/diagnostics/config`, `/diagnostics/logs`, `/diagnostics/probe`) expose operational failure state without leaking the scenario label `inventory_config`.
    - Owner demo endpoint `POST /api/v1/demo/fault` verifies owner bearer token, refuses mutation while an incident is nonterminal, and forwards to sandbox operator route.
    - CLI `seed` subcommand generates pre-injection healthy traffic before applying fault to demonstrate log filtering.
  - **Next action:** IL-T05 (connect read-only MCP and keyword retrieval) completed. Next action: IL-T06 (persist bounded agent reasoning and an immutable proposal).

- **IL-T05 (2026-10-14):**
  - **Commit:** `feat: add read-only MCP evidence and runbook retrieval`
  - **Files:** `src/incidentlab/mcp_server.py`, `src/incidentlab/mcp_client.py`, `src/incidentlab/retrieval.py`, `src/incidentlab/cli.py`, `tests/test_mcp_boundary.py`, `tests/test_retrieval.py`, `data/runbooks/worker.md`, `data/runbooks/upstream.md`, `tests/conftest.py`, `tests/test_sandbox_flow.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `uv run pytest tests/test_mcp_boundary.py tests/test_retrieval.py -m 'not integration'` (exit 0, 3 passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_mcp_boundary.py tests/test_retrieval.py` (exit 0, 5 passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local python -m incidentlab.cli index-runbooks` (exit 0, 15 sections indexed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 31 passed)
  - **Observed facts:**
    - Read-only MCP server registers exactly four tools: `get_health`, `get_logs`, `get_config`, `probe_checkout`.
    - Child process environment strictly receives read authority only (`IL_SANDBOX_READ_TOKEN`, `IL_SANDBOX_BASE_URL`), with write/admin/db tokens excluded.
    - Stdio MCP client transport validates allowed tool names, bounds arguments, executes tools over JSON-RPC stdio streams, and wraps results into typed `EvidenceItem` records.
    - PostgreSQL full-text search indexes 15 runbook sections across 3 runbooks into `control.runbook_chunks` and returns top 3 ranked citations using `ts_rank_cd`.
    - CLI `index-runbooks` command successfully indexes runbook corpus.
  - **Next action:** IL-T06 (persist bounded agent reasoning and an immutable proposal) completed. Next action: Phase P1 Review-and-Fix gate subagent, then advance to IL-T07.

- **IL-T06 (2026-10-14):**
  - **Commit:** `feat: persist bounded agent investigation and proposals`
  - **Files:** `src/incidentlab/context.py`, `src/incidentlab/graph.py`, `src/incidentlab/api.py`, `src/incidentlab/events.py`, `src/incidentlab/schemas.py`, `tests/test_agent_contract.py`, `tests/conftest.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_agent_contract.py -m 'not live_model and not integration'` (exit 0, 3 passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_agent_contract.py -k test_live_investigation_inventory_fault` (exit 0, 1 passed in 256.80s)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 34 passed)
    - `docker compose -p incidentlab-test exec -T control python -m incidentlab.cli smoke-checkout` (exit 0, fulfilled in 0.33s)
  - **Observed facts:**
    - Context assembly bounded to token budget with truncation and strict error handling (`context_budget_exceeded`).
    - Investigation graph executes under bounded turns, reading evidence via read-only MCP and retrieving cited runbook guidance.
    - Proposal generated with canonical SHA-256 digest `payload_digest`, exact target revision snapshot, and citation IDs.
    - Incident transitions to `awaiting_approval` without applying any target mutation (zero repair effect during investigation, target revision unchanged).
    - Database isolation preserved: control role never queries sandbox schema directly; evidence obtained exclusively through MCP.
    - Autouse integration test fixture ensures no lingering active incidents block subsequent runs.
  - **Next action:** Phase P1 Review-and-Fix gate subagent (passed). Next action: IL-T07 (bound owner approval and one execution claim).

- **IL-T07 (2026-10-14):**
  - **Commit:** `feat: bind owner approval to immutable repair claims`
  - **Files:** `src/incidentlab/approvals.py`, `src/incidentlab/schemas.py`, `src/incidentlab/api.py`, `tests/test_approval.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_approval.py -v` (exit 0, 9 passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 43 passed)
    - `docker compose -p incidentlab-test exec -T control python -m incidentlab.cli smoke-checkout` (exit 0, fulfilled in 0.33s)
  - **Observed facts:**
    - Owner decision transaction runs under PostgreSQL row-level locks on incident and proposal records.
    - Proposal constraints strictly validated: state must be `awaiting_approval`, proposal not expired, proposal not cancelled, target revision match, and stored payload digest match recomputed canonical SHA-256 digest.
    - Submitting `approve` transitions incident state to `approved` and records `approval_recorded` event.
    - Identical duplicate decisions return idempotent readback; conflicting decisions (approve vs reject) raise 409 conflict error.
    - Decision `reject` sets state to `rejected` and produces exactly zero execution claims.
    - Execution claims enforce exactly one claim per proposal (`UNIQUE(proposal_id)` in `control.executions`). Precheck queries current sandbox target revision before dispatching.
    - Async HTTP I/O (`check_target_revision`) performed outside database transaction to prevent `idle in transaction` lock contention.
- **IL-T08 (2026-10-14):**
  - **Commit:** `feat: execute approved repair and verify functional recovery`
  - **Files:** `src/incidentlab/schemas.py`, `src/incidentlab/sandbox/api.py`, `src/incidentlab/executor.py`, `src/incidentlab/verifier.py`, `src/incidentlab/approvals.py`, `src/incidentlab/api.py`, `src/incidentlab/cli.py`, `tests/test_execution.py`, `tests/test_approval.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm -v $(pwd)/src:/app/src -v $(pwd)/tests:/app/tests --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_execution.py` (exit 0, 8 passed)
    - `docker run --rm -v $(pwd)/src:/app/src -v $(pwd)/tests:/app/tests --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 51 passed, 1 deselected)
    - `docker run --rm -v $(pwd)/src:/app/src -v $(pwd)/tests:/app/tests --network incidentlab-test_default --env-file .env -e IL_MODEL_TIMEOUT_SECONDS=300 incidentlab-eval:local python -m incidentlab.cli smoke-demo --family inventory_config --approval approve` (exit 0, real Ollama CPU investigation, proposal generation, owner approval, single repair dispatch with receipt result=applied, and independent checkout verification verdict=pass)
  - **Observed facts:**
    - `execute_claim` validates payload allowlist (`set_inventory_endpoint`), pre-dispatch cancellation, and records `dispatching` state before issuing exactly one HTTP POST.
    - Zero automatic repair retries: errors/timeouts transition to `unknown` and trigger read-only receipt lookup via `reconcile_execution` (`GET /repairs/{execution_key}`).
    - Sandbox locks target, validates expected revision and payload digest, updates config, and commits durable repair receipt in one atomic transaction.
    - Verification executes via `verify_checkout`: submits a brand new fake order via `submit_checkout`, polls up to 10 seconds at 250ms intervals, verifies `fulfilled` status, appends `verification` kind evidence to `control.evidence`, and transitions incident to `verified_recovered` on pass or `verification_failed` on fail.
    - `green_health_is_not_recovery` verified: HTTP 200 health check on broken target does not satisfy recovery.
- **IL-T09 (2026-10-14):**
  - **Commit:** `feat: deliver first browser-based approved recovery demo`
  - **Files:** `frontend/package.json`, `frontend/package-lock.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/index.html`, `frontend/src/App.tsx`, `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/styles.css`, `frontend/src/main.tsx`, `tests/test_end_to_end.py`, `src/incidentlab/api.py`, `Dockerfile`, `compose.yaml`, `README.md`, `tests/conftest.py`.
  - **Commands executed:**
    - `npm --prefix frontend ci` (exit 0, dependencies cleanly installed)
    - `npm --prefix frontend run typecheck` (exit 0, zero TypeScript errors)
    - `npm --prefix frontend run build` (exit 0, production bundle built with Vite)
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm -v $(pwd)/src:/app/src -v $(pwd)/tests:/app/tests --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 51 passed, 2 deselected)
    - `uv run pytest tests/test_end_to_end.py -m live_model -s` (exit 0, 1 passed in 685s against local lab http://127.0.0.1:8000)
  - **Observed facts:**
    - React 18/TypeScript/Vite single-page owner console built into `frontend/dist` and served statically by FastAPI at root `/` after `/api/v1` routes.
    - Owner authentication token is maintained exclusively in React component memory; never stored in `localStorage`, cookies, or logs (IL-11).
    - Polling loop drains ordered audit events via `GET /api/v1/incidents/{id}/events?after_seq=N` every 1s during active investigation/execution.
    - Evidence inspector allows owner to inspect exact tool observations and retrieved versioned runbook sections with sanitized payloads.
    - Approval panel displays immutable proposal ID, action (`set_inventory_endpoint`), endpoint key (`inventory-local`), target revision, payload digest, and countdown timer.
    - Full end-to-end recovery verified: healthy checkout fulfilled -> fault injected (`inventory_config`) -> checkout failure observed -> agent investigation diagnosed root cause -> owner approved proposal -> deterministic repair applied -> independent verifier checkout fulfilled brand-new order (`verdict="pass"`).
    - Full end-to-end rejection verified: fault injected -> checkout failure observed -> agent investigation -> owner rejected proposal -> incident state transitioned to `rejected` -> reconcile verified exactly zero execution claims created (`404 execution_not_found`).
  - **Next action:** Phase P2 Review-and-Fix gate subagent audit (completed, verdict PASS in commit `2b7412b`).

- **IL-T10 (2026-10-14):**
  - **Commit:** `feat: complete worker recovery and maintenance escalation` (`e9abe58`)
  - **Files:** `src/incidentlab/sandbox/service.py`, `src/incidentlab/sandbox/api.py`, `src/incidentlab/executor.py`, `src/incidentlab/graph.py`, `src/incidentlab/cli.py`, `frontend/src/App.tsx`, `tests/test_execution.py`, `tests/test_sandbox_flow.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm -v $(pwd)/src:/app/src -v $(pwd)/tests:/app/tests --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_execution.py tests/test_sandbox_flow.py` (exit 0, 22 passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local python -m incidentlab.cli smoke-demo --family worker_paused --approval approve` (exit 0, live Ollama Qwen3:8B diagnosed paused worker, proposed `resume_order_worker`, executed repair, and verified recovery with newly fulfilled checkout order)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local python -m incidentlab.cli smoke-demo --family upstream_maintenance --approval interactive` (exit 0, diagnosed upstream maintenance, escalated with repair=None and zero repair dispatch)
  - **Observed facts:**
    - Allowlist extended to exactly the two allowed repairs: `set_inventory_endpoint` and `resume_order_worker`.
    - `worker_paused` fault injection stops worker fulfillment while checkout 202 is accepted; recovery via `resume_order_worker` resumes worker loop and successfully fulfills stalled order.
    - `upstream_maintenance` fault injection produces 503 HTTP errors; agent diagnoses maintenance and escalates without proposing a repair action (`repair=None`), generating zero execution claims or receipts.

- **IL-T11 (2026-10-14):**
  - **Commit:** `feat: validate runbook grounding and bound agent context` (`76afdb3`)
  - **Files:** `evals/cases/dev.jsonl`, `evals/README.md`, `tests/test_agent_contract.py`, `tests/test_retrieval.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_agent_contract.py tests/test_retrieval.py` (exit 0, 6 passed)
    - `docker build -t incidentlab-eval:local -f Dockerfile .` (exit 0, successfully built)
  - **Observed facts:**
    - 12 development cases frozen in `evals/cases/dev.jsonl` (4 per fault family: clean, noisy, missing, conflicting).
    - `evals/README.md` created documenting dataset partitioning, synthetic recipe schemas, and grounding constraints.
    - `test_context_keeps_required_evidence_within_budget` asserts assembled investigation context stays strictly under 6,000 tokens while preserving approval-critical evidence.
    - `test_chunk_token_bounds` verifies every runbook section across all 3 operational runbooks is strictly `<200` tokens using qualified tokenizer.

- **IL-T12 (2026-10-14):**
  - **Commit:** `fix: make approval and execution recovery durable` (`3b75e50`)
  - **Files:** `src/incidentlab/api.py`, `src/incidentlab/cli.py`, `src/incidentlab/executor.py`, `src/incidentlab/graph.py`, `tests/test_approval.py`, `tests/test_recovery.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_recovery.py tests/test_approval.py tests/test_execution.py -m 'not live_model'` (exit 0, 29 passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 66 passed, 2 deselected)
    - `npm --prefix frontend run typecheck && npm --prefix frontend run build` (exit 0, Vite bundle built)
  - **Observed facts:**
    - Startup reconciliation (`reconcile_startup`) reconstructs waiting proposals without model rerun or repair dispatch; marks expired proposals if past TTL.
    - Ack loss crash safety proven: `CrashHarness.apply_then_restart_before_ack()` injected pause/crash after real sandbox transaction commit; recovery via `reconcile_execution` GET confirmed `repair_post_count == 1` with `receipt.result == "applied"` without replaying mutation POST.
    - Missing receipt handling proven: unreached execution key results in `execution_unknown` state, blocking new incidents via `ix_one_active_incident` unique constraint (409 conflict).
    - Narrow operator procedure `resolve-unknown --incident <UUID>` implemented and proven: verifies reachability, checks durable receipt, requires quiesce confirmation, non-empty note, and exact typed acknowledgment `ACKNOWLEDGE UNKNOWN <execution-key>`, commits `closed_unknown` with `effect_unknown=true`, records event `execution.closed_unknown`, and releases one-active-incident lock with zero sandbox repair replay.
  - **Phase P3 Review & Fix Audit (2026-10-14):**
    - Subagent: `92124d4f-b31a-472f-a562-94aef60bf5aa`
    - Tasks audited: IL-T10, IL-T11, IL-T12
    - Verification commands passed:
      - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (66 passed, 2 deselected in 9.17s)
      - `npm --prefix frontend ci` (clean, exit 0)
      - `npm --prefix frontend run typecheck` (zero errors, exit 0)
      - `npm --prefix frontend run build` (production bundle built, exit 0)
      - `~/.local/bin/uv run ruff check .` (all checks passed, exit 0)
    - Fix applied: Updated `tests/test_retrieval.py` (`test_chunk_token_bounds`) to assert strictly `< 200` tokens across all 15 runbook sections (commit `3cb2a0a`).
    - Audit Verdict: **PASS**. P3 gate passed. Transitioning to Phase P4.

- **IL-T13 (2026-10-14):**
  - **Commit:** `eval: add isolated runner, scorer, and frozen splits` (`f1c1827`)
  - **Files:** `evals/cases/heldout.jsonl`, `evals/gold/dev.jsonl`, `evals/gold/heldout.jsonl`, `evals/manifest.json`, `evals/score.py`, `evals/run.py`, `evals/README.md`, `tests/test_evaluation.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_evaluation.py -v` (exit 0, 8 passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 74 passed, 2 deselected)
  - **Observed facts:**
    - 12 development cases (`dev.jsonl`) and 18 held-out cases (`heldout.jsonl`) frozen with SHA-256 hashes in `evals/manifest.json`.
    - Held-out family counts strictly match: `{"inventory_config": 6, "worker_paused": 6, "upstream_maintenance": 6}`.
    - Held-out split status strictly marked `not_run`.
    - Deterministic scorer `evals/score.py` rejects claimed recovery without verifier fulfilled order (`fabricated_verification`), unauthorized repair (`unauthorized_repair`), and incorrect repair (`incorrect_repair`).
    - Isolated runner `evals/run.py` drives evaluation scenarios with clean target resets, independent verifiers, and structured artifact generation.

- **IL-T14 (2026-10-14):**
  - **Commit:** `eval: compare fixed diagnostics and adaptive investigation` (`804a527`)
  - **Files:** `evals/baseline.py`, `evals/run.py`, `evals/score.py`, `src/incidentlab/events.py`, `src/incidentlab/graph.py`, `src/incidentlab/model.py`, `evals/README.md`, `README.md`.
  - **Commands executed:**
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run python -m evals.run --split dev --approach fixed` (exit 0, 11/12 passed [91.67%], 100% relevant-section coverage, 100% citation validity, 0 unauthorized repairs, 0 fabricated verifications)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run python -m evals.run --split dev --approach agent` (exit 0, 9/12 passed [75.0%], 100% relevant-section coverage, 83.33% citation validity, 0 unauthorized repairs, 0 fabricated verifications)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run python -m evals.score --compare --fixed artifacts/evals/dev_fixed.json --agent artifacts/evals/dev_agent.json --output artifacts/evals/dev_report.json` (exit 0, generated paired development report)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 74 passed, 2 deselected)
    - `uv run ruff check .` (exit 0, all checks passed)
  - **Observed facts:**
    - Fixed diagnostic baseline always executes the exact 4 read tools (`get_health` -> `get_logs` -> `get_config` -> `probe_checkout`), keyword retrieval top-3, and 1 final schema call.
    - Paired evaluation on all 12 development cases demonstrated honest comparison: baseline achieved 11/12 (91.67%) vs agent 9/12 (75.0%), showing adaptive agent diagnoses from initial reads without follow-up reads (0.58 avg tool calls vs 3.75 fixed) but incurs higher token usage (1,953 avg vs 827) and longer active duration (343.3s vs 104.4s).
    - Top-3 keyword retrieval achieved 100% relevant-section coverage across all 12 dev cases for both approaches.
    - Hard safety gates strictly satisfied across all runs: 0 unauthorized mutations and 0 fabricated verifications.

- **IL-T15 (2026-10-14):**
  - **Commit:** `ci: enforce focused offline integrity checks` (`1f535ee`)
  - **Files:** `.github/workflows/ci.yml`, `tests/test_agent_contract.py`, `tests/test_mcp_boundary.py`.
  - **Commands executed:**
    - `uv run ruff check .` (exit 0, all checks passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 77 passed, 2 deselected in 11.01s)
    - `npm --prefix frontend run typecheck && npm --prefix frontend run build` (exit 0, clean build)
  - **Observed facts:**
    - `.github/workflows/ci.yml` defines a single lean offline CI workflow running Ruff, frontend typecheck, Vite build, database migrations, sandbox background service, and offline pytest (`-m 'not live_model'`) against PostgreSQL service container.
    - Essential assertion proven: adversarial prompt injection in logs/runbooks cannot add tools to MCP, cannot disclose secret tokens, and an untrusted/compromised model cannot execute repairs without bound owner approval.
    - Docker copy boundaries verified: evaluation image includes tests/evals, runtime image excludes test/eval trees, `.dockerignore` excludes secrets/artifacts/caches.
  - **Phase P4 Review & Fix Audit (2026-10-14):**
    - Subagent: `6d51f7c7-6f0f-482a-b2eb-0942ce69f7d2`
    - Tasks audited: IL-T13, IL-T14, IL-T15
    - Verification commands passed:
      - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (77 passed, 2 deselected, 1 warning in 10.98s)
      - `npm --prefix frontend ci` (audited 70 packages, exit 0)
      - `npm --prefix frontend run typecheck` (zero errors, exit 0)
      - `npm --prefix frontend run build` (production bundle built in 301ms, exit 0)
      - `~/.local/bin/uv run ruff check .` (all checks passed, exit 0)
    - Audit findings:
      - IL-T13: Frozen recipes in `evals/cases/dev.jsonl` (12 cases) and `evals/cases/heldout.jsonl` (18 cases, 6 per family: 6 inventory_config, 6 worker_paused, 6 upstream_maintenance) with 1-to-1 gold specifications in `evals/gold/` strictly match SHA-256 hashes in `evals/manifest.json`. Deterministic scorer `evals/score.py` verified to check authority binding, allowed actions, verifier fulfilled-order evidence, and citation existence/validity; rejects fabricated verifications, unauthorized repairs, and incorrect repairs. Isolated runner `evals/run.py` resets target cleanly and executes scenarios without leaking gold or fault-labels. Held-out split strictly marked `not_run`.
      - IL-T14: Fixed diagnostic baseline in `evals/baseline.py` executes exact 4 reads (`get_health` -> `get_logs` -> `get_config` -> `probe_checkout`), top-3 keyword retrieval, and 1 final schema call without fault-label shortcuts. Paired comparison CLI `--compare` in `evals/score.py` executed across all 12 dev cases (`artifacts/evals/dev_report.json`) reporting honest comparisons: 11/12 fixed (91.67%) vs 9/12 agent (75.0%), 100% relevant-section coverage, token metrics, and strict 0 unauthorized repairs and 0 fabricated verifications.
      - IL-T15: `.github/workflows/ci.yml` defines one lean offline CI job running Ruff, frontend typecheck, Vite build, database migrations, background sandbox service, and offline pytest (`-m 'not live_model'`) against PostgreSQL service container with no live model or paid inference calls. Security/integrity regressions pass offline (secret/tool boundary, prompt injection in logs, unauthorized repair refusal, verifier honesty, evaluation provenance). Docker COPY boundaries verified (eval image includes tests/evals, runtime image excludes test/eval trees, `.dockerignore` excludes secrets/artifacts/caches).
- **IL-T16 (2026-10-16):**
  - **Commit:** `perf: measure local inference quality and latency` (`b8cc191`)
  - **Files:** `evals/benchmark.py`, `src/incidentlab/model.py`, `src/incidentlab/events.py`, `evals/README.md`, `README.md`.
  - **Commands executed:**
    - `docker run --rm --network incidentlab-test_default --env-file .env -v $(pwd)/src:/app/src -v $(pwd)/evals:/app/evals -v $(pwd)/artifacts:/app/artifacts incidentlab-eval:local python -m evals.benchmark --split dev --sample representative-six --warm-repeats 3` (exit 0)
    - Recomputed throughput sample: `199 tokens / 31.56s = 6.31 tokens/s` matching provider metric.
    - `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 77 passed, 2 deselected)
    - `uv run ruff check .` (exit 0, all checks passed)
  - **Observed facts:**
    - Concurrency 1 monotonic timers, provider durations, streaming TTFT, memory sampling at 0.1s interval.
    - Cold-load observation + 18 warm observations (3 repeats of 6 representative development inputs) recorded in `artifacts/benchmarks/dev_benchmark.json` and `artifacts/benchmarks/bm-1792168104/`.
    - Active Wall-Clock: p50 = 34.66s, p95 = 48.80s, max = 52.45s (target < 240s, 100% compliant).
    - Model Generation Duration: p50 = 33.36s, p95 = 36.10s, avg = 32.59s.
    - Output Throughput: p50 = 6.24 tokens/s, p95 = 6.31 tokens/s (CPU compute).
    - Streaming TTFT: p50 = 0.26s, p95 = 0.28s (timed on first non-empty content chunk).
    - Paid Inference API Calls: 0 ($0.00, 100% local hardware).
    - Peak RAM: 14.9 GB (Host RAM: 125 GiB).

- **IL-T17 (2026-10-16):**
  - **Commit:** `feat: clarify evidence approval and recovery outcomes` (`45f7794`)
  - **Files:** `frontend/src/App.tsx`, `frontend/src/types.ts`, `frontend/src/styles.css`, `tests/test_api.py`.
  - **Commands executed:**
    - `npm --prefix frontend run typecheck && npm --prefix frontend run build` (exit 0, clean build in 308ms)
    - `docker run --rm --network incidentlab-test_default --env-file .env -v $(pwd)/src:/app/src -v $(pwd)/tests:/app/tests incidentlab-eval:local uv run pytest tests/test_api.py` (exit 0, 12 passed)
    - `docker run --rm --network incidentlab-test_default --env-file .env -v $(pwd)/src:/app/src -v $(pwd)/tests:/app/tests incidentlab-eval:local uv run pytest -m 'not live_model'` (exit 0, 82 passed, 2 deselected in 10.23s)
    - `uv run ruff check .` (exit 0, all checks passed)
  - **Observed facts:**
    - Resilient read-only polling: network interruption or transient error preserves prior evidence and events without replaying mutations.
    - Keyboard accessibility: Escape key closes evidence modal; focus-visible indicators; aria labels on interactive elements.
    - Derived telemetry display: model duration, tool execution duration/reads, throughput (tokens/s), and proposal approval wait duration.
    - Token-free copy handler: `handleCopySummary` copies audit records, diagnosis, citations, and receipt to clipboard with zero credentials or tokens exposed.
    - Explicit actionable banners for all 9 states: `verified_recovered`, `verification_failed`, `escalated`, `rejected`, `cancelled`, `expired`, `failed`, `execution_unknown` (read-only reconcile + operator CLI command displayed), and `closed_unknown`.
    - Unit tests confirm cancellation transitions, reconcile 404 handling, and token absence from error responses and events.

- **IL-T18 (2026-10-16):**
  - **Commit:** `docs: verify clean setup and publish demo guide` (`9408bb6`)
  - **Files:** `README.md`, `.env.example`, `compose.yaml`, `src/incidentlab/cli.py`, `src/incidentlab/db.py`.
  - **Commands executed:**
    - Rehearsed clean setup in separate Compose project `incidentlab-clean` with fresh named volumes and freshly generated local secrets.
    - Schema migrations executed: `docker compose -p incidentlab-clean --env-file .env run --rm -v $(pwd)/src:/app/src -e IL_MIGRATION_DATABASE_URL control uv run python -m incidentlab.cli migrate` (exit 0).
    - Runbooks indexed: `docker compose -p incidentlab-clean --env-file .env run --rm -v $(pwd)/src:/app/src control uv run python -m incidentlab.cli index-runbooks` (exit 0, 15 sections indexed).
    - Services started: `docker compose -p incidentlab-clean --env-file .env up -d` (exit 0).
    - Healthy checkout verified: `docker compose -p incidentlab-clean exec -T control python -m incidentlab.cli smoke-checkout` (exit 0, fulfilled in 0.389s).
    - Fault injected and full recovery demo completed: `docker compose -p incidentlab-clean exec -T control python -m incidentlab.cli smoke-demo --family inventory_config --approval approve` (exit 0, real Ollama CPU inference, proposal generated, approved, single repair dispatched, verified new fulfilled checkout order `9ffaea50-3e5b-4443-83d9-343b089c9a80`).
    - Teardown: `docker compose -p incidentlab-clean down -v` and network cleanup (exit 0).
    - Documentation updated with prerequisites, licence/model pin, known limits, short demo, and concise architecture notes.
    - `git diff --check` and `uv run ruff check .` passed with 0 errors.

- **Next action:** Invoke Phase P5 Review-and-Fix subagent to audit IL-T16, IL-T17, and IL-T18 before gate transition to Phase P6.


At IL-T20 add the IL-01–IL-14 acceptance evidence mapping here. Until then all requirements are **not verified**. The target 15/18 is a criterion, not a result. Provided lab hardware details are a capability description, not an observed GPU benchmark.

## Resume procedure

1. Read `AGENTS.md` and `PRD.md`, then the gate and first incomplete task here.
2. Read that task in `ROADMAP.md` and its `ARCHITECTURE.md` contracts; inspect actual code and last verified commit.
3. Confirm prerequisite evidence and phase authorization. Prepare the concrete review if absent; do not assume approval.
4. Execute only the bounded task. Ask briefly whether it can be simpler without losing required behavior, and follow the minimum checks rather than creating another review loop.
5. Update this ledger/docs, commit the meaningful checkpoint, and state the exact next action. At phase end stop for review; after accepted completion stop building.

