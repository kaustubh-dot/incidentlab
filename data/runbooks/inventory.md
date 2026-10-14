# Runbook: Inventory Service Configuration (inventory v1)

## symptoms
Checkout requests fail immediately with HTTP 503 `inventory_unavailable`.
Clients receive an error message indicating the inventory dependency cannot be reached.
No pending or fulfilled order is created in database orders table for the failed checkout request.

## diagnostics
1. Call `get_config` to inspect current target configuration, especially `endpoint_key`.
2. Call `get_logs` with the failed `request_id` to observe `inventory_failed` events.
3. Call `probe_checkout` to inspect live inventory HTTP status code and reachability.
4. Verify whether `endpoint_key` differs from expected active service endpoint `inventory-local`.

## repair
If `endpoint_key` is set to `inventory-legacy` or an obsolete target,
the sole authorized repair action is `set_inventory_endpoint` with parameter `endpoint_key="inventory-local"`.
Preconditions: Observed target revision must match expected revision.
Evidence from `get_config` and `get_logs` must confirm misconfiguration.

## verification
After repair receipt reports `applied`, submit an independent verification checkout request.
Poll the newly generated order ID until status transitions to `fulfilled`.
Read back the persisted order to confirm fulfillment timestamp and request linkage.

## escalation
If `endpoint_key` is already configured as `inventory-local` but inventory checks still fail,
or if diagnostic logs indicate upstream maintenance or dependency shutdown,
do not apply `set_inventory_endpoint`. Escalate the incident immediately without mutation.
