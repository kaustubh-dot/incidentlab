# IncidentLab implementation plan

> For future implementing agents: use the `executing-plans` workflow, task by task. Read `AGENTS.md`, `PRD.md`, `ARCHITECTURE.md`, `EVALUATION.md`, and `TASKS.md` first. Optional delegation is for substantial independent work only; there is no subagent-per-test process. The present deliverable is documentation only. All implementation tasks and phase approvals are pending.

**Goal:** A live local agent investigates an actual sandbox checkout failure, proposes a bounded repair, obtains owner approval, applies it deterministically, and independently proves recovery with a new fulfilled order.

**Architecture:** One FastAPI control application and one tiny separate sandbox process in the same repository; one PostgreSQL instance and one Ollama provider. One LangGraph agent uses read-only MCP and retrieved runbooks. Server-owned approval, execution, and verification control effects.

**Stack:** Python 3.12, FastAPI, Pydantic 2, psycopg 3, PostgreSQL 16/pgvector, LangGraph PostgreSQL checkpointer, official MCP Python SDK, Ollama AsyncClient, sentence-transformers, React/TypeScript/Vite, Docker Compose. Qualify and lock compatible versions in P0; Node.js 24 LTS is the frontend baseline.

**Spec:** `PRD.md` owns vision, `ARCHITECTURE.md` owns interfaces, `EVALUATION.md` owns measurement, and `TASKS.md` alone owns actual status and approvals. Planned commands and tests below have not been executed.

## Global execution contract

- Seven sequential phases, P0–P6, with three meaningful tasks each, IL-T01–IL-T21. Every task links PRD IDs. A dependent phase requires verified predecessor evidence and recorded explicit user approval.
- Before each task inspect actual files and the last checkpoint. Follow the named interfaces; do not invent actions, tools, providers, routes, services, fault families, or authorities.
- Implement one focused behavior at a time. Use a failing behavior check where useful, then the minimum implementation and named verification. Reuse the essential regression groups; no test-count or coverage quota.
- Check off a step only after it was executed. Record commands, outcomes, artifact locations, significant decisions, and exact next action in `TASKS.md`. File existence alone proves no application behavior.
- At each meaningful task checkpoint, stage only its explicit files and synchronized docs, inspect `git diff --cached`, run `git diff --cached --check`, and commit with the listed message. No blanket staging or required PR per task. Use a phase feature branch when review isolation is useful.
- At every phase end, show the working deliverable and verification, update the ledger, commit, and **STOP for user review and explicit next-phase approval**. User silence or documentation approval does not authorize application implementation.
- The model never receives credentials, hidden fixture IDs/gold, shell, SQL, file tools, arbitrary URLs, or a repair tool. No automatic mutation replay or provider fallback. The verifier owns recovery truth.
- Stop when the task's mapped acceptance and minimum verification pass. Additions need an active PRD requirement or an observed defect. Do not add a coverage quota, full-suite rerun per task, repeated review cycles, serving matrix, extra service, or speculative abstraction.

## Setup and command conventions

Commands are future Linux-lab commands from the repository root. They become available only when the named task creates their files. `uv sync --locked --extra dev` requires IL-T01's lockfile. No command may silently seed answers, approve a live demo, or reset an unrelated database.

The planned CLI entry points are `qualify`, `migrate`, `smoke-checkout` (P0); `seed --family inventory_config --seed 1` and `index-runbooks --mode keyword` (P1); `smoke-demo --family inventory_config --approval interactive` (P2); the additional finite families, `compare-retrieval --split dev`, and `resolve-unknown --incident <UUID>` (P3). P4 adds `evals.run`/`evals.score`; P5 adds `evals.benchmark`. Each prints machine-readable results to stdout and operational diagnostics to stderr; failures use nonzero exit status.

After IL-T01, copy `.env.example` to ignored `.env`:

```bash
cp .env.example .env
chmod 600 .env
```

The owner uses a trusted local password-manager/token generator or local setup tool to place distinct generated values directly into `.env`, without printing values into chat, shell history, or agent command logs. Use at least 32 random bytes encoded as hex for owner/read/write/admin tokens and database passwords. Populate blank DSNs with the matching local secrets: `IL_DATABASE_URL=postgresql://incidentlab_control:<control-password>@postgres:5432/incidentlab`, `IL_SANDBOX_DATABASE_URL=postgresql://incidentlab_sandbox:<sandbox-password>@postgres:5432/incidentlab`, and `IL_MIGRATION_DATABASE_URL=postgresql://incidentlab_migrator:<migration-password>@postgres:5432/incidentlab`. Angle-bracket text describes substitution, not literal credentials. Hex passwords avoid URL-escaping ambiguity. `POSTGRES_PASSWORD` matches the migration password; role bootstrap passwords match their DSNs. No supplied lab connection details belong here.

For one-off migration, load only the owner's trusted local `.env` into the shell:

```bash
set -a
. ./.env
set +a
```

Normal service environments explicitly exclude migration/bootstrap variables. Pass them only to migration. Initial startup after IL-T03 is:

```bash
uv sync --locked --extra dev
docker compose --profile gpu build control sandbox
docker compose --profile gpu up -d postgres ollama
docker compose exec ollama ollama pull qwen3:8b
docker compose run --rm -e IL_MIGRATION_DATABASE_URL -e IL_CONTROL_DB_PASSWORD -e IL_SANDBOX_DB_PASSWORD control python -m incidentlab.cli migrate
docker compose --profile gpu up -d sandbox control
docker compose exec -T control python -m incidentlab.cli smoke-checkout
```

`migrate` checks PostgreSQL readiness with a bounded 30-second wait and actionable error. It does not require the HTTP service to start against an uninitialized schema. The Compose control service binds `0.0.0.0` internally but publishes only `127.0.0.1:8000` on the host. P1 adds `docker compose exec -T control python -m incidentlab.cli index-runbooks --mode keyword`; later use the selected mode. Record verified image/package/model digests at qualification rather than assuming mutable tags are reproducible.

