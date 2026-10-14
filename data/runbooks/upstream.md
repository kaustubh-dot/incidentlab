# Runbook: Upstream Dependency Maintenance (upstream v1)

## symptoms
Checkout requests fail with HTTP 503 while target endpoint configuration is correct (`inventory-local`).
Inventory dependency returns explicit maintenance response code and notice.
Checkout errors indicate upstream provider is temporarily unavailable for scheduled maintenance.

## diagnostics
1. Call `get_config` to confirm `endpoint_key` is configured as `inventory-local`.
2. Call `get_logs` for failed checkout request ID to check error messages and maintenance details.
3. Call `probe_checkout` to inspect HTTP response status and confirm upstream maintenance payload.
4. Verify that target configuration is not misconfigured.

## repair
None. Upstream dependency maintenance is an external operational condition.
It cannot be repaired by changing local sandbox endpoint configurations or resuming workers.
No authorized repair action exists for upstream maintenance.

## verification
Periodically call `probe_checkout` to monitor upstream dependency reachability until maintenance window ends.
Once probe returns HTTP 200, submit a fresh verification checkout to confirm recovery.

## escalation
Because no authorized repair exists, explain the upstream maintenance condition with cited probe evidence.
Escalate the incident immediately without performing any configuration mutation or scenario reset.
