# IncidentLab architecture contracts — planned v1

Status: design only, 2026-10-13. No runtime components or benchmark results exist yet. `PRD.md` governs scope; this file governs shared interfaces. Changes to these contracts must update dependent roadmap tasks and the decision record in `TASKS.md`.

## Components and direction of trust

```text
Owner browser -> FastAPI control application -> PostgreSQL: control + checkpoints
                       |       |
                       |       +-> Ollama native API (one qualified model)
                       +-> backend-owned MCP stdio child -> sandbox diagnostic HTTP
                       +-> deterministic executor        -> sandbox repair HTTP
                       +-> deterministic verifier        -> sandbox checkout HTTP
                       +-> owner-only scenario control   -> sandbox operator HTTP

Sandbox HTTP process + worker loop -> PostgreSQL: sandbox
```

The browser is a thin client served from FastAPI after Vite builds static assets. Polling is sufficient for one owner: `GET /api/v1/incidents/{id}/events?after_seq=N` every second while active or awaiting approval; stop on a terminal result and show polling errors without clearing the last result. This is an event timeline, not token streaming.

Compose contains `postgres`, `control`, `sandbox`, and `ollama`; the model service uses the `gpu` profile on the qualified Linux lab. Only the control service is published to `127.0.0.1:8000`. No sandbox, PostgreSQL, or Ollama ports are published. The control server binds `0.0.0.0` inside its container, with loopback host publication. The sandbox listens on the internal network at port 8001. No service gets privileged mode, Docker socket, host filesystem mounts, or host administration. Named volumes hold PostgreSQL and downloaded model data; code/static assets are image contents. No cloud deployment is planned.

Ollama and PostgreSQL may retain data across demo runs. Reset clears only the sandbox demo state, never approval receipts or control audit records. Fresh-clone verification uses a separate Compose project name and fresh named volumes; do not delete an owner's existing data.

## Selected stack and bounded decisions

| Decision | Contract and reason |
|---|---|
| Runtime | Python 3.12; FastAPI, Pydantic 2, psycopg 3 SQL, httpx, official MCP Python SDK, LangGraph plus PostgreSQL checkpointer, Ollama Python `AsyncClient`, sentence-transformers. Pin mutually compatible versions in `uv.lock` during P0. No ORM or dependency framework is needed. |
| Storage | PostgreSQL 16 with pgvector extension available; one instance, separate `control` and `sandbox` schemas plus a `checkpoints` search path for the checkpointer. Simple numbered SQL migrations and a migration ledger are sufficient. |
| UI | React, TypeScript, Vite; ordinary CSS; one page with checkout/scenario panel, investigation, evidence/timeline, and approval/outcome. Lock frontend versions in `package-lock.json`. |
| Model | `qwen3:8b` through native Ollama API is a candidate, not a qualified guarantee. Record exact tag, digest, quantization, licence, runtime version, tool/schema capability, tokenizer, and observed GPU use. No provider abstraction beyond one small client module. |
| Retrieval | Keyword baseline, then development comparison with dense cosine similarity or reciprocal-rank hybrid retrieval over pgvector. Select by measured usefulness; no reranker. `all-MiniLM-L6-v2` runs as a small embedding component, not another reasoning agent. |
| State | One incident ID equals one LangGraph `thread_id`. Application records are authoritative for approval and repair; graph state is orchestration data. Never treat a checkpoint as permission to write. |
| Execution | One backend process, one active incident, one asyncio background investigation task, one sandbox worker. No broker, queue service, distributed scheduler, or automatic parallel investigations. |

The default deployment is Compose because it provides a repeatable database/model environment and isolates the repair target. A single process for control and target was rejected because it would weaken the credential and fault boundary. A larger service or agent platform adds no required behavior. These are design choices within the PRD; qualification may revise a pinned library version without adding a new architectural component.

## Planned files and responsibilities

