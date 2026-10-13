# IncidentLab execution ledger

Last updated: 2026-10-13. Current state: documentation-only planning baseline. Application implementation has not started. No model/GPU qualification, passing application test, benchmark, recovery demo, evaluation score, or implementation-phase acceptance is claimed.

`PRD.md` is the v1 scope with the approved pre-lock retrieval simplification recorded below. `ARCHITECTURE.md` owns interfaces, `EVALUATION.md` owns measurements, and `ROADMAP.md` owns execution steps. This file alone owns statuses, gates, decisions, deviations, blockers, evidence, and exact next action.

## Status and exact next action

Task states: `pending`, `in_progress`, `blocked`, `verified_complete`. Only mark verified complete after the task deliverable and minimum verification actually pass with evidence. A plan, directory, stub, or installed dependency is not a completed feature. Phase acceptance and authorization are separate from task verification.

**Next action:** Obtain explicit P0 implementation authorization. Then IL-T01 begins by inspecting the actual lab/runtime and recording model/GPU capabilities. The approved documentation amendment does not authorize application coding.

**Current blockers:** None demonstrated by execution. Hardware fitness, package compatibility, model tool/schema/tokenizer behavior, and clean setup are pending qualification, not measured failures. When an operation fails, record its exact error, affected requirement/task, and safe next step.

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
| IL-T01 | Qualified/pinned runtime and model probe | IL-05, IL-11, IL-13, IL-14 | P0 start approval | pending | Not run |
| IL-T02 | Storage, roles, owner API boundary | IL-07, IL-10, IL-11, IL-14 | IL-T01 | pending | Not run |
| IL-T03 | Actual healthy checkout and worker fulfillment | IL-01, IL-09, IL-14 | IL-T02 | pending | Not run |
| IL-T04 | Observable inventory fault and runbook | IL-02, IL-03, IL-06 | P0 accepted; P1 approved; IL-T03 | pending | Not run |
| IL-T05 | Read-only MCP and keyword retrieval | IL-03, IL-04, IL-06, IL-11 | IL-T04 | pending | Not run |
| IL-T06 | Bounded agent and durable proposal | IL-03, IL-05, IL-06, IL-07, IL-10, IL-11 | IL-T05 | pending | Not run |
| IL-T07 | Bound approval and atomic claim | IL-07, IL-08, IL-11 | P1 accepted; P2 approved; IL-T06 | pending | Not run |
| IL-T08 | Repair, receipt and functional verification | IL-08, IL-09, IL-11 | IL-T07 | pending | Not run |
| IL-T09 | Thin UI and first complete live E2E | IL-01, IL-03, IL-07, IL-08, IL-09, IL-10, IL-14 | IL-T08 | pending | Not run |
| IL-T10 | Worker recovery and maintenance escalation | IL-02, IL-03, IL-08, IL-09 | P2 accepted; P3 approved; IL-T09 | pending | Not run |
| IL-T11 | Runbook grounding and bounded context | IL-03, IL-06, IL-12 | IL-T10 | pending | Not run |
| IL-T12 | Restart/cancel/unknown outcome integrity | IL-07, IL-08, IL-09, IL-11 | IL-T10, IL-T11 | pending | Not run |
| IL-T13 | Frozen cases, isolated runner and scoring | IL-11, IL-12 | P3 accepted; P4 approved; IL-T12 | pending | Not run |
| IL-T14 | Fixed-versus-agent development comparison | IL-03, IL-05, IL-06, IL-12 | IL-T13 | pending | Not run |
| IL-T15 | Essential security regressions and offline CI | IL-04, IL-07, IL-08, IL-09, IL-11, IL-14 | IL-T14 | pending | Not run |
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
| Planning → P0 | Scope/contracts/evaluation/plan | Pending | Planning documents written; no runtime | Pending |
| P0 → P1 | Model/lab qualification and healthy checkout | Pending | Not run | Pending |
| P1 → P2 | Live evidence to proposal, zero effect | Pending | Not run | Pending |
| P2 → P3 | Full inventory recovery and rejection | Pending | Not run | Pending |
| P3 → P4 | Three families, retrieval, crash safety | Pending | Not run | Pending |
| P4 → P5 | Dev comparison, frozen held-out, offline CI | Pending | Not run | Pending |
| P5 → P6 | Measured serving, usable UI, clean setup | Pending | Not run | Pending |
| P6 → Complete | ≥15/18 plus hard-zero gates and final demo | Pending | Not run | Pending |

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

No implementation checkpoint exists at this baseline. After each task append its ID, commit, exact commands/exit results, essential assertions, artifact/run references, real limitations, and next action. Keep secrets and large logs out of this file. Failed checks include actual error and resolution status.

At IL-T20 add the IL-01–IL-14 acceptance evidence mapping here. Until then all requirements are **not verified**. The target 15/18 is a criterion, not a result. Provided lab hardware details are a capability description, not an observed GPU benchmark.

## Resume procedure

1. Read `AGENTS.md` and `PRD.md`, then the gate and first incomplete task here.
2. Read that task in `ROADMAP.md` and its `ARCHITECTURE.md` contracts; inspect actual code and last verified commit.
3. Confirm prerequisite evidence and phase authorization. Prepare the concrete review if absent; do not assume approval.
4. Execute only the bounded task. Ask briefly whether it can be simpler without losing required behavior, and follow the minimum checks rather than creating another review loop.
5. Update this ledger/docs, commit the meaningful checkpoint, and state the exact next action. At phase end stop for review; after accepted completion stop building.
