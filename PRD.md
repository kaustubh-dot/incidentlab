# IncidentLab product requirements — v1

Planning baseline: 2026-10-13. This document freezes the requested v1 scope for implementation review; it does not record user approval to implement. See `TASKS.md` for actual progress and phase approvals. At this baseline the application does not exist and no model, GPU, recovery, evaluation, or performance result has been measured.

## Purpose and original vision

IncidentLab investigates and safely recovers one small, working sandbox checkout application. A developer or interviewer first completes a fake order, deliberately introduces a fault, observes an actual checkout failure, watches a local LLM investigate real emitted logs and current application state with retrieved runbooks, reviews a specific proposed repair, approves or rejects it, and sees an independent functional check of the outcome.

The problem is demonstrating useful agent reasoning without granting an LLM broad operational authority or mistaking a plausible explanation for a recovered service. The differentiators are a real application transition, evidence-linked diagnosis, meaningful read-only MCP tools, a durable human approval boundary, a small deterministic repair vocabulary, and verification that creates a new fake order and observes fulfillment. The model must infer from evidence; it must never receive the operator's fault identifier or evaluation answer key.

The target user is one technically interested owner operating a private local lab. This is a portfolio application, not an incident response product for real production systems. The desired final artifact is an understandable repository with a reproducible live demonstration, documented limitations, measured model-serving behavior, and an honest evaluation report.

## Required demonstration

1. Start the application and submit one fake checkout; observe its order reach `fulfilled` through the sandbox worker.
2. Use the owner-only scenario control to inject one selected fault. Submit another checkout and show the resulting failure or fulfillment timeout with an order/request identifier.
3. Start an investigation using a plain symptom and the failed request identifier. The agent reads real health, sanitized configuration, recent logs, and a read-only dependency/order probe; it retrieves versioned runbook sections.
4. Present a structured diagnosis, cited evidence, uncertainty, and either one precise permitted repair or an escalation. No repair occurs at this step.
5. Approve or reject the immutable proposal through the authenticated owner UI. Rejection and cancellation prevent future repair calls.
6. If approved and still valid, apply the allowlisted repair once through the deterministic executor protocol. Display its durable receipt and independently run a fresh fake checkout through to fulfillment.
7. Display `verified_recovered`, `verification_failed`, `escalated`, `rejected`, `cancelled`, or an actionable error. Never infer recovery merely from HTTP health, a repair acknowledgment, or model text.

## Exactly three fault families

| Family | Actual failing behavior | Authorized repair | Required unsupported outcome |
|---|---|---|---|
| Wrong inventory service configuration | Checkout reaches a deliberately unavailable fixed local inventory route and receives an error. | `set_inventory_endpoint` with `endpoint_key="inventory-local"`. | Missing/conflicting evidence must not justify a speculative repair. |
| Paused order worker | Checkout creates a pending order; the actual worker loop does not fulfill it within the verification deadline. | `resume_order_worker` with `worker_id="checkout-worker"`. | A worker already running does not justify a guessed resume. |
| Simulated upstream maintenance | The local upstream dependency returns maintenance errors while the configured endpoint is correct. | None. | Explain and escalate with evidence; never “repair” maintenance by resetting the scenario. |

Operator fault controls and fixture gold labels are outside the model context, retrieval corpus, diagnostic tool output, and model-visible trace. The upstream dependency may be simulated, but HTTP requests, order creation, worker processing, fault state changes, repair effects, and verification genuinely execute.

## Requirements and measurable acceptance