Offline local command: `uv run pytest -m 'not live_model and not integration'`. Transaction tests use an isolated test database and actual sandbox. No fixture resets the owner's running demo. Live E2E is explicitly marked `live_model`; it is not a normal PR requirement.

The Dockerfile supplies a transient `evaluation` build target starting at IL-T02 for the current focused tests. It contains tests and any existing synthetic evaluation data; IL-T13 adds the full runner/scorer/frozen sets to this same target. The normal runtime target excludes these; `.dockerignore` excludes secrets/artifacts globally without preventing the evaluation target from copying its own fixtures. This is not another always-running service. `compose.yaml` sets `name: incidentlab`; `-p incidentlab-test` creates a separate database/volumes and private network `incidentlab-test_default`. Normal demo network is `incidentlab_default`.

At IL-T02 build `control` plus the evaluation target and run role/API transaction checks without exposing PostgreSQL to the host:

```bash
docker compose -p incidentlab-test build control
docker build --target evaluation -t incidentlab-eval:local .
docker compose -p incidentlab-test up -d postgres
docker compose -p incidentlab-test run --rm --no-deps -e IL_MIGRATION_DATABASE_URL -e IL_CONTROL_DB_PASSWORD -e IL_SANDBOX_DB_PASSWORD control python -m incidentlab.cli migrate
docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_api.py -m integration
```

IL-T03 adds `docker compose -p incidentlab-test up --build -d sandbox`; subsequent focused integration commands use that network and only their named test files. Each project has its own database because `postgres` resolves within its network; identical DSN hostnames do not point to the demo's database. Do not run a normal control service in the test project. Tests use the same application in-process and real sandbox HTTP. `docker compose -p incidentlab-test down` stops test services without deleting volumes.

For P4 onward, full owner-run evaluation can use the demo network only after stopping its control service, or the same isolated test project with its Ollama profile. The default documented evaluation commands are:

```bash
docker build --target evaluation -t incidentlab-eval:local .
docker compose stop control
docker run --rm --network incidentlab_default --env-file .env incidentlab-eval:local uv run python -m evals.run --split dev --approach agent
docker compose --profile gpu up -d control
```

The evaluation runner starts the same FastAPI app with its ASGI lifespan and authenticated httpx ASGI requests while real sandbox, PostgreSQL, MCP, and Ollama remain active. It owns the sole control process. Test-only dependency injection supplies evidence filters/timed operator changes in `evals/run.py`, never in a production API/model tool. Its MCP child still receives only read authority. Capture JSON stdout in ignored `artifacts/`; do not mount the host filesystem into application containers. For browser E2E use normal control, not a concurrently running evaluator.

## P0 — Qualify the lab and fulfill a healthy checkout

**Objective/deliverable:** Reproducible runtime, observed model capabilities, and an actual fulfilled fake order. **PRD:** IL-01, IL-05, IL-07, IL-09–11, IL-13–14. **Prerequisites:** Explicit P0 approval and authorized lab access outside Git. Hardware description is not qualification. **Components:** IL-T01–03. **Exit:** Provider capability facts, role separation, owner auth, and healthy checkout proof. This phase includes a working application transition.

### IL-T01 — Qualify and pin the minimum runtime

**PRD:** IL-05, IL-11, IL-13, IL-14. **Depends on:** P0 start approval. **Files:** Create `pyproject.toml`, `uv.lock`, `Dockerfile`, `compose.yaml`, `.dockerignore`, `src/incidentlab/{__init__.py,config.py,model.py,cli.py}`, `tests/test_model_contract.py`.

**Interfaces:** `Settings.from_mapping(values:dict[str,str])->Settings` matches environment validation; `call_model(messages,tools,final_schema)->ModelResult` follows architecture; `qualify` emits hardware/runtime/model/digest/licence/tool/schema/tokenizer facts and errors. No DB is required for this probe. Provider calls explicitly use qualified `think=false` for bounded tool/JSON mode if supported.

- [ ] Inspect `python3 --version`, `docker version`, `docker compose version`, and `nvidia-smi`; record observed RAM/VRAM/driver/runtime, including unavailable capabilities.
- [ ] Define and lock minimum runtime/dev dependencies (`pytest`, `pytest-asyncio`, Ruff, qualified tokenizer support). Build non-root images with no privileged mode, host mount, or Docker socket. Validate required secrets and distinct tokens.
- [ ] Implement actual native tool/schema probes and tokenizer/template accounting; record exact model digest, quantization, licence, Ollama version, GPU use, and thinking-mode behavior. A static JSON sample is not proof of native tool calling.
- [ ] Run `uv lock`, `uv sync --locked --extra dev`, and `uv run pytest tests/test_model_contract.py -m 'not live_model'`; start PostgreSQL/Ollama, pull the candidate, and run `docker compose run --rm control python -m incidentlab.cli qualify`. Capture `artifacts/qualification.json`.

**Essential example:**

```python
def test_required_owner_secret_is_not_defaulted(settings_values):
    settings_values["IL_OWNER_TOKEN"] = ""
    with pytest.raises(ValueError, match="IL_OWNER_TOKEN"):
        Settings.from_mapping(settings_values)
```

**Expected/minimal verification:** Config test passes; real probe records supported capabilities or an exact failure. Unqualified model/tool/tokenizer behavior blocks dependent model work and is reviewed; no silent substitute. **Commit:** `chore: qualify and pin IncidentLab runtime`.

### IL-T02 — Establish storage and owner-authenticated control

**PRD:** IL-07, IL-10, IL-11, IL-14. **Depends on:** IL-T01. **Files:** Create `migrations/001_initial.sql`, `src/incidentlab/{schemas.py,db.py,events.py,api.py}`, `tests/{conftest.py,test_api.py}`; extend `cli.py`, `compose.yaml`, `Dockerfile` with the transient evaluation target for private-network tests.

**Interfaces:** Architecture schemas/tables and restricted roles; `append_event(incident_id:UUID,event_type:str,payload:dict)->int`; `read_events(incident_id:UUID,after_seq:int,limit:int)->EventPage`, where `EventPage={events,next_seq,has_more}`. Add `/healthz`, owner-auth dependency, and incident/evidence/event read routes. No unimplemented start/repair endpoint is advertised.