```text
pyproject.toml / uv.lock             Python dependency and command definitions
Dockerfile / compose.yaml           reproducible control, sandbox, database, model services
.dockerignore                       keep credentials, generated artifacts, and caches out of build context
src/incidentlab/
  __init__.py
  api.py                            FastAPI lifecycle, owner routes, static UI
  config.py                         validated settings and credential scoping
  schemas.py                        shared strict public and internal data models
  db.py                             psycopg connection/transaction helpers
  cli.py                            documented finite setup/demo/evaluation commands
  model.py                          one Ollama AsyncClient adapter and token accounting
  graph.py                          primary agent nodes, state, pause/resume orchestration
  context.py                        bounded evidence notes and recent result assembly
  retrieval.py                      runbook loading, indexing, keyword/dense/hybrid search
  mcp_client.py                     fixed stdio child ownership and four-tool allowlist
  mcp_server.py                     read-only tools using sandbox diagnostic credential
  approvals.py                      proposal freeze, owner decision, atomic claim
  executor.py                       two repair dispatches and receipt reconciliation
  verifier.py                       fresh checkout and independent persisted-order readback
  events.py                         append/read redacted timeline and measurement events
  sandbox/
    __init__.py
    api.py                          checkout, dependency, diagnostic, repair, operator routes
    service.py                      order, configuration, injection, and repair transactions
    worker.py                       small pending-to-fulfilled background loop
    schemas.py                      sandbox request and receipt models
migrations/001_initial.sql          schemas, tables, role grants, migrations
frontend/src/{App.tsx,api.ts,types.ts,styles.css}
frontend/{package.json,package-lock.json,tsconfig.json,vite.config.ts,index.html}
tests/{conftest.py,test_model_contract.py,test_sandbox_flow.py,test_mcp_boundary.py,
       test_retrieval.py,test_agent_contract.py,test_approval.py,test_execution.py,
       test_recovery.py,test_api.py,test_evaluation.py,test_end_to_end.py}
evals/{cases/dev.jsonl,cases/heldout.jsonl,gold/heldout.jsonl,manifest.json,
       run.py,baseline.py,score.py,benchmark.py,README.md}
data/runbooks/{inventory.md,worker.md,upstream.md}
.github/workflows/ci.yml
artifacts/                           ignored qualification, run, and performance outputs
```

The tree names future files, not delivered code. Runtime Docker stages explicitly copy only the application package, built frontend, migrations, and runbooks; they do not copy `evals`, tests, or gold. A separate transient `evaluation` build target may copy synthetic tests/evals/gold from the same build context. `.dockerignore` excludes real secrets/artifacts/caches, not synthetic fixtures needed by that target. The evaluation target is available in P0 for private-network integration tests; P4 adds datasets and scoring, not another runtime service. Gold and operator recipes never enter retrieval, MCP access, or model context. This is harness data separation, not a claim that the repository owner cannot inspect answers.

## Sandbox behavior and HTTP boundary

The target sells one fake SKU, `SKU-1`, with quantity one. It has no identity, payment, shipping, or stock depletion features. `POST /checkout` performs a real inventory HTTP request to a fixed route, inserts one pending order on success, and returns `202 {request_id,order_id,status:"pending"}`. The worker scans pending orders every 250 ms and fulfills them when enabled. Client `request_key` is unique so a deliberately repeated checkout request returns the same order. Inventory failure returns `503 {code:"inventory_unavailable",request_id,detail}` without an order. Health may remain healthy during business failure.

The only configured endpoint keys are `inventory-local` (fixed `http://127.0.0.1:8001/dependency/inventory/SKU-1`) and `inventory-legacy` (fixed `http://127.0.0.1:8001/dependency/unavailable/SKU-1`). Neither API accepts a URL. `inventory-legacy` is operator injection data, never an allowed repair destination. A local maintenance switch makes the proper dependency route return `503` with a maintenance response. This switch is excluded from sanitized configuration; its externally observable response/logs are legitimate evidence.

| Route | Credential | Contract |
|---|---|---|
| `POST /checkout` | write | `{request_key:UUID,sku:"SKU-1",quantity:1}`; creates an actual fake order. Owner checkout and verifier invoke executor's fixed `submit_checkout` helper; the write client/token remains owned by executor. |
| `GET /orders/{order_id}` | read | `{order_id,request_id,status:pending\|fulfilled,created_at,fulfilled_at}`. |
| `GET /diagnostics/health` | read | Process/database reachability, timestamp, and target revision; never “recovered.” |
| `GET /diagnostics/config` | read | Endpoint key, worker ID, worker running flag, target revision; no secrets, fault IDs, maintenance switch, or operator metadata. |
| `GET /diagnostics/logs?request_id=&limit=40` | read | Up to 40 ordered real emitted structured entries; 512-character message cap, truncation flag, request/order IDs, timestamp, severity, event kind. No fault labels or credential headers. |
| `GET /diagnostics/probe?order_id=` | read | A fresh read-only inventory GET plus optional persisted order read; structured response/status/observed time. Does not create an order or change target state. |
| `POST /repairs` | write | Strict `RepairRequest` below; only the executor uses this route. |
| `GET /repairs/{execution_key}` | write | Durable receipt or explicit `404 receipt_not_found`; never triggers a mutation. |
| `POST /operator/reset` | admin | Healthy reset for this demo; permitted only without a nonterminal incident. Advances revision, resets target config, clears fake orders/logs, retains repair receipts. |
| `POST /operator/inject` | admin | `{family:inventory_config\|worker_paused\|upstream_maintenance,variant_seed:int}`; starts from a healthy reset, applies one fault, advances revision, returns operator-only state. |

