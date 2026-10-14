# IncidentLab Evaluation Harness & Case Splits

This directory defines the reproducible offline evaluation harness, development recipes, held-out test splits, and scoring protocols for IncidentLab.

## Case Splits and Separation

Per `EVALUATION.md`, the dataset is strictly partitioned into:

1. **Development Split (`evals/cases/dev.jsonl`)**:
   - 12 total cases (4 per fault family).
   - Covers four systematic operational conditions:
     - `clean`: standard initial conditions, single fault injection, immediate observation.
     - `noisy`: realistic background traffic, prior fulfilled orders, unrelated operational logs.
     - `missing`: declared diagnostic tool filters (e.g. masked config or probe) requiring safe escalation for insufficient evidence.
     - `conflicting`: target state changes between observation and proposal dispatch requiring safe refusal or escalation.
   - Used for prompt engineering, token budget verification, keyword retrieval grounding checks, and baseline comparison.

2. **Held-Out Split (`evals/cases/heldout.jsonl` & `evals/gold/heldout.jsonl`)**:
   - 18 frozen cases (6 per fault family).
   - Frozen with separate gold outcome hashes.
   - Evaluated exclusively in Phase P6.

## Recipe Schema

Each line in `evals/cases/*.jsonl` defines an executable operator recipe:

```json
{
  "case_id": "dev-inventory-01",
  "split": "dev",
  "family": "inventory_config",
  "variation": "clean",
  "seed": 101,
  "initial_conditions": {
    "endpoint_key": "inventory-legacy",
    "worker_running": true,
    "maintenance": false
  },
  "traffic_recipe": {
    "pre_fault_checkouts": 0,
    "post_fault_checkouts": 1,
    "noise_logs": 0
  },
  "evidence_filter": null,
  "state_change": null,
  "symptom_template": "Checkout failed: Inventory service returned HTTP 503"
}
```

## Grounding & Token Bounds

- **Runbook Indexing**: Versioned markdown runbooks in `data/runbooks/` are split into 5 standard sections (`symptoms`, `diagnostics`, `repair`, `verification`, `escalation`).
- **Section Bounds**: All sections are strictly kept under 200 tokens according to the qualified model tokenizer.
- **Context Budget**: The agent context assembly adheres to the 6,000-token input budget cap (`IL_MAX_INPUT_TOKENS`), preserving required evidence IDs and citation ancestry.