- [ ] Implement numbered migration/checksum ledger, schema/role grants, one-active-incident constraint, and official PostgreSQL checkpointer setup using its restricted search path. Keep migration credentials out of normal services.
- [ ] Implement strict models, transactions, constant-time owner token check, redaction before event storage, and per-incident ordered event allocation under row lock.
- [ ] Implement lifespan readiness and authenticated reads with the exact public error shape; missing DB/auth is an error, never an empty success.
- [ ] Run the documented one-off migration, `uv run pytest tests/test_api.py -m 'not integration'`, and the IL-T02 evaluation-image command against `incidentlab-test_default` for isolated DB role/event-order checks.

**Essential example:** `assert (await client.get('/api/v1/incidents/00000000-0000-0000-0000-000000000001')).status_code == 401` for an unauthenticated ASGI client. **Expected:** Auth occurs before data lookup; control role cannot read sandbox tables; runtime env lacks migrator secrets; event readback preserves order. **Commit:** `feat: add durable storage and owner API boundary`.

### IL-T03 — Complete a real healthy fake checkout

**PRD:** IL-01, IL-09, IL-14. **Depends on:** IL-T02. **Files:** Create `src/incidentlab/sandbox/{__init__.py,schemas.py,service.py,worker.py,api.py}`, `src/incidentlab/executor.py`, `tests/test_sandbox_flow.py`; modify `api.py`, `cli.py`, `compose.yaml`, `Dockerfile`.

**Interfaces:** Sandbox checkout/order/dependency/diagnostic/reset routes from architecture; owner `/demo/reset` and `/demo/checkout`; CLI `smoke-checkout`. `submit_checkout(request_key:UUID)->CheckoutAccepted` in executor owns the fixed write client; owner/verifier call this helper without reading its token. `worker_tick()->int` returns actual fulfilled transitions.

- [ ] Make checkout perform the fixed local inventory HTTP request, insert a unique request-key order on success, and emit operational logs. Repeating its key returns the same order.
- [ ] Run the 250-ms sandbox worker loop, transactionally fulfill pending orders, and stop it during lifespan shutdown. No broker or worker service.
- [ ] Connect owner demo checkout to fixed submit helper and persisted order readback; complete Compose startup order and private publication.
- [ ] Start the test sandbox with the IL-T03 command above and run `docker run --rm --network incidentlab-test_default --env-file .env incidentlab-eval:local uv run pytest tests/test_sandbox_flow.py -m integration`. Then use the documented normal startup sequence and run `docker compose exec -T control python -m incidentlab.cli smoke-checkout` twice with distinct keys.

**Essential example:**

```python
async def test_actual_worker_fulfills_order(sandbox_client, wait_for_order):
    created = await sandbox_client.checkout(request_key=uuid4())
    order = await wait_for_order(created.order_id, timeout_seconds=10)
    assert order.status == "fulfilled"
    assert order.request_id == created.request_id
```

Fixtures use real sandbox HTTP/test DB; `wait_for_order` only polls. **Expected:** Actual inventory request, two persisted fulfilled orders, no secrets in logs. **Commit:** `feat: complete healthy sandbox checkout flow`.

**P0 verification/completion:** Qualification artifact, healthy checkout trace, and auth/role checks; all three task evidence entries recorded. **STOP:** Demonstrate P0, disclose any capability failure, and obtain P1 approval.

## P1 — Investigate a real inventory fault

**Objective/deliverable:** A live LLM produces a structured cited repair proposal from real failure evidence. **PRD:** IL-02–07, IL-10–11. **Prerequisites:** Accepted P0 and explicit P1 approval. **Components:** IL-T04–06. **Exit:** Actual checkout failure, no leaked family label, durable proposal awaiting approval, zero repair effect.

### IL-T04 — Introduce inventory misconfiguration and its runbook

**PRD:** IL-02, IL-03, IL-06. **Depends on:** IL-T03. **Files:** Modify `sandbox/{api.py,service.py}`, `api.py`, `cli.py`, `tests/test_sandbox_flow.py`; create `data/runbooks/inventory.md`.

**Interfaces:** Operator injection `inventory_config`, owner `/api/v1/demo/fault`, CLI `seed --family inventory_config --seed 1`. Runbook ID `inventory`, version `1`, sections `symptoms`, `diagnostics`, `repair`, `verification`, `escalation`. Model-facing diagnostics show operational state only.

- [ ] Under target-state lock set endpoint key `inventory-legacy` and increment revision. Operator metadata stays outside incident/model state; checkout emits its own dependency failure logs.
- [ ] Write operational runbook content: evidence needed, exact allowed `inventory-local` destination, verification, and uncertainty/escalation conditions.
- [ ] Generate unrelated genuine healthy traffic before injection to demonstrate log filtering. Ensure diagnostic JSON excludes hidden scenario identifiers.
- [ ] Run `uv run pytest tests/test_sandbox_flow.py -m 'not integration'`; run `seed`, then `smoke-checkout` and inspect its nonzero failure/request ID.

**Essential assertions:** `assert failed.code == "inventory_unavailable"`; `assert "inventory_config" not in diagnostic_json`. **Expected:** Real reproducible failure and evidence without a gold label. **Commit:** `feat: add observable inventory configuration fault`.

### IL-T05 — Connect read-only MCP and keyword retrieval

**PRD:** IL-03, IL-04, IL-06, IL-11. **Depends on:** IL-T04. **Files:** Create `src/incidentlab/{mcp_server.py,mcp_client.py,retrieval.py}`, `tests/{test_mcp_boundary.py,test_retrieval.py}`; extend `cli.py`.

**Interfaces:** Exactly four architecture MCP tools, `call_read_tool(...)->EvidenceItem`, `retrieve_runbooks(query,mode="keyword",limit=3)`, `index-runbooks --mode keyword`; `build_mcp_environment(settings)->dict[str,str]` constructs the exact read-only env subset. Backend owns fixed stdio command and rejects registry mismatch.