The dependency routes are bound to the sandbox's loopback interface or protected from nonlocal calls; they cannot accept arbitrary inventory IDs or routes. Fault injectors emit ordinary operational failures, not entries saying which scenario was injected. A target revision changes on configuration, worker enable/disable, maintenance, reset, or repair. Ordinary order transitions and diagnostic reads do not change it.

## Shared schemas and core interfaces

All Pydantic API/tool models use strict types, `extra="forbid"`, bounded strings and arrays, finite enums, and UTC ISO timestamps. IDs are UUID strings except monotonically increasing evidence/event sequence numbers. Unknown fields fail visibly; do not discard them. Public errors are `{error:{code,message,request_id,retryable}}`, with safe operational details and no stack traces/secrets. Full sanitized diagnostics remain in owner-visible events.

```python
class EvidenceItem:
    evidence_id: str             # e.g. "ev:<incident UUID>:3"
    kind: Literal["health", "logs", "config", "probe", "runbook", "verification"]
    observed_at: datetime
    source: str                  # fixed tool name or runbook ID
    source_version: str | None
    section_id: str | None
    data: dict                   # validated kind-specific bounded shape

class RepairPayload:
    # Discriminated union; exactly these two variants, no free-form args.
    action: Literal["set_inventory_endpoint", "resume_order_worker"]
    # set_inventory_endpoint variant: endpoint_key: Literal["inventory-local"]
    # resume_order_worker variant: worker_id: Literal["checkout-worker"]

class Diagnosis:
    summary: str                 # 1..1200 chars
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str]      # 1..8 existing evidence IDs
    runbook_citations: list[RunbookCitation]  # 1..3, ID/version/section
    decision: Literal["propose_repair", "escalate"]
    repair: RepairPayload | None
    uncertainty: str             # 0..800 chars; required if confidence != high

class Proposal:
    proposal_id: UUID
    incident_id: UUID
    payload: RepairPayload
    payload_digest: str          # SHA-256 canonical payload JSON
    target_revision: int
    created_at: datetime
    expires_at: datetime
    evidence_ids: list[str]

class ApprovalDecision:
    proposal_id: UUID
    payload_digest: str
    target_revision: int
    decision: Literal["approve", "reject"]
    # Reviewer is derived from authenticated owner, never accepted from client.

class RepairRequest:
    execution_key: UUID          # allocated once for this approved proposal
    incident_id: UUID
    proposal_id: UUID
    payload: RepairPayload
    payload_digest: str
    expected_revision: int

class RepairReceipt:
    execution_key: UUID
    payload_digest: str
    result: Literal["applied", "precondition_failed"]
    before_revision: int
    after_revision: int
    recorded_at: datetime

class VerificationResult:
    verdict: Literal["pass", "fail", "error"]
    request_id: UUID
    order_id: UUID | None
    observed_status: Literal["fulfilled", "pending", "unavailable", "unknown"]
    started_at: datetime
    finished_at: datetime
    evidence_id: str
```

These blocks define schema fields, not complete application implementation. `RunbookCitation` is exactly `{runbook_id:str,source_version:str,section_id:str}`. The repair union variants have different fields; an irrelevant field from the other variant is invalid. A diagnosis with `decision="escalate"` must have `repair=null`; a repair proposal must carry one permitted payload. Server validation requires all cited evidence to belong to this incident, cited sections to have been retrieved, current configuration evidence to support the proposed change, and non-low confidence. Valid schema is necessary, not proof the model's diagnosis is correct.

Canonical payload serialization is UTF-8 JSON with lexicographically sorted keys, separators `(',', ':')`, no optional omitted variation, and no floats. Hash only this validated payload; bind the other authority fields in the immutable proposal row. The server recomputes the digest from stored payload at approval and claim time; browser-supplied payload bodies never replace it.