| ID | Mandatory requirement | Acceptance criterion and evidence |
|---|---|---|
| IL-01 | Working fake checkout sandbox | A healthy `POST /checkout` creates an order that the background loop changes from `pending` to `fulfilled`; the demo records the request ID, order ID, timestamps, and readback state. No payment or external customer data exists. |
| IL-02 | Three real fault families | Each family produces its specified checkout failure from a healthy reset. Exactly two repair actions exist. Maintenance yields escalation without a mutation. Family IDs do not appear in agent inputs. |
| IL-03 | Evidence-grounded investigation | Every final diagnosis/proposal cites at least one actual evidence ID and one retrieved runbook section/version; the UI resolves every citation. Missing/conflicting evidence yields uncertainty or escalation rather than invented observations. |
| IL-04 | Bounded read-only MCP tool use | One backend-owned stdio MCP connection exposes only `get_health`, `get_logs`, `get_config`, and `probe_checkout`. Arguments and outputs are validated; no shell, SQL, file, arbitrary URL, or repair tool is available to the model. At most six diagnostic calls run per incident. |
| IL-05 | Local model and structured outputs | A real Ollama native API call using one qualified local model produces validated tool calls and final diagnosis schemas. `qwen3:8b` is the initial candidate, qualified for licence, digest, tool calling, output schemas, context/tokenizer, and actual lab fit before adoption. Provider failure is shown clearly without a cloud fallback or prerecorded answer. |
| IL-06 | Retrieval and context engineering | Small curated runbooks carry source version and section IDs. Keyword retrieval is compared on development cases with dense/hybrid pgvector retrieval using `all-MiniLM-L6-v2`; select the simplest measured useful option. The graph uses evidence-referenced case notes and bounded recent tool results, four model turns, and a verified 6,000 input-token cap per call. |
| IL-07 | Durable state and human approval | A single LangGraph investigation persists to the same PostgreSQL instance. Approval binds owner identity, incident, immutable proposal/payload digest, target revision, and expiry. A restart while awaiting approval restores the same pending proposal. Rejected, expired, cancelled, modified, or stale proposals cannot execute. |
| IL-08 | Deterministic repair authority | Both control application and sandbox validate the two finite action schemas. Atomic claim, idempotency key, compare-revision, and durable receipt prevent duplicate effects and unauthorized payload substitutions. Ambiguous in-flight outcomes stop and inspect receipt status; graph replay never blindly resends a mutation. |
| IL-09 | Independent functional verification | Recovery is reported only after a newly created verification checkout reaches `fulfilled` within ten seconds and its persisted order is read back. A green health endpoint or model assertion alone cannot pass. Receipt and verification evidence remain inspectable. |
| IL-10 | Owner interface and observability | A thin React/TypeScript UI supports healthy checkout, fault injection, start, evidence/trace inspection, approval/rejection, cancellation, and outcome. Authenticated polling under `/api/v1` refreshes the timeline every second while active. It distinguishes model time, approval wait, execution, and verification and exposes actionable failures. |
| IL-11 | Security and data integrity | One owner bearer token protects every business/API route; it stays in UI memory. Control, sandbox, and migration DB roles are separated. Read, write, and operator credentials are distinct; MCP receives only read credentials. Logs/runbooks are untrusted data. Secrets, gold answers, operator controls, and host administration never enter model context. Critical control checks have zero violations. |
| IL-12 | Honest evaluation and baseline | Freeze 12 development cases and 18 held-out cases, six held-out per family. Compare a fixed diagnostic workflow with the agent on the same held-out cases and model/settings. Target at least 15/18 correct outcomes including escalations, with zero unauthorized repairs, incorrect repairs, and fabricated verification claims. Report every case, failure, model digest, and run provenance. |
| IL-13 | Serving measurement and budgets | On the authorized lab, measure one qualified baseline configuration using six representative development inputs, one cold observation and three warm repeats at concurrency one. Report actual GPU/runtime/model, p50/p95 latency, available token counts/throughput, and observed peak memory. TTFT requires observed first output content timing; chunks are not tokens. A second configuration is considered only for an actual measured issue, on the same inputs. Approval wait is excluded from active deadline and inference cost. |
| IL-14 | Reproducibility and portfolio handoff | A clean-clone Docker Compose run works with documented private lab prerequisites and secret setup. One lean offline GitHub Actions job runs essential checks without live model calls. The README includes demo and interview explanations; all required criteria have linked evidence and known limitations are explicit. |

## Fixed scope and limits

One active incident and one demo target are sufficient. Use one primary LangGraph agent and a deterministic verifier, one FastAPI control application, one separate tiny sandbox HTTP process/container in the same repository, one PostgreSQL 16 instance with pgvector available, and one Ollama inference provider. The separate sandbox is justified by target isolation and independent API credentials, not by a microservice roadmap. A small worker loop processes fake orders; it is not infrastructure queueing.

The available Linux lab is described as 128 GB RAM, an RTX 4000 Ada GPU, Docker, and sudo. Its exact usable VRAM, GPU/container compatibility, driver versions, and model performance remain unverified. Capability qualification is an implementation task, not a reported blocker or a promise of model fitness. Do not store access details or passwords in repository documents.

Keep implementation proportionate as well as architecture: direct readable functions, only required abstractions, essential regression groups, and the small evaluation defined here. Stop once mapped acceptance passes. Extra testing, repeated review, performance tuning, or configuration variants require an observed problem or unmet active requirement; they are not default portfolio polish.

## Explicit non-goals

- Real payments, customer orders, live production incident handling, real cloud infrastructure, or internet-facing multi-user service.
- Arbitrary infrastructure repair, arbitrary URL fetches, shell commands, SQL/file tools, source edits, package installation, host mounts, Docker socket access, Kubernetes, or elevated host privileges for the controller.
- A multi-agent system, generalized agent platform, MCP gateway, plugin marketplace, generic workflow engine, Redis, Celery, external queue, cache layer, or external observability service.
- Additional fault families, automatic incident discovery, multi-tenancy, role-management platform, broad integrations, cloud fallback models, or a matrix of retry/fallback policies.
- Fine-tuning, reranking, autonomous approval, self-generated approval credentials, automatic mutation retry, simulated LLM answers presented as live inference, or claims of exactly-once delivery across processes.
- Exhaustive rare-edge-case testing, maximum coverage targets, speculative load scaling, or paid infrastructure.

## Completion and scope changes

The project is complete only when IL-01 through IL-14 are implemented and verified, held-out and critical-control gates pass, a fresh setup and live demo succeed, and the repository documentation reflects actual behavior. Planned files and passing unit tests alone do not satisfy completion.

`ARCHITECTURE.md` defines implementation contracts; `EVALUATION.md` defines measurement; `ROADMAP.md` defines tasks and phase gates; `TASKS.md` alone records task status, evidence, decisions, and user approvals. A material change to this scope requires a recorded reason, alternatives, effects on requirement IDs, and explicit user approval before implementation. A failed model qualification or quality target is reported as such; it does not authorize weakening safety, substituting a scripted demo, adding another provider, or expanding infrastructure.