- [ ] Use official SDK stdio lifecycle; stdout is protocol-only. Launch only the fixed module with read token/base URL and safe runtime paths, never inherited complete env.
- [ ] Validate bounded args/results and current-incident request/order scope; persist evidence before passing it to the model. Log truncation/retries explicitly.
- [ ] Index stable Markdown sections and source versions/hashes with PostgreSQL full-text search; return top three cited sections.
- [ ] Run `uv run pytest tests/test_mcp_boundary.py tests/test_retrieval.py -m 'not integration'`, index the runbook, and inspect one genuine stdio session against the broken sandbox.

**Essential example:**

```python
def test_mcp_child_has_read_authority_only(child_env):
    assert "IL_SANDBOX_READ_TOKEN" in child_env
    assert not {"IL_SANDBOX_WRITE_TOKEN", "IL_SANDBOX_ADMIN_TOKEN", "IL_DATABASE_URL"} & child_env.keys()
```

**Expected:** Four read tools, resolvable citations, explicit refusal of extra tools/arguments. **Commit:** `feat: add read-only MCP evidence and runbook retrieval`.

### IL-T06 — Persist bounded agent reasoning and an immutable proposal

**PRD:** IL-03, IL-05–07, IL-10–11. **Depends on:** IL-T05 and qualified P0 model. **Files:** Create `src/incidentlab/{context.py,graph.py}`, `tests/test_agent_contract.py`; modify `model.py`, `schemas.py`, `api.py`, `events.py`.

**Interfaces:** `AgentState` fields follow architecture; `build_context`, `run_investigation`, `POST /api/v1/incidents`; incident UUID is LangGraph thread ID. Freeze proposal payload/digest/revision before `awaiting_approval`.

- [ ] Implement health/logs → retrieval → model/read loop → final validated diagnosis/proposal or escalation. Reserve turn four for final schema; count every call within four turns/six reads.
- [ ] Use qualified tokenizer/template and non-thinking provider mode; bound notes/recent results. Check citation ownership/version, exact action schema, and current supporting configuration. Malformed output/provider failure stops visibly.
- [ ] Persist graph/app state and events. Interrupt node reads an existing proposal and performs no mutation or duplicate proposal creation on replay. Check cancellation/deadline at call boundaries.
- [ ] Run `uv run pytest tests/test_agent_contract.py -m 'not live_model and not integration'` and one explicit live-model investigation; inspect prompt exclusion, citations, digest, and unchanged target revision.

**Essential assertions:** `assert result.state == "awaiting_approval"`; `assert result.proposal.payload.action == "set_inventory_endpoint"`; `assert target.revision == revision_before_investigation`. A live failure is recorded, never replaced with a canned result. **Expected:** Actual model + evidence + durable proposal with no effect. **Commit:** `feat: persist bounded agent investigation and proposals`.

**P1 verification/completion:** Actual fault→model→proposal trace and read/context boundaries. **STOP:** Present P1 evidence and obtain P2 approval before authority-bearing execution work.

## P2 — Complete the first approved recovery in the browser

**Objective/deliverable:** First complete vertical slice from healthy checkout through actual fault, diagnosis, human decision, repair receipt, and a new fulfilled verification order. **PRD:** IL-01, IL-03, IL-07–11, IL-14. **Prerequisites:** Accepted P1 and P2 approval. **Components:** IL-T07–09. **Exit:** Owner can perform real inventory recovery or rejection from the UI.

### IL-T07 — Enforce bound owner approval and one execution claim

**PRD:** IL-07, IL-08, IL-11. **Depends on:** IL-T06. **Files:** Create `src/incidentlab/approvals.py`, `tests/test_approval.py`; modify `api.py`, `graph.py`, `schemas.py`, `events.py`.

**Interfaces:** Architecture `decide_proposal`, `claim_execution`, and `/decision`; reviewer comes from authenticated owner. Browser cannot supply replacement payload. One stored immutable proposal yields at most one execution key.

- [ ] Implement approval transaction under row lock: state/incident/proposal/digest/revision/expiry/cancel checks, recomputed stored digest, unique decision, identical duplicate readback, conflicting decision error.
- [ ] Implement atomic unique claim and related state/event update, then current-revision precheck. Sandbox still performs final atomic revision comparison.
- [ ] Resume graph only from persisted owner decision. Reject/expire/cancel produces no execution dispatch.
- [ ] Run focused approval tests, including real PostgreSQL concurrent deliveries and invalid binding cases in the isolated integration environment.

**Essential example:** `await owner_api.decide(proposal, decision="reject"); assert await db.execution_count(proposal.proposal_id) == 0`. `db.execution_count` is a read-only test helper. **Expected:** One bound decision/claim, no unauthorized dispatch. **Command:** `uv run pytest tests/test_approval.py -m 'not live_model'`. **Commit:** `feat: bind owner approval to immutable repair claims`.

### IL-T08 — Execute the finite repair and independently verify checkout

**PRD:** IL-08, IL-09, IL-11. **Depends on:** IL-T07. **Files:** Extend `executor.py`; create `verifier.py`, `tests/test_execution.py`; modify `sandbox/{schemas.py,service.py,api.py}`, `graph.py`, `cli.py`.

**Interfaces:** `execute_claim`, `reconcile_execution`, `verify_checkout`, sandbox `/repairs` and `/repairs/{key}`, approved→executing→verifying path. Executor owns write client; verifier calls its fixed checkout helper. Inventory action is enabled first; worker action becomes available only with IL-T10 implementation.

- [ ] Validate stored payload, action allowlist, digest, revision and cancellation. Persist `dispatching` before one POST. Sandbox locks target, checks key/digest/revision, applies update and inserts receipt in one transaction; repeat key returns same receipt.
- [ ] Never retry repair POST. Timeout/ack loss becomes unknown and triggers read-only receipt lookup; unresolved result halts. Graph replay cannot dispatch again.
- [ ] Make verifier create a new request key, wait up to ten seconds, read persisted fulfillment, and store its own evidence. Applied receipt plus actual verifier pass alone yields recovery.
- [ ] Run `uv run pytest tests/test_execution.py -m 'not live_model'` using isolated target transactions; run CLI `smoke-demo --family inventory_config --approval interactive` and read its exact proposed action before owner approval.

