# IncidentLab

A bounded application-recovery agent: investigate a broken sandbox checkout, propose a specific repair, wait for human approval, and verify the result through actual application behavior.

**Status: Active implementation in progress. Phase P0 (qualified runtime & healthy checkout sandbox), Phase P1 (observable fault injection, MCP tools, runbook FTS indexing, and bounded LangGraph investigation), and Phase P2 (bound owner approval, deterministic repair execution, independent functional verification, and thin owner UI console) are implemented.**

Private repository: [kaustubh-dot/incidentlab](https://github.com/kaustubh-dot/incidentlab).

## The product we are building

An operator sees a working fake checkout fail, asks the agent to investigate, inspects its evidence and proposed repair, then approves or rejects the action. A deterministic executor enforces authorization. An independent functional check establishes whether recovery happened; a model's statement that it succeeded is insufficient.

Planned capabilities:

- One small functional sandbox checkout with three deliberately bounded fault families.
- Investigation using application observations, real emitted logs, and versioned runbooks retrieved with keyword search.
- Read-only diagnostic tools exposed through a small MCP interface.
- A clear proposal and approval screen, safe pause/resume, and action receipts.
- Actual end-to-end recovery verification or explicit escalation when no allowed repair exists.
- Inspectable traces, a fixed-workflow baseline, and a small outcome-based evaluation.

This is not an autonomous DevOps platform, cloud administrator, unrestricted coding agent, or real payment system. Fine-tuning is not required.

Dense/hybrid retrieval experiments are outside active scope unless an observed retrieval failure justifies a separately approved change. The required focus remains investigation, approval, bounded repair, and verified recovery.

## Read the planning pack

| File | Authority and purpose |
| --- | --- |
| [PRD.md](PRD.md) | Canonical product vision, requirements, acceptance criteria, scope, and non-goals. |
| [AGENTS.md](AGENTS.md) | Binding instructions for vision preservation, implementation, safety, and handoff. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | State, tools, approvals, action receipts, API, configuration, and module contracts. |
| [ROADMAP.md](ROADMAP.md) | Ordered phases, exact tasks, dependencies, commands, and review gates. |
| [TASKS.md](TASKS.md) | Current statuses, approvals, decisions, blockers, and the next action. |
| [EVALUATION.md](EVALUATION.md) | Outcome evaluation, baseline comparison, and essential regression checks. |
| [.env.example](.env.example) | Configuration names; local credentials are supplied only during setup. |

Use these documents together. Do not create duplicate competing plans or treat a task ledger as a substitute for the PRD.

## Architecture summary

One control application built with Python/FastAPI and a thin React/TypeScript UI, one PostgreSQL instance, and a small separate sandbox process in the same repository. The sandbox is a controlled test target, not a second commercial application. Its separation protects the application boundary and makes genuine HTTP-based recovery visible.

One LangGraph agent investigates through read-only MCP tools. Repair execution remains outside the model's tool registry and requires an approved, unchanged proposal. The sandbox exposes only finite allowed controls, not arbitrary shell commands, code execution, arbitrary network destinations, or host Docker access.

Ollama serves the initial `qwen3:8b` candidate. Model identity, tool behavior, memory fit, and latency must be measured. Linux lab access with Docker and sudo is authorized; actual installed components and available GPU capacity remain unverified. The lab is not assumed to be a permanent public host.

## Setup and execution status

This planning scaffold is not runnable. Application commands become available only after their files and lockfiles are created by [ROADMAP.md](ROADMAP.md). That document owns the exact setup and execution sequence.

The planned reference environment is Linux, Python 3.12, Node.js 24 LTS, Git, `uv`, Docker/Compose, and the qualified Ollama endpoint. Windows can hold the repository and edit the documents. The GPU profile is qualified before use; never assume that sudo access proves a working container GPU runtime.

Configuration belongs in an ignored local `.env`. [.env.example](.env.example) contains no real credentials. Blank required secrets must fail clearly. Keep SSH passwords and keys out of files, command logs, and Git. The public-facing UI is local/private by default; database, model, and sandbox-control services must not be exposed publicly.

Run one flagship GPU workload at a time unless measured capacity supports more. IncidentLab must not require MoneyTrail to be installed or running.

## First implementation handoff

Give the implementation agent this instruction:

> Read AGENTS.md, PRD.md, ARCHITECTURE.md, ROADMAP.md, TASKS.md, and EVALUATION.md. Identify the last verified checkpoint and first incomplete task. Execute only the phase I authorize. Preserve the actual investigation, explicit approval, bounded repair, and independent verification workflow. Do not turn the system into canned diagnosis or unrestricted infrastructure automation. At the gate, demonstrate the result, run the minimal checks, update TASKS.md, commit the coherent checkpoint, and stop for review before the next phase.

The user has authorized the project direction and this planning repository. Per-phase implementation approvals are separate and recorded in `TASKS.md`.

## Intended interview demonstration

This is the required product journey, not a currently available demo:

1. Place a successful fake order through the sandbox UI.
2. Activate a known demonstration fault through operator-only controls.
3. Show the real checkout or processing failure.
4. Ask the agent to investigate and inspect its tool observations and runbook citations.
5. Review the proposed action. Reject it once and show that no repair occurs.
6. Approve a fresh valid proposal and observe the bounded repair.
7. Verify a new order completes correctly; inspect the receipt and run timeline.
8. Demonstrate upstream maintenance, where the correct outcome is escalation rather than an invented repair.

Prepared fault scenarios are legitimate demo data. The agent must not receive their hidden IDs or answer keys. Saved traces are labelled replays, not live inference. A green health badge alone does not prove functional recovery.

## Interview preparation at completion

Replace this preparation checklist with concise notes based on the actual implementation before marking the project complete:

- Explain the operator problem and why the agent is limited to one sandbox application.
- Trace investigation, proposal, approval, execution, and independent verification.
- Explain how the agent is prevented from obtaining write credentials or arbitrary execution tools.
- Explain why a restart or repeated approval does not justify blindly repeating a write.
- Present outcome evaluation and baseline results, including failures and escalations.
- Explain the smallest architecture that provides these guarantees and why extra agents, queues, and Kubernetes were excluded.
- Discuss one real failure and its verified fix, along with the project's limits.

## Completion standard

All mandatory PRD requirements must work, including the actual sandbox workflow and the critical authorization controls. Verify acceptance checks, clean-clone setup, accurate startup instructions, and a repeatable demonstration. Resolve material security and integrity failures. Do not mark the project complete merely because its folders, prompts, tests, or UI exist.