| Interface | Responsibility |
|---|---|
| `start_incident(request:StartIncident) -> IncidentView` (async) | Authenticate caller at route, claim the one-active slot, persist incident, launch bounded investigation. |
| `run_investigation(incident_id:UUID) -> None` (async) | Advance the one persisted LangGraph thread with budget/cancel checks. |
| `call_model(messages:list[dict], tools:list[dict], final_schema:dict or None) -> ModelResult` (async) | One Ollama native request; validate structured output and record actual usage/error. |
| `call_read_tool(incident_id:UUID, name:ToolName, args:dict) -> EvidenceItem` (async) | Validate allowlist/scope, call MCP, persist sanitized evidence. |
| `build_context(state:AgentState, token_budget:int=6000) -> list[dict]` | Assemble and count bounded evidence-linked model context. |
| `retrieve_runbooks(query:str, mode:RetrievalMode, limit:int=3) -> list[EvidenceItem]` (async) | Return versioned sections using the selected bounded retrieval method. |
| `decide_proposal(incident_id:UUID, owner_id:str, decision:ApprovalDecision) -> ApprovalView` (async) | Atomically bind authenticated owner decision to stored proposal. |
| `claim_execution(incident_id:UUID, proposal_id:UUID) -> ExecutionClaim` (async) | Atomically create one execution key after authority checks. |
| `execute_claim(claim:ExecutionClaim) -> RepairReceipt` (async) | Dispatch the stored finite repair once, never automatically retry mutation. |
| `reconcile_execution(execution_key:UUID) -> ExecutionView` (async) | Read a durable sandbox receipt; never dispatch. |
| `submit_checkout(request_key:UUID) -> CheckoutAccepted` (async) | Executor-owned fixed checkout HTTP write; no model-accessible caller. |
| `verify_checkout(incident_id:UUID, request_key:UUID) -> VerificationResult` (async) | Request a fresh checkout through executor and read persisted fulfillment. |
| `resolve_unknown(incident_id:UUID, acknowledgement:str, note:str) -> IncidentView` (async, local owner CLI only) | After operator quiescence, inspect receipt/target and record explicit unresolved closure without an effect or success claim. |

`ToolName` is the four-name MCP enum; `RetrievalMode` is `keyword|dense|hybrid`. `AgentState` contains incident ID, phase, evidence ID list, evidence-linked case notes, two recent result references, read/model counters, accumulated active milliseconds, diagnosis, proposal ID, and cancellation flag. `CheckoutAccepted` contains request ID, order ID, and `pending` status; dependency failure is a typed error, not an accepted order. These are interface contracts, not incomplete executable stubs.

`StartIncident={symptom:str[1..2000],request_id:UUID,order_id:UUID|null}`. `IncidentView` contains ID, state, timestamps, diagnosis/proposal/outcome, and last event sequence, never credentials or fixture IDs. `ModelResult={tool_calls:list[ValidatedToolCall],diagnosis:Diagnosis|null,input_tokens:int|null,output_tokens:int|null,duration_ms:int}`. `ApprovalView` is stored proposal/decision/reviewer/decision time. `ExecutionClaim` is stored execution key, proposal binding, expected revision, validated payload, and state. `ExecutionView` adds receipt and reconciliation/error status. Exact frontend types mirror OpenAPI schemas.

Operator reset/injection CLI commands may use admin credentials; diagnostic tools may not. Only `executor.py` creates the HTTP client with the write credential; owner checkout and verifier call its fixed checkout helper, and receipt reconciliation uses its read-status method. No caller can request an arbitrary endpoint or payload. Backend environment inheritance into the MCP child is an explicit allowlist containing only read token, sandbox URL, required runtime path, and safe log level. Do not pass the backend's whole environment to `StdioServerParameters`. The model sees tool schemas and validated results, never credentials or subprocess launch configuration.

## Model, MCP, retrieval, and context