**Essential example:**

```python
async def test_green_health_is_not_recovery(verifier, broken_target):
    assert (await broken_target.health()).process_ok is True
    result = await verifier.verify_checkout(uuid4(), uuid4())
    assert result.verdict != "pass"
```

**Expected:** One effect, durable receipt, different newly fulfilled verification order, no health-only success. **Commit:** `feat: execute approved repair and verify functional recovery`.

### IL-T09 — Build the thin owner UI and live E2E

**PRD:** IL-01, IL-03, IL-07–10, IL-14. **Depends on:** IL-T08. **Files:** Create `frontend/{package.json,package-lock.json,tsconfig.json,vite.config.ts,index.html}`, `frontend/src/{App.tsx,api.ts,types.ts,styles.css}`, `tests/test_end_to_end.py`; modify `Dockerfile`, `api.py`, `README.md`.

**Interfaces:** `api.ts` typed requests/polling; types mirror OpenAPI. One-second incremental events with page draining; token in React memory. UI supports demo checkout/fault, start, evidence, approve/reject/cancel, receipt and verification. Static assets served by FastAPI after API routing.

- [ ] Pin frontend versions on Node.js 24 LTS; build semantic readable single-page controls and exact-action approval panel. Operator scenario labels never enter investigation request.
- [ ] Implement token entry, polling, explicit auth/error feedback, and reload incident recovery. Expired/stale approval is disabled visually and refused server-side.
- [ ] Add frontend Docker build stage and `test_inventory_recovery_and_rejection`, the one critical explicit live-model E2E using actual backend/target/model/approval/verifier.
- [ ] Run `npm --prefix frontend ci`, `npm --prefix frontend run typecheck`, `npm --prefix frontend run build`, then the live E2E on the lab. Its host-side fixture calls only the owner API at `http://127.0.0.1:8000`; it never tries to resolve private container service names from the host. Inspect the built UI once approving and once rejecting.

**Essential assertions:** Accepted outcome is `verified_recovered` with actual receipt/new-order IDs; rejected execution count is zero. **Live command:** `uv run pytest tests/test_end_to_end.py -m live_model`. **Expected:** Full real browser workflow and rejection path. **Commit:** `feat: deliver first browser-based approved recovery demo`.

**P2 verification/completion:** Critical live E2E, rejection/no-effect run, production frontend build, actual receipt/new order. **STOP:** Demonstrate the complete inventory slice and obtain P3 approval; remaining families stay explicitly pending.

## P3 — Complete the three families, retrieval comparison, and restart safety

**Objective/deliverable:** Full v1 behavior, one bounded retrieval comparison, and durable authority across restart/cancellation. **PRD:** IL-02–09, IL-11–12. **Prerequisites:** Accepted P2 and P3 approval. **Components:** IL-T10–12. **Exit:** Two recoverable families, honest maintenance escalation, measured retrieval choice, and safe handling of interrupted execution.

### IL-T10 — Add worker recovery and maintenance escalation

**PRD:** IL-02, IL-03, IL-08, IL-09. **Depends on:** IL-T09. **Files:** Modify `sandbox/{service.py,api.py}`, `executor.py`, `cli.py`, `tests/test_sandbox_flow.py`, `tests/test_execution.py`, `frontend/src/App.tsx`; create `data/runbooks/{worker.md,upstream.md}`.

**Interfaces:** Enable exact `resume_order_worker(worker_id="checkout-worker")` action in executor/sandbox; add owner scenarios `worker_paused`, `upstream_maintenance`. Runbook IDs `worker`/`upstream`, version `1`, same five section IDs as inventory. Maintenance diagnosis has no repair.

- [ ] Pause actual worker processing while checkout still inserts pending orders. Resume through target-state transaction/receipt and let the loop fulfill real orders. Implement maintenance response on the correct inventory route.
- [ ] Write logs/runbooks distinguishing backlog from dependency maintenance and evidence needed for resume/escalation. Exclude hidden switches/scenario labels from agent config.
- [ ] Extend existing target/action regression groups; final registry has exactly the two allowed actions. No fault-family router or gold lookup is used for diagnosis.
- [ ] Run focused existing sandbox/execution tests, index runbooks, and run CLI `smoke-demo --family worker_paused --approval interactive`, then `smoke-demo --family upstream_maintenance --approval interactive`.

**Essential assertions:** Worker verification reaches a new `fulfilled` order; maintenance yields `decision="escalate"`, `repair=None`, and zero repair receipts. **Expected:** Two actual repairs and one unsupported-case escalation. **Commit:** `feat: complete worker recovery and maintenance escalation`.

### IL-T11 — Compare retrieval and enforce bounded context

**PRD:** IL-03, IL-06, IL-12. **Depends on:** IL-T10. **Files:** Modify `retrieval.py`, `context.py`, `cli.py`, `tests/test_retrieval.py`, `tests/test_agent_contract.py`; create `evals/cases/dev.jsonl`, `evals/README.md`.

**Interfaces:** `RetrievalMode=keyword|dense|hybrid`; 384-dimensional embeddings, exact cosine search, reciprocal-rank fusion constant 60. `compare-retrieval --split dev` prints per-case top-three sections/citation validity/token counts. Twelve development cases are four per family. No held-out data is used.

- [ ] Define twelve meaningful dev recipes covering clear, noisy, missing, and conflicting evidence; keep operator recipe/gold facts separate from model-visible observations.
- [ ] Pin embedding model revision; chunk sections within 200 embedding tokens and preserve stable citation ancestry. Index in existing PostgreSQL; no embedding service or ANN infrastructure.
- [ ] Compare keyword/dense/hybrid once on identical development queries. Select the simplest useful mode; retain keyword on a tie. Verify context cap with the qualified tokenizer, retaining required evidence/revisions.
- [ ] Run focused retrieval/context tests and `docker compose exec -T control python -m incidentlab.cli index-runbooks --mode hybrid`. Then run `docker run --rm --network incidentlab_default --env-file .env incidentlab-eval:local python -m incidentlab.cli compare-retrieval --split dev` using the rebuilt evaluation target. This command only compares retrieved sections; it does not start another incident. Normal runtime image does not contain `evals`.

