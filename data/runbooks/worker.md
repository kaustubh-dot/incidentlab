# Runbook: Background Order Worker (worker v1)

## symptoms
Checkout requests return HTTP 202 accepted and place orders in `pending` status.
However, pending orders do not transition to `fulfilled` within the ten-second deadline.
Queue of unprocessed pending orders accumulates in the orders table.

## diagnostics
1. Call `get_config` to inspect `worker_running` status and `worker_id`.
2. Call `get_logs` to check for recent worker fulfillment events or background tick errors.
3. Call `probe_checkout` with the pending `order_id` to confirm persistent pending status.
4. Verify if `worker_running` is false while checkout intake remains healthy.

## repair
If `worker_running` is false and `worker_id` is `checkout-worker`,
the sole authorized repair action is `resume_order_worker` with parameter `worker_id="checkout-worker"`.
Preconditions: Observed target revision must match expected revision.
Evidence from `get_config` must confirm `worker_running=false`.

## verification
After repair receipt reports `applied`, submit an independent verification checkout request.
Poll the newly generated order ID until status transitions to `fulfilled`.
Confirm background worker actively drains and fulfills pending orders.

## escalation
If `worker_running` is already true, or if diagnostic logs indicate database transaction deadlocks,
disk errors, or missing order records, do not apply `resume_order_worker`.
Escalate the incident immediately without mutation.