The backend launches the fixed command `python -m incidentlab.mcp_server` using the same installed environment, owns its lifespan, and checks `list_tools` against the exact four names below. Extra or missing tools fail startup; stdout is protocol only and sanitized diagnostics go to stderr. No user/model-selected modules or command arguments are permitted. The official SDK supplies stdio client/server transport; it is not a tool authorization system, so argument validation remains local. [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

| MCP tool | Arguments | Result |
|---|---|---|
| `get_health` | `{}` | Validated health evidence, current revision. |
| `get_logs` | `{request_id:UUID|null,limit:int[1..40]}` | Bounded logs; request ID restricted to current incident's checkout or null for recent service logs. |
| `get_config` | `{}` | Sanitized current endpoint/worker configuration. |
| `probe_checkout` | `{order_id:UUID|null}` | Read-only dependency/order result; order restricted to current incident. |

Runbook retrieval is a bounded graph function, not an arbitrary file tool. Index Markdown sections using stable `runbook_id`, explicit `source_version`, section heading/ID, and content hash. Each section is at most 200 embedding tokens; split longer sections into stable numbered pieces without losing citation parent. Start with three runbooks and their operational symptoms, diagnostic checks, permitted repair/preconditions, verification, and escalation guidance. No scenario IDs or gold outcomes enter the corpus. Keyword uses PostgreSQL full-text rank. Dense stores 384-dimensional embeddings and cosine distance. Hybrid retrieves up to six results from each and uses reciprocal rank with constant 60, deduplicates by chunk ID, returns top three. Exact vector scan is enough for this corpus; no approximate index or embedding service is required. The model card describes the embedding model and dimensionality; pin its actual revision. [Model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2), [pgvector documentation](https://github.com/pgvector/pgvector).

The graph obtains initial health and relevant logs through MCP, retrieves runbooks from those symptoms, then asks the model for additional tool calls or a final diagnosis. Initial calls count toward the six-call budget. Use at most four model turns total, including the final structured answer; reserve the fourth for the final schema and disable further tool selection then. Native tool calling and schema mode may be separate calls if qualification requires it; both consume the same four-turn budget. Qualify the actual SDK/API support for `think=false` and explicitly disable Qwen thinking in this bounded tool/JSON workflow, so the 1,024 output cap is not consumed by hidden reasoning. Unsupported behavior fails qualification pending a documented compatible configuration; never silently increase budgets or swap providers. Never parse executable commands from prose. Ollama's native chat API is the integration contract; record returned token counts and timing fields when present. [Ollama native chat API](https://docs.ollama.com/api/chat).

Context includes the fixed policy/tool/action schemas, symptom, cited runbook sections, current case notes, and the two most recent bounded tool results. Case notes contain hypothesis, uncertainty, and evidence IDs; the server checks referenced IDs exist and never promotes a model note into observed fact. Keep original evidence in storage. The 6,000-token cap is per input call, with a 1,024-output-token cap and an initial `num_ctx=8192`; qualify the actual chat template/tokenizer and include tool/schema tokens in counting. Use the pinned tokenizer via `transformers`, CPU-only, with the qualified model's chat template; count the fully assembled prompt, not characters divided by four. If the template does not match, qualification fails until a verified count method is recorded. Remove oldest nonessential tool text first while retaining cited evidence, revisions, and approval-critical facts; if it still exceeds the cap, stop with `context_budget_exceeded`.

Read HTTP operations have a five-second timeout and at most one retry for a connection failure or HTTP 502/503/504; malformed output, authentication errors, and semantic failures are not retried. Retried attempts count separately in event metrics and cannot exceed six actual MCP tool invocations. Model calls have a 60-second timeout, no automatic model retry, and no provider fallback. A 240-second active deadline includes diagnosis, execution, and verification but excludes time in `awaiting_approval`; persist accumulated active milliseconds at every state change and call boundary. Budget exhaustion terminates visibly and cannot authorize a repair. Human waiting adds no model usage.

## State and persistence

```text
created -> investigating -> awaiting_approval -> approved -> executing -> verifying
                         \-> escalated                          |          |
                         \-> failed                             |          +-> verified_recovered
                                                                |          +-> verification_failed
awaiting_approval -> rejected / expired / cancelled              +-> execution_unknown
approved -> stale / cancelled / executing
any active state -> failed or cancellation request (subject to in-flight rule)
```

`stale` requires a new investigation/proposal and a new approval. `execution_unknown` blocks further mutations until receipt reconciliation is conclusive or the explicit owner quiesce/inspection procedure below records terminal `closed_unknown`. A lookup showing `applied` proceeds to verification; a missing/ambiguous receipt alone is not proof of no effect. `closed_unknown` always preserves uncertainty and is never a recovery claim. Cancellation checks occur before each model/tool call, before claim, and immediately before dispatch. When a mutation is already in flight cancellation cannot promise to prevent or undo it: persist `cancel_requested`, reconcile/read the receipt, and display any applied effect. No new repair is sent. If effect is confirmed applied, a readback/functional verification may finish as a safety observation, with final state `cancelled` plus explicit receipt/verdict, never silently `verified_recovered`.

| Table | Required fields / constraints |
|---|---|
| `control.incidents` | `id`, `state`, symptom, request/order IDs, `created_at`, `updated_at`, `active_ms`, `cancel_requested`, outcome/error JSON; partial unique index on constant true for all nonterminal states enforces one active incident. `execution_unknown` remains nonterminal; explicit `closed_unknown` is terminal with unresolved outcome retained. |
| `control.evidence` | `(incident_id,evidence_id)` primary key, kind, source/version/section, observed time, sanitized typed payload JSON; append-only. |
| `control.proposals` | `id`, `incident_id`, immutable payload JSON/digest, target revision, evidence IDs, created/expiry times; at most one live proposal per incident. Database grants/API disallow payload updates. |
| `control.approvals` | `proposal_id` unique, decision, authenticated `owner_id="owner"`, reviewed digest/revision, decided time. No model inserts. |
| `control.executions` | `execution_key` primary key, `proposal_id` unique, state `claimed\|dispatching\|applied\|precondition_failed\|unknown`, payload digest, expected revision, claim/dispatch times, receipt/error JSON. |
| `control.events` | `(incident_id,seq)` primary key allocated under incident row lock, event type/time, safe payload, duration/token fields; append-only, ordered polling. |
| `control.runbook_chunks` | chunk ID primary key, runbook/version/section, content hash/text, tsvector, nullable `vector(384)`, embedding model revision. |
| `control.schema_migrations` | integer version primary key, checksum, applied time; mismatched checksum fails startup/migration. |
| `checkpoints.*` | Official LangGraph PostgreSQL checkpointer tables created by its setup method under this restricted search path; do not hand-roll checkpoint serialization. |
| `sandbox.target_state` | single row, monotonic revision, endpoint enum, worker flag, maintenance flag; lock this row for fault/configuration/repair mutations. |
| `sandbox.orders` | order ID primary key, request key unique, request ID, fixed SKU/quantity, status and timestamps. |
| `sandbox.logs` | sequence, timestamp, request/order IDs, event kind, safe JSON details. |
| `sandbox.repair_receipts` | execution key primary key, payload digest, result, before/after revision, recorded time; retained across resets. |

`incidentlab_control` has DML on control/checkpoints only; `incidentlab_sandbox` has DML on sandbox only. `incidentlab_migrator` owns schema objects and extension setup and is used only for explicit migration, never normal HTTP service startup. Passwords and tokens are supplied locally. The control application does not connect directly to sandbox tables; all target reads and writes pass through credentialed HTTP contracts. No model/MCP DB credential exists.

LangGraph interrupts can restart a node from its beginning on resume, so the approval node only reads an already frozen proposal and calls `interrupt`; it performs no repair, creates no duplicate proposal, and sends no external mutation before/inside the interrupt. `Command(resume=...)` is produced only by a server-side persisted decision, never directly from arbitrary browser or model state. [LangGraph interrupt semantics](https://docs.langchain.com/oss/python/langgraph/interrupts).

## Approval, claim, dispatch, and crash protocol

1. Store a validated diagnosis, exact repair payload/digest, observed target revision, evidence IDs, and expiry (`created_at + 600 seconds`) before setting `awaiting_approval`. The UI displays action and concrete argument, reason/evidence, target revision, expiry, and “Approve repair”/“Reject”; it cannot edit a payload in place.
2. On owner decision, begin a PostgreSQL transaction, lock the incident/proposal, verify authenticated owner, current `awaiting_approval`, matching incident/proposal/digest/revision, unexpired proposal, and no cancellation. Recompute stored digest. Insert exactly one approval and update state/event atomically. Duplicate identical decision returns the stored decision; conflicting decision is `409 approval_already_decided`. Rejection commits `rejected` with no execution.
3. Approval is not yet a write. Claim in a separate transaction locks the same incident, verifies the stored approval binding/expiry/cancellation again, and inserts one execution row with a unique execution key and unique proposal ID. Update state and claim event in the same commit. Concurrent requests cannot both claim. The application reads current target revision before dispatch; changed revision marks `stale` and requires a new proposal/approval. The sandbox also checks revision atomically to close the race.
4. Just before the HTTP call, recheck cancellation and atomically change execution `claimed -> dispatching`. Dispatch the one `RepairRequest`. Do not configure automatic HTTP POST retries. Approval expiry is checked before dispatch, not after an already accepted effect. The sandbox authenticates write credential, validates finite action and digest, locks target state, and looks up execution key. A matching existing receipt is returned without another effect; a mismatched digest is `409 execution_key_conflict`.
5. In one sandbox database transaction, compare expected revision, apply the allowed state update only on match, increment revision, and insert the receipt. On mismatch insert `precondition_failed` receipt without mutation. Commit these together; rollback means neither effect nor receipt. The sandbox cannot approve an action by itself, and the control executor sends only stored approved payloads. The sandbox credential's finite power remains a deliberate boundary, not a substitute for owner approval.
6. Store returned receipt and incident/event update atomically in the control database. If receipt is `applied`, launch independent verification. A declined precondition becomes `stale`, not a successful recovery. Timeouts, disconnects, or restart after `dispatching` may mean an effect occurred; mark `execution_unknown` and query `GET /repairs/{execution_key}`. A confirmed applied receipt resumes verification; never resend the POST automatically, even if the lookup says missing.
7. On backend startup, reconstruct waiting approvals and graph state; do not let checkpoint replay call repair. A `claimed` row that never became `dispatching` can safely continue after all checks. Any `dispatching`/`unknown` row first performs read-only receipt reconciliation. If outcome remains unknown, show `execution_outcome_unknown` with key, last state, and the narrow operator procedure below. Terminal incidents, including `closed_unknown`, never resume a checkpoint or dispatch.

This protects one effect per execution key and disallows blind replay. It does not claim a distributed exactly-once transaction. There is no saga or compensation framework; these two repairs have explicit preconditions and a human review boundary.

### Narrow owner resolution of an unknown execution

Ordinary `/reconcile` only reads receipts. If the receipt remains missing/ambiguous, the owner may close the investigation without claiming to know its historical effect. This is a local CLI procedure, not another model tool or browser repair action:

```bash
docker compose stop control sandbox
docker compose up -d sandbox
docker compose run --rm control python -m incidentlab.cli resolve-unknown --incident <incident-UUID>
docker compose --profile gpu up -d control
```

Stopping both services must complete before restarting sandbox; this quiesces old controller requests and sandbox handlers, preventing an old in-flight mutation from arriving after closure/reset. The CLI does not launch normal API lifespan/background recovery. It requires the locally configured owner credential and control DB authority; it prints incident/proposal/execution binding, last dispatch state, a fresh receipt lookup and current sanitized target configuration/revision. It records those observations as evidence. If a durable receipt is found, it records the receipt and routes to the existing reconciliation outcome; do not close a known result as unknown. If target or receipt lookup is unavailable, it fails visibly and leaves the block.

For a reachable target with unresolved history, the CLI asks the owner to confirm the completed stop/restart procedure, enter a nonempty inspection note, and type `ACKNOWLEDGE UNKNOWN <execution-key>`. Under an incident row lock it rechecks the same still-unknown execution and commits `closed_unknown`, owner ID, timestamp, note, inspected evidence IDs, and `effect_unknown=true` atomically with event `execution.closed_unknown`. It performs no sandbox mutation, reset, verification-success declaration, or POST replay. The execution row stays `unknown` for audit; the terminal incident releases the one-active-incident constraint. A subsequent owner reset and fresh incident are permitted after closure, and the old incident's unresolved outcome remains visible. This deliberately trades automated recovery for an explicit, auditable owner decision; missing receipt never becomes a claim that nothing happened.

## Control API

All business routes require `Authorization: Bearer <IL_OWNER_TOKEN>` and constant-time token comparison. The browser obtains the token from the owner, stores it only in React memory, and asks again after reload. No URL token, localStorage, cookies, analytics, or CORS wildcard. Static assets and bare `/healthz` may be unauthenticated; `/healthz` returns only process liveness. The API does not have login, user provisioning, or multiple roles.

| Route | Request / response and restriction |
|---|---|
| `POST /api/v1/demo/reset` | Empty body; calls owner operator route; refuses while an incident is nonterminal. |
| `POST /api/v1/demo/fault` | Operator-only family and seed; forwards to admin route; refused while active. Returned fault labels remain in operator panel, never incident state/context. |
| `POST /api/v1/demo/checkout` | `{request_key:UUID}`; server fills SKU/quantity, posts actual checkout, polls order for at most ten seconds, returns functional result. |
| `POST /api/v1/incidents` | `StartIncident`; returns `202 IncidentView`; `409 active_incident_exists` on concurrent start. |
| `GET /api/v1/incidents/{id}` | `IncidentView`; readback survives reload/restart. |
| `GET /api/v1/incidents/{id}/events?after_seq=0&limit=100` | Ordered `{events,next_seq,has_more}`; `limit` at most 100; client drains pages before waiting. |
| `GET /api/v1/incidents/{id}/evidence/{evidence_id}` | Stored sanitized `EvidenceItem`; explicit `404 evidence_not_found`. |
| `POST /api/v1/incidents/{id}/decision` | `ApprovalDecision`; returns stored `ApprovalView`; body never supplies reviewer or arbitrary payload. |
| `POST /api/v1/incidents/{id}/cancel` | Empty body; persists cancellation request and returns current status, including any in-flight effect. |
| `POST /api/v1/incidents/{id}/reconcile` | Empty body; owner-triggered receipt read only; returns `ExecutionView`; never resends a repair. |

Failure codes include `unauthorized`, `active_incident_exists`, `provider_unavailable`, `model_timeout`, `invalid_model_output`, `tool_timeout`, `invalid_tool_output`, `context_budget_exceeded`, `execution_budget_exceeded`, `approval_expired`, `proposal_mismatch`, `approval_already_decided`, `target_revision_changed`, `execution_outcome_unknown`, and `verification_failed`. UI shows a concrete next action: check local model service, inspect the cited tool error, rerun a new investigation after correcting the cause, or inspect receipt status. It never silently selects another model, drops citations, “repairs” malformed JSON, or hides an error behind success.

## Configuration contract

`.env.example` documents these values; actual secrets remain in ignored `.env`. Empty required secrets fail startup with variable names only. Container service environments are explicit per service; the MCP child receives only the scoped read subset. The migration DSN and bootstrap passwords are not included in normal control/sandbox process environments.

| Variable | Default / required value |
|---|---|
| `IL_BIND_HOST` / `IL_PORT` | `127.0.0.1` / `8000` for native launch; Compose overrides internal bind to `0.0.0.0`, publishes host loopback only. |
| `IL_DATABASE_URL` | Required control-role DSN, database `incidentlab`, host `postgres`, port 5432. No default password. |
| `IL_SANDBOX_DATABASE_URL` | Required sandbox-role DSN for the same instance/database. |
| `IL_MIGRATION_DATABASE_URL` | Required only for explicit migration, migrator role on the same instance. |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `incidentlab` / `incidentlab_migrator` / required local secret. Bootstrap/migration only. |
| `IL_CONTROL_DB_PASSWORD` / `IL_SANDBOX_DB_PASSWORD` | Required local secrets used by bootstrap role creation only. Match runtime DSNs. |
| `IL_OWNER_TOKEN` | Required randomly generated owner bearer token, at least 32 random bytes encoded as hex. |
| `IL_SANDBOX_READ_TOKEN` / `IL_SANDBOX_WRITE_TOKEN` / `IL_SANDBOX_ADMIN_TOKEN` | Three distinct required random tokens; diagnostic, executor/checkout, and owner scenario control respectively. |
| `IL_SANDBOX_BASE_URL` | `http://sandbox:8001`; fixed deployment configuration, not a model argument. |
| `IL_OLLAMA_BASE_URL` | `http://ollama:11434`; one local inference provider. |
| `IL_MODEL` | `qwen3:8b` initial candidate; pin qualified digest in run manifest and deployment instructions. |
| `IL_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2`; pin qualified revision. |
| `IL_RETRIEVAL_MODE` | `keyword` initially; only `keyword`, `dense`, or `hybrid`; chosen from development comparison. |
| `IL_MODEL_TIMEOUT_SECONDS` / `IL_ACTIVE_DEADLINE_SECONDS` | `60` / `240`. |
| `IL_MAX_MODEL_TURNS` / `IL_MAX_READ_CALLS` | `4` / `6`. |
| `IL_MAX_INPUT_TOKENS` / `IL_MAX_OUTPUT_TOKENS` | `6000` / `1024`. |
| `IL_APPROVAL_TTL_SECONDS` / `IL_LOG_LEVEL` | `600` / `INFO`. |

Do not expose the migration role, admin token, or write token to prompts, event payloads, browser responses, runbook indexing, or MCP subprocesses. Structured logging redacts authorization headers, token/password settings, connection strings, and secret-bearing errors before persistence. Operational logs and runbooks are explicitly untrusted text; model instructions found there do not change system policy, tool registry, or repair authority. The finite executor and schema checks enforce this independently of prompt wording.

## Verification and event contract

`verify_checkout` generates a new request key, calls sandbox checkout, waits up to ten seconds polling persisted order every 250 ms, and reads `GET /orders/{id}` after fulfillment. A pass requires the returned IDs to match the new request and status to be `fulfilled`; any wrong ID, timeout, dependency failure, or unreadable order is fail/error. The graph cannot manufacture a `VerificationResult`; only verifier code creates `kind="verification"` evidence and the server sets `verified_recovered` from its pass plus an applied receipt.

Events use `incident.created`, `state.changed`, `model.started`, `model.finished`, `tool.started`, `tool.finished`, `retrieval.finished`, `proposal.created`, `approval.decided`, `execution.claimed`, `execution.dispatched`, `execution.receipt`, `verification.finished`, `cancel.requested`, and `error`. Each event contains incident ID, monotonic sequence, UTC timestamp, component, safe payload, and optional measured milliseconds/token counts. Record tool attempts, model version/digest, retrieval mode/version, budget counters, approval wait, actual effects, and verification IDs. Do not persist hidden reasoning or chain-of-thought; use concise evidence-linked summaries and observable calls/results. Local PostgreSQL events and the UI timeline satisfy observability without an external service.