**Essential example:**

```python
def test_context_keeps_required_evidence_within_budget(state, qualified_tokenizer):
    messages = build_context(state, token_budget=6000)
    assert qualified_tokenizer.count_request(messages) <= 6000
    assert "ev:fixture:1" in json.dumps(messages)
```

The fixture makes `ev:fixture:1` approval-critical evidence; production IDs remain incident-scoped. **Expected:** Measured retrieval choice and bounded prompt, not an assumed improvement. **Commit:** `feat: compare grounded retrieval and bound agent context`.

### IL-T12 — Prove restart, cancellation, and unknown-outcome handling

**PRD:** IL-07–09, IL-11. **Depends on:** IL-T10–11. **Files:** Create `tests/test_recovery.py`; modify `api.py`, `graph.py`, `approvals.py`, `executor.py`, `events.py`, `cli.py`, `tests/test_approval.py`.

**Interfaces:** Complete startup reconciliation, owner `/cancel` and read-only `/reconcile`, plus the single operator CLI procedure `resolve-unknown --incident <UUID>` from architecture. Terminal `closed_unknown` preserves unresolved history and is never recovered/no-effect. No additional repair action.

- [ ] Restart at `awaiting_approval`; restore the same proposal with no model rerun/effect. Resume only from stored authenticated decision.
- [ ] Inject one crash after actual sandbox receipt commit but before control acknowledgment. Recover by receipt GET; repair POST count must not increase. Missing receipt stays unknown and blocks new incidents.
- [ ] Check cancellation before/in-flight dispatch, stale revision, and expiry in existing authority regression groups. Implement owner quiesce→inspect→typed acknowledgment closure exactly as architecture; never infer no effect from missing receipt.
- [ ] Run the focused recovery/approval/execution integration checks and inspect target revision, receipt, and persisted events. Show unknown closure records uncertainty and does not run a repair.

**Essential example:**

```python
async def test_ack_loss_does_not_replay_mutation(crash_harness):
    run = await crash_harness.apply_then_restart_before_ack()
    await run.reconcile()
    assert run.repair_post_count == 1
    assert run.receipt.result == "applied"
```

The hook pauses around a real sandbox transaction, not mocked success. **Command:** `uv run pytest tests/test_recovery.py tests/test_approval.py tests/test_execution.py -m 'not live_model'` in the isolated integration environment. **Expected:** Safe waiting restart, receipt reconciliation, truthful cancellation/unknown outcomes. **Commit:** `fix: make approval and execution recovery durable`.

**P3 verification/completion:** Worker/maintenance live evidence, bounded retrieval comparison, one actual crash/receipt check, and narrow unknown-resolution control. **STOP:** Present P3 evidence and obtain P4 approval. No additional resilience framework is justified.

## P4 — Freeze evaluation and establish lean offline CI

**Objective/deliverable:** Reproducible evaluation with untouched held-out cases, development comparison, and essential offline checks. **PRD:** IL-03–14. **Prerequisites:** Accepted P3 and P4 approval. **Components:** IL-T13–15. **Exit:** Honest dev report, frozen held-out manifest, no live model calls in CI.

### IL-T13 — Build the isolated runner, scorer, and frozen splits

**PRD:** IL-11, IL-12. **Depends on:** P3 complete interfaces. **Files:** Create `evals/{run.py,score.py,manifest.json,cases/heldout.jsonl,gold/heldout.jsonl}`, `tests/test_evaluation.py`; modify `evals/README.md`, `Dockerfile`, `.dockerignore`.

**Interfaces:** `run_case(case:CaseRecipe,approach:fixed|agent)->CaseArtifact`; `score_case(artifact:CaseArtifact,gold:CaseGold)->CaseScore`. `CaseScore` stores correctness, unauthorized/incorrect effect, fabricated-verification flags, factual-review result, and reasons. `python -m evals.run --split dev --approach agent` writes run JSON; `python -m evals.score --input <run-json>` scores it. No external judge/service.

- [ ] Freeze 12 dev and 18 held-out recipes (six held-out per family) and separate gold hashes. Use actual traffic and declared observation filters/state changes per `EVALUATION.md`. Keep held-out status `not_run`.
- [ ] Extend the existing evaluation Docker target with the same-app authenticated ASGI runner from command conventions. Normal runtime stage copies only package/static/migrations/runbooks; synthetic eval files may be in build context but never in its final image.
- [ ] Implement provenance/deterministic scoring plus human factual review fields. Keep failures in denominator and invalid proposals visible. Gold scores afterward; it must not select which valid proposals the harness approves.
- [ ] Run `uv run pytest tests/test_evaluation.py -m 'not integration'`; build evaluation image and inspect one actual dev run with `--case dev-inventory-01`. Do not run held-out yet.

**Essential assertions:** Held-out counts equal `{"inventory_config":6,"worker_paused":6,"upstream_maintenance":6}`; claimed recovery without verifier-owned fulfilled-order evidence fails scoring. **Expected:** Exact split/provenance separation and runnable harness. **Commit:** `test: add isolated incident evaluation harness and frozen cases`.

### IL-T14 — Compare fixed diagnostics with the agent on development cases

**PRD:** IL-03, IL-05, IL-06, IL-12. **Depends on:** IL-T13. **Files:** Create `evals/baseline.py`; modify `evals/run.py`, `evals/score.py`, `evals/README.md`, `README.md`.

**Interfaces:** `run_fixed_diagnostics(incident_id:UUID)->Diagnosis` uses health/logs/config/probe in fixed order and one final schema model call. Both approaches use the same selected retrieval mode, model/digest, final schema, context/output limits, total budgets, and action/authority/verifier path. Retrieval ablation is separate. No family/gold lookup.

- [ ] Implement fixed diagnostic order and paired manifests using identical target resets/seeds and selected retrieval configuration; adaptive read selection is the intended comparison.
- [ ] Run all 12 dev cases for `fixed` and `agent`, approving valid proposals through the same owner endpoint. Inspect actual effects, failed cases, and citation facts.
- [ ] Correct only observed issues within scope; rerun affected checks after a change. Record configurations and failures; no repeated broad experiment grid or cherry-picked scores.
- [ ] Run the following in separate isolated sessions, save JSON artifacts, score them, and restore normal control afterward.

```bash
docker run --rm --network incidentlab_default --env-file .env incidentlab-eval:local uv run python -m evals.run --split dev --approach fixed
docker run --rm --network incidentlab_default --env-file .env incidentlab-eval:local uv run python -m evals.run --split dev --approach agent
```

**Minimal verification:** Each case has actual model/evidence/receipt/verifier provenance; baseline contains no fault-label branch and uses the same retrieval setting. **Expected:** Honest paired development report, including no improvement if that is observed. **Commit:** `eval: compare fixed diagnostics and adaptive investigation`.

### IL-T15 — Add the essential control regressions and one offline CI job

**PRD:** IL-04, IL-07–09, IL-11, IL-14. **Depends on:** IL-T14. **Files:** Create `.github/workflows/ci.yml`; modify existing `tests` groups, `pyproject.toml`, `Dockerfile`, `frontend/package.json`.

**Interfaces:** Pytest markers `integration` and `live_model`; one CI job with local PostgreSQL/pgvector service and sandbox fixture process. No Ollama download/call or paid inference per PR. Existing lockfiles define dependencies.

- [ ] Complete only the focused security/integrity regression groups from `EVALUATION.md`: secret/tool boundary, untrusted evidence, bound approval, receipt/restart integrity, honest verification, auth, and evaluation provenance.
- [ ] Configure one workflow for Ruff, offline essential pytest including database/sandbox transactions, TypeScript check, and production UI build. Explicit fake-adapter tests are labelled offline, never model-quality evidence.
- [ ] Check Docker COPY boundaries and exclusions; secrets/artifacts/caches stay out of all images, synthetic gold only in owner-run evaluation image.
- [ ] Run `uv run ruff check .`, `uv run pytest -m 'not live_model'` against isolated services, `npm --prefix frontend run typecheck`, and `npm --prefix frontend run build` once. Review workflow parity.

**Essential assertion:** An injected log/runbook instruction cannot add tools, disclose tokens, or create an execution without bound owner approval. **Expected:** Essential checks pass offline; live E2E remains explicit. Stop after these checks pass. **Commit:** `ci: enforce focused offline integrity checks`.

**P4 verification/completion:** Paired 12-case dev report, frozen unused held-out set, focused safety checks, and one offline workflow. **STOP:** Review P4 and obtain P5 approval.

## P5 — Measure serving and prove a usable clean setup

**Objective/deliverable:** Real baseline serving measurements, clear owner flows, and a verified fresh setup. **PRD:** IL-01, IL-03, IL-05, IL-07, IL-09–14. **Prerequisites:** Accepted P4 and P5 approval. **Components:** IL-T16–18. **Exit:** Usable app and clean startup, with facts about local serving instead of assumed performance.

### IL-T16 — Measure one qualified serving configuration

**PRD:** IL-05, IL-12, IL-13. **Depends on:** P4 harness and qualified candidate. **Files:** Create `evals/benchmark.py`; modify `model.py`, `events.py`, `evals/README.md`, `README.md`.

**Interfaces:** `python -m evals.benchmark --split dev --sample representative-six --warm-repeats 3` emits measured JSON for one model/provider/configuration at concurrency one. Select and freeze six representative development inputs (two per family) before timing. Unavailable fields are null with reason.

- [ ] Record actual GPU/VRAM/driver/runtime/model digest and current settings; retain the 6,000 input/1,024 output caps and qualified non-thinking mode.
- [ ] Capture one cold-load observation plus three warm repeats of the six fixed representative inputs. Record actual provider token/timing fields, wall time, memory sample interval, and quality; approval wait is separate.
- [ ] Report valid TTFT only if first nonempty output content can be timed; tool-only responses have TTFT unavailable. Never count chunks as tokens. A second configuration is optional only after a measured latency/memory/quality problem, using the identical six inputs and documented reason.
- [ ] Run the benchmark once, inspect raw sample calculations, and report p50/p95/sample count and limitations. Stop if the baseline meets required quality/budgets; no serving matrix.

**Minimal verification:** Recompute one tokens-per-second sample from provider counts/duration and confirm identical quality inputs/budgets. **Expected:** Honest local metrics and any observed issue, no invented optimization claim. **Commit:** `perf: measure local inference quality and latency`.

### IL-T17 — Make evidence, authority, and failures legible

**PRD:** IL-03, IL-07, IL-09–11. **Depends on:** IL-T16 events and complete behavior. **Files:** Modify `frontend/src/{App.tsx,api.ts,types.ts,styles.css}`, `events.py`, `api.py`, `tests/test_api.py`, `README.md`.

**Interfaces:** Existing schemas/routes only. Show model/tool durations, approval wait, exact action/revision/expiry, receipt, verification order, uncertainty, and last known state. Unknown execution offers read-only reconcile; closure stays an explicit owner CLI procedure.

- [ ] Inspect laptop/narrow layouts and keyboard flow. Make action/rejection/cancellation, citations, and outcome readable without introducing dashboard/platform features.
- [ ] Show provider-down, expired/stale, rejection, cancellation, and unknown outcome using existing controls. Error text names the actual issue and next action; failed polling preserves prior evidence.
- [ ] Confirm token absence from URL/storage/logs/events/copied results. Polling retry is a read retry, never a repair resend.
- [ ] Run frontend typecheck/build and focused API regression; manually inspect the built UI once. No static-layout snapshot suite or browser matrix.

**Expected/minimal verification:** Clear owner decision/effect distinction, readable evidence and actionable failures, no credential exposure. **Commit:** `feat: clarify evidence approval and recovery outcomes`.

### IL-T18 — Rehearse clean setup and write accurate operation instructions

**PRD:** IL-01, IL-10, IL-13, IL-14. **Depends on:** IL-T16–17. **Files:** Modify `README.md`, `.env.example`, `compose.yaml`, `Dockerfile`, `.dockerignore`, `evals/README.md`; synchronize planning/status docs only for actual implementation decisions.

**Interfaces:** README describes actual setup/start/stop/migrate/index/demo/eval commands; CLI parser matches them. Normal services never receive migration/bootstrap credentials.

- [ ] Use a separate clean checkout and Compose project `incidentlab-clean` with fresh named volumes. Generate new local secrets/DSNs, migrate before starting services, index runbooks, and build UI; preserve the original environment.
- [ ] Complete healthy checkout and one actual approved inventory recovery in the clean environment, showing real inference and a new fulfilled verification order.
- [ ] Fix only discovered setup defects and rerun affected steps. Write prerequisites, licence/model pin, known limits, short demo, and concise architecture/tradeoff notes.
- [ ] Record exact command/exit outcomes; run `git diff --check` and inspect tracked files for secret/cache/artifact mistakes. No extra deployment target or cloud setup.

**Expected/minimal verification:** Clean setup plus one live smoke without undocumented fixes. Only the tested environment is claimed supported. **Commit:** `docs: verify clean setup and publish demo guide`.

**P5 verification/completion:** Baseline benchmark, inspected UI/error states, and clean-clone live smoke. Freeze configuration for held-out evaluation. **STOP:** Review P5 and obtain P6 approval.

## P6 — Verify held-out outcomes and hand off the finished project

**Objective/deliverable:** Verified portfolio application with honest outcomes and an explainable live demo. **PRD:** IL-01–14. **Prerequisites:** Accepted P5 and P6 approval; frozen prompt/model/runbooks/tools/config/scorer and untouched held-out manifest. **Components:** IL-T19–21. **Exit:** Required gates pass and user accepts the delivered application, or exact failed gate remains open.

### IL-T19 — Run the paired eighteen-case held-out evaluation

**PRD:** IL-03, IL-05–09, IL-11–13. **Depends on:** Frozen P5 candidate. **Files:** Update `README.md`, `evals/README.md`, `TASKS.md`; detailed generated outputs stay in ignored `artifacts/evals/<run-id>/`.

**Interfaces:** Existing runner/scorer, same eighteen cases/seeds/model/retrieval/config/budgets for both approaches. No post-result scoring-rule changes.

- [ ] Verify all manifest hashes and candidate commit, stop normal control, and run the isolated evaluator.
- [ ] Run `python -m evals.run --split heldout --approach fixed` and corresponding `--approach agent` in the evaluation image; retain all cases and score each complete run.
- [ ] Human-review factual diagnoses/citations against gold and actual evidence. Distinguish proposal errors, refused stale actions, incorrect effects, safe escalations, and failures.
- [ ] Require at least 15/18 correct agent outcomes and zero unauthorized repairs, incorrect repairs, and invented verification claims. Restore normal control and publish denominators, paired outcomes, failures, and limitations.

**Minimal verification:** Inspect one repair, one maintenance escalation, one evidence-uncertainty case, and every failed case against raw evidence/receipts. **Expected:** Actual complete report. Failed gate leaves task incomplete; record it and request a bounded correction/new uncontaminated evaluation version, never change the denominator. **Commit:** `eval: report frozen held-out incident outcomes` after an honest complete run.

### IL-T20 — Review PRD coverage and interview readiness

**PRD:** IL-01–14. **Depends on:** IL-T19 release gates pass. **Files:** Update `README.md`, `TASKS.md`, and exact docs whose wording conflicts with actual behavior.

**Interfaces:** Requirement evidence table maps each IL ID to actual test/run/artifact and result. Review is one focused pass, not another feature phase.

- [ ] Check each PRD criterion against code/evidence; remove unsupported claims and identify any missing behavior. No additional provider/action/tool/fault family/dependency enters scope.
- [ ] Concentrate safety review on secret/privilege boundaries, auth, immutable approval, receipt recovery, and verifier ownership; do not add speculative testing.
- [ ] Write factual interview notes: problem, flow, reasons for isolation/SQL/MCP/LangGraph/local model, tradeoffs, one observed challenge/fix, measurements, limitations, and demo sequence.
- [ ] Run `git diff --check` and only checks affected by actual corrections. If all acceptance is already met, stop reviewing and move to handoff.

**Expected/minimal verification:** Every IL ID has evidence, no open critical issue, and no unneeded component. **Commit:** `docs: complete requirement evidence and interview handoff`.

### IL-T21 — Demonstrate live behavior and close the ledger

**PRD:** IL-01–14. **Depends on:** IL-T20. **Files:** Update `README.md`, `TASKS.md`; code changes only if a genuine demo defect needs a scoped fix and verification.

**Interfaces:** Delivered browser/API/model path; no prerecorded diagnosis/effect responses.

- [ ] Demonstrate healthy checkout → inventory fault → actual failure → cited proposal → owner approval → receipt → new fulfilled order; also show rejection/no effect, worker recovery, and maintenance escalation.
- [ ] Show recorded durable-state behavior and measured evaluation/serving results; state actual limitations without promising production operations.
- [ ] Confirm current release/CI evidence and no tracked secret/generated files. Record final commit/demo evidence and present completion against IL-01–14.
- [ ] **STOP for final user acceptance.** Only after explicit acceptance record P6 acceptance/project completion; otherwise record the exact requested correction and next action.

**Expected/minimal verification:** Real final demo plus current release evidence, no open critical errors. **Commit:** `docs: record verified IncidentLab completion` after actual final acceptance.

**P6 completion rule:** Documentation, passing unit tests, or a polished UI do not establish completion. A missed quality/safety/setup criterion stays visible. Do not weaken requirements, add fallback providers, or expand scope to make the ledger appear complete. Once mapped acceptance passes and the owner accepts the demo, stop.
