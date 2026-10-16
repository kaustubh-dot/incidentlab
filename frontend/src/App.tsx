import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelIncident,
  demoCheckout,
  demoFault,
  demoReset,
  getEvents,
  getEvidence,
  getIncident,
  reconcileIncident,
  startIncident,
  submitDecision,
} from "./api";
import {
  ApprovalDecision,
  EventItem,
  EvidenceItem,
  IncidentState,
  IncidentView,
  RepairReceipt,
} from "./types";

export default function App() {
  // Authentication: In-memory token only (never localStorage per IL-11)
  const [token, setToken] = useState<string>("");
  const [authError, setAuthError] = useState<string | null>(null);

  // Operator demo state
  const [demoStatus, setDemoStatus] = useState<string | null>(null);
  const [demoLoading, setDemoLoading] = useState(false);
  const [selectedFamily, setSelectedFamily] = useState<string>("inventory_config");

  // Incident & Form state
  const [symptom, setSymptom] = useState<string>(
    "Checkout failed: Inventory service returned HTTP 503"
  );
  const [requestId, setRequestId] = useState<string>("");
  const [orderId, setOrderId] = useState<string>("");
  const [incident, setIncident] = useState<IncidentView | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pollingError, setPollingError] = useState<string | null>(null);
  const [copyFeedback, setCopyFeedback] = useState<string | null>(null);

  // Execution receipt & verification details
  const [executionReceipt, setExecutionReceipt] = useState<RepairReceipt | null>(null);

  // Current timestamp for expiry countdown
  const [currentTime, setCurrentTime] = useState<number>(Date.now());

  // Events & Timeline
  const [events, setEvents] = useState<EventItem[]>([]);
  const nextSeqRef = useRef<number>(0);

  // Evidence modal state
  const [inspectingEvidence, setInspectingEvidence] = useState<EvidenceItem | null>(null);

  // Helper: check if incident is in an active (non-terminal) state
  const isTerminal = (state?: IncidentState): boolean => {
    if (!state) return true;
    return [
      "verified_recovered",
      "verification_failed",
      "escalated",
      "failed",
      "rejected",
      "expired",
      "cancelled",
      "closed_unknown",
    ].includes(state);
  };

  // Keyboard accessibility: close modal on Escape
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && inspectingEvidence) {
        setInspectingEvidence(null);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [inspectingEvidence]);

  // Live countdown timer for active proposal
  useEffect(() => {
    if (!incident?.proposal || incident.state !== "awaiting_approval") return;
    const timer = setInterval(() => {
      setCurrentTime(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, [incident?.proposal, incident?.state]);

  // Extract execution receipt from events if present
  useEffect(() => {
    const receiptEvent = events.find((e) => e.event_type === "execution.receipt");
    if (receiptEvent && receiptEvent.payload) {
      setExecutionReceipt(receiptEvent.payload as unknown as RepairReceipt);
    }
  }, [events]);

  // Poll incident and events (Read-only GET retry; preserves existing evidence on error)
  const pollIncident = useCallback(async () => {
    if (!token || !incident || isTerminal(incident.state)) return;

    try {
      // 1. Fetch latest incident view
      const updated = await getIncident(token, incident.id);
      setIncident(updated);
      setPollingError(null);

      // 2. Drain event pages
      let currentSeq = nextSeqRef.current;
      let hasMore = true;
      const newEvents: EventItem[] = [];

      while (hasMore) {
        const page = await getEvents(token, incident.id, currentSeq, 100);
        if (page.events.length > 0) {
          newEvents.push(...page.events);
          currentSeq = page.next_seq;
        }
        hasMore = page.has_more;
      }

      if (newEvents.length > 0) {
        nextSeqRef.current = currentSeq;
        setEvents((prev) => [...prev, ...newEvents]);
      }
    } catch (err: unknown) {
      // Preserve prior incident and events; indicate read-only polling interruption
      console.warn("Read-only polling interruption (prior evidence preserved):", err);
      setPollingError("Polling interrupted; retrying read without replaying mutation...");
    }
  }, [token, incident]);

  // Polling loop interval
  useEffect(() => {
    if (!token || !incident || isTerminal(incident.state)) return;
    const interval = setInterval(pollIncident, 1000);
    return () => clearInterval(interval);
  }, [token, incident, pollIncident]);

  // Operator Handlers
  const handleResetSandbox = async () => {
    if (!token) {
      setAuthError("Owner bearer token is required");
      return;
    }
    setDemoLoading(true);
    setAuthError(null);
    setExecutionReceipt(null);
    try {
      const res = await demoReset(token);
      setDemoStatus(`Sandbox reset to healthy baseline (revision ${res.revision})`);
    } catch (err: unknown) {
      setDemoStatus(`Reset failed: ${String(err)}`);
    } finally {
      setDemoLoading(false);
    }
  };

  const handleInjectFault = async () => {
    if (!token) {
      setAuthError("Owner bearer token is required");
      return;
    }
    setDemoLoading(true);
    setAuthError(null);
    try {
      const res = await demoFault(token, selectedFamily, 1);
      setDemoStatus(`Fault '${res.family}' injected (revision ${res.revision})`);
    } catch (err: unknown) {
      setDemoStatus(`Fault injection failed: ${String(err)}`);
    } finally {
      setDemoLoading(false);
    }
  };

  const handleDemoCheckout = async () => {
    if (!token) {
      setAuthError("Owner bearer token is required");
      return;
    }
    setDemoLoading(true);
    setAuthError(null);
    try {
      const res = await demoCheckout(token);
      if (res.status === "fulfilled") {
        setDemoStatus(
          `Healthy checkout fulfilled: order_id=${res.order_id}, request_id=${res.request_id}`
        );
      } else {
        setDemoStatus(
          `Checkout failed as expected: status=${res.status}${res.error ? `, error=${res.error}` : ""}`
        );
        setRequestId(res.request_id);
        if (res.order_id) {
          setOrderId(res.order_id);
        }
        if (res.error) {
          setSymptom(`Checkout failed: ${res.error}`);
        } else if (res.status === "pending") {
          setSymptom("Checkout accepted order but fulfillment timed out: order remains pending");
        }
      }
    } catch (err: unknown) {
      setDemoStatus(`Checkout request failed: ${String(err)}`);
    } finally {
      setDemoLoading(false);
    }
  };

  // Start Incident Handler
  const handleStartIncident = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token) {
      setAuthError("Owner bearer token is required");
      return;
    }
    setActionLoading(true);
    setActionError(null);
    setExecutionReceipt(null);
    setEvents([]);
    nextSeqRef.current = 0;

    try {
      const reqIdToUse = requestId.trim() || crypto.randomUUID();
      const newInc = await startIncident(token, {
        symptom: symptom.trim(),
        request_id: reqIdToUse,
        order_id: orderId.trim() || undefined,
      });
      setIncident(newInc);
      setRequestId(reqIdToUse);
    } catch (err: unknown) {
      setActionError(String(err));
    } finally {
      setActionLoading(false);
    }
  };

  // Decision Handlers
  const handleDecision = async (decision: "approve" | "reject") => {
    if (!token || !incident || !incident.proposal) return;
    setActionLoading(true);
    setActionError(null);

    const payload: ApprovalDecision = {
      proposal_id: incident.proposal.proposal_id,
      payload_digest: incident.proposal.payload_digest,
      target_revision: incident.proposal.target_revision,
      decision,
    };

    try {
      await submitDecision(token, incident.id, payload);
      // Immediately read updated view
      const updated = await getIncident(token, incident.id);
      setIncident(updated);
    } catch (err: unknown) {
      setActionError(`Decision submission failed: ${String(err)}`);
    } finally {
      setActionLoading(false);
    }
  };

  // Cancel Handler
  const handleCancel = async () => {
    if (!token || !incident) return;
    setActionLoading(true);
    setActionError(null);
    try {
      const updated = await cancelIncident(token, incident.id);
      setIncident(updated);
    } catch (err: unknown) {
      setActionError(`Cancellation failed: ${String(err)}`);
    } finally {
      setActionLoading(false);
    }
  };

  // Reconcile Handler
  const handleReconcile = async () => {
    if (!token || !incident) return;
    setActionLoading(true);
    setActionError(null);
    try {
      const execView = await reconcileIncident(token, incident.id);
      if (execView.receipt) {
        setExecutionReceipt(execView.receipt);
      }
      const updated = await getIncident(token, incident.id);
      setIncident(updated);
    } catch (err: unknown) {
      setActionError(`Reconciliation failed: ${String(err)}`);
    } finally {
      setActionLoading(false);
    }
  };

  // Evidence Inspector
  const handleInspectEvidence = async (evidenceId: string) => {
    if (!token || !incident) return;
    try {
      const item = await getEvidence(token, incident.id, evidenceId);
      setInspectingEvidence(item);
    } catch (err: unknown) {
      setActionError(`Failed to load evidence: ${String(err)}`);
    }
  };

  // State Badge Helper
  const renderStateBadge = (state?: IncidentState) => {
    if (!state) return null;
    let badgeClass = "badge-gray";
    if (state === "verified_recovered") badgeClass = "badge-green";
    else if (state === "awaiting_approval") badgeClass = "badge-amber";
    else if (["investigating", "executing", "verifying"].includes(state))
      badgeClass = "badge-blue";
    else if (["verification_failed", "failed", "rejected"].includes(state))
      badgeClass = "badge-red";
    else if (["cancelled", "closed_unknown"].includes(state)) badgeClass = "badge-purple";

    return <span className={`badge ${badgeClass}`}>{state.replace("_", " ")}</span>;
  };

  // Expiry calculations for proposal
  const expiresAtMs = incident?.proposal
    ? new Date(incident.proposal.expires_at).getTime()
    : 0;
  const remainingMs = Math.max(0, expiresAtMs - currentTime);
  const isExpired = incident?.proposal ? remainingMs <= 0 : false;
  const remainingSeconds = Math.floor(remainingMs / 1000);
  const countdownMinutes = Math.floor(remainingSeconds / 60);
  const countdownSeconds = remainingSeconds % 60;
  const countdownText = isExpired
    ? "EXPIRED"
    : `${countdownMinutes}m ${countdownSeconds.toString().padStart(2, "0")}s`;

  // Derived telemetry metrics from audit events
  const modelEvents = events.filter((e) => e.event_type === "model_called");
  const toolEvents = events.filter((e) => e.event_type === "tool_executed");
  const totalModelDurationMs = modelEvents.reduce((acc, e) => acc + (e.duration_ms || 0), 0);
  const totalToolDurationMs = toolEvents.reduce((acc, e) => acc + (e.duration_ms || 0), 0);
  const tpsSamples = modelEvents
    .map((e) => e.payload?.tokens_per_second)
    .filter((v): v is number => typeof v === "number");
  const avgTokensPerSec =
    tpsSamples.length > 0
      ? (tpsSamples.reduce((a, b) => a + b, 0) / tpsSamples.length).toFixed(1)
      : null;

  // Approval wait time calculation
  const proposalCreatedAt = incident?.proposal
    ? new Date(incident.proposal.created_at).getTime()
    : 0;
  const approvalDecidedEvent = events.find((e) => e.event_type === "approval_decided");
  const decidedAt = approvalDecidedEvent
    ? new Date(approvalDecidedEvent.created_at).getTime()
    : (incident && isTerminal(incident.state)
        ? new Date(incident.updated_at).getTime()
        : currentTime);
  const approvalWaitMs =
    proposalCreatedAt > 0 ? Math.max(0, decidedAt - proposalCreatedAt) : 0;
  const approvalWaitSeconds = Math.floor(approvalWaitMs / 1000);

  // Copy safe incident summary (strictly credential-free)
  const handleCopySummary = async () => {
    if (!incident) return;
    const diagSummary =
      incident.diagnosis?.summary || incident.diagnosis?.hypothesis || "None";
    const citEvs = (
      incident.diagnosis?.evidence_ids ||
      incident.diagnosis?.cited_evidence_ids ||
      []
    ).join(", ");
    const citRbs = (
      incident.diagnosis?.runbook_citations?.map((c) => `${c.runbook_id}:${c.section_id}`) ||
      incident.diagnosis?.cited_runbook_sections ||
      []
    ).join(", ");

    const summaryLines = [
      `=== IncidentLab Audit Record ===`,
      `Incident ID: ${incident.id}`,
      `State: ${incident.state}`,
      `Symptom: ${incident.symptom}`,
      `Active Duration: ${(incident.active_ms / 1000).toFixed(2)}s`,
      `Model Time: ${(totalModelDurationMs / 1000).toFixed(2)}s${avgTokensPerSec ? ` (${avgTokensPerSec} tok/s)` : ""}`,
      `Tool Time: ${(totalToolDurationMs / 1000).toFixed(2)}s (${toolEvents.length} reads)`,
      `Approval Wait: ${approvalWaitSeconds}s`,
      `Diagnosis: ${diagSummary}`,
      `Evidence Citations: ${citEvs || "None"}`,
      `Runbook Citations: ${citRbs || "None"}`,
    ];

    if (incident.proposal) {
      summaryLines.push(
        `Proposal Action: ${incident.proposal.payload.action}`,
        `Target Revision: ${incident.proposal.target_revision}`,
        `Payload Digest: ${incident.proposal.payload_digest}`
      );
    }

    if (executionReceipt) {
      summaryLines.push(
        `Execution Key: ${executionReceipt.execution_key}`,
        `Receipt Result: ${executionReceipt.result}`,
        `Revision Delta: ${executionReceipt.before_revision} -> ${executionReceipt.after_revision}`
      );
    }

    try {
      await navigator.clipboard.writeText(summaryLines.join("\n"));
      setCopyFeedback("Incident summary copied to clipboard (zero credentials included).");
      setTimeout(() => setCopyFeedback(null), 3000);
    } catch {
      setCopyFeedback("Failed to copy summary to clipboard.");
    }
  };

  return (
    <div className="app-container">
      {/* Header & Authentication */}
      <header className="app-header">
        <div className="brand">
          <h1>IncidentLab</h1>
          <p>Autonomous Incident Recovery &amp; Approval Console</p>
        </div>
        <div className="auth-bar">
          <input
            type="password"
            placeholder="Enter Owner Bearer Token..."
            value={token}
            aria-label="Owner Bearer Token"
            onChange={(e) => {
              setToken(e.target.value);
              setAuthError(null);
            }}
          />
          {token ? (
            <span className="badge badge-green">Token Active</span>
          ) : (
            <span className="badge badge-amber">Auth Required</span>
          )}
        </div>
      </header>

      {authError && <div className="alert alert-danger">{authError}</div>}
      {actionError && <div className="alert alert-danger">{actionError}</div>}
      {pollingError && (
        <div className="alert alert-warning" role="status">
          <strong>Connection Notice:</strong> {pollingError}
        </div>
      )}
      {copyFeedback && (
        <div className="alert alert-info" role="status">
          {copyFeedback}
        </div>
      )}

      <div className="dashboard-grid">
        {/* Operator Playground Panel */}
        <section className="panel">
          <div className="panel-header">
            <h2>Operator Playground</h2>
            <span className="badge badge-gray">Demo Controls</span>
          </div>

          <div className="form-group">
            <label htmlFor="fault-family-select">Select Fault Family</label>
            <select
              id="fault-family-select"
              value={selectedFamily}
              onChange={(e) => {
                const fam = e.target.value;
                setSelectedFamily(fam);
                if (fam === "worker_paused") {
                  setSymptom("Checkout accepted order but fulfillment timed out: order remains pending");
                } else if (fam === "upstream_maintenance") {
                  setSymptom("Checkout failed: Upstream inventory is currently undergoing maintenance");
                } else {
                  setSymptom("Checkout failed: Inventory service returned HTTP 503");
                }
              }}
              disabled={demoLoading}
            >
              <option value="inventory_config">Wrong Inventory Configuration (P1/P2)</option>
              <option value="worker_paused">Paused Order Worker (P3)</option>
              <option value="upstream_maintenance">Upstream Maintenance (P3 Escalation)</option>
            </select>
          </div>

          <div className="btn-group">
            <button
              className="btn btn-primary"
              onClick={handleDemoCheckout}
              disabled={demoLoading}
            >
              Submit Demo Checkout
            </button>
            <button
              className="btn btn-danger"
              onClick={handleInjectFault}
              disabled={demoLoading}
            >
              Inject Fault
            </button>
            <button
              className="btn"
              onClick={handleResetSandbox}
              disabled={demoLoading}
            >
              Reset Sandbox
            </button>
          </div>

          {demoStatus && <div className="alert alert-info">{demoStatus}</div>}
        </section>

        {/* Start Investigation Panel */}
        <section className="panel">
          <div className="panel-header">
            <h2>Incident Management</h2>
            {incident && renderStateBadge(incident.state)}
          </div>

          <form onSubmit={handleStartIncident}>
            <div className="form-group">
              <label htmlFor="symptom-input">Reported Symptom</label>
              <textarea
                id="symptom-input"
                rows={2}
                value={symptom}
                onChange={(e) => setSymptom(e.target.value)}
                placeholder="Observed error symptom..."
                disabled={actionLoading || Boolean(incident && !isTerminal(incident.state))}
                required
              />
            </div>

            <div className="form-group" style={{ marginTop: "0.5rem" }}>
              <label htmlFor="request-id-input">Failed Request Key / ID (UUID)</label>
              <input
                id="request-id-input"
                type="text"
                value={requestId}
                onChange={(e) => setRequestId(e.target.value)}
                placeholder="e.g. 550e8400-e29b-41d4-a716-446655440000"
                disabled={actionLoading || Boolean(incident && !isTerminal(incident.state))}
              />
            </div>

            <div className="form-group" style={{ marginTop: "0.5rem" }}>
              <label htmlFor="order-id-input">Affected Order ID (Optional UUID)</label>
              <input
                id="order-id-input"
                type="text"
                value={orderId}
                onChange={(e) => setOrderId(e.target.value)}
                placeholder="e.g. 550e8400-e29b-41d4-a716-446655440001"
                disabled={actionLoading || Boolean(incident && !isTerminal(incident.state))}
              />
            </div>

            <div className="btn-group" style={{ marginTop: "1rem" }}>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={
                  actionLoading ||
                  Boolean(incident && !isTerminal(incident.state)) ||
                  !token
                }
              >
                {actionLoading ? "Investigating..." : "Start Agent Investigation"}
              </button>
              {incident && !isTerminal(incident.state) && (
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={handleCancel}
                  disabled={actionLoading}
                >
                  Cancel Incident
                </button>
              )}
              {incident && (
                <button
                  type="button"
                  className="btn"
                  onClick={handleCopySummary}
                  title="Copy safe incident audit summary without credentials"
                >
                  Copy Safe Audit Summary
                </button>
              )}
            </div>
          </form>

          {incident && (
            <div className="telemetry-bar" style={{ marginTop: "0.5rem" }}>
              <span className="telemetry-chip">
                Active: <strong>{(incident.active_ms / 1000).toFixed(1)}s</strong>
              </span>
              <span className="telemetry-chip">
                Model: <strong>{(totalModelDurationMs / 1000).toFixed(1)}s</strong>
                {avgTokensPerSec && ` (${avgTokensPerSec} tok/s)`}
              </span>
              <span className="telemetry-chip">
                Tools: <strong>{toolEvents.length} reads</strong> ({(totalToolDurationMs / 1000).toFixed(1)}s)
              </span>
              {incident.proposal && (
                <span className="telemetry-chip">
                  Approval Wait: <strong>{approvalWaitSeconds}s</strong>
                </span>
              )}
            </div>
          )}
        </section>

        {/* Active Approval Panel (Human Gate) */}
        {incident && incident.state === "awaiting_approval" && incident.proposal && (
          <section className="panel full-width">
            <div className="approval-box">
              <div className="panel-header" style={{ borderBottomColor: "var(--accent-amber)" }}>
                <h2 style={{ color: "var(--accent-amber)" }}>
                  Human Authorization Gate Required
                </h2>
                <span className="badge badge-amber">Action Pending Approval</span>
              </div>

              <p style={{ fontSize: "0.9rem" }}>
                The bounded agent investigation has diagnosed the root cause and proposed an
                immutable repair. Per architectural safety requirements, zero mutations can execute
                without your explicit, authenticated authorization.
              </p>

              <div className="kv-table">
                <div className="kv-key">Proposal ID</div>
                <div className="kv-val">{incident.proposal.proposal_id}</div>

                <div className="kv-key">Proposed Action</div>
                <div className="kv-val" style={{ color: "var(--accent-blue)", fontWeight: 700 }}>
                  {incident.proposal.payload.action}
                </div>

                {"endpoint_key" in incident.proposal.payload && (
                  <>
                    <div className="kv-key">Endpoint Key</div>
                    <div className="kv-val">{incident.proposal.payload.endpoint_key}</div>
                  </>
                )}

                {"worker_id" in incident.proposal.payload && (
                  <>
                    <div className="kv-key">Worker ID</div>
                    <div className="kv-val">{incident.proposal.payload.worker_id}</div>
                  </>
                )}

                <div className="kv-key">Target Revision Snapshot</div>
                <div className="kv-val">{incident.proposal.target_revision}</div>

                <div className="kv-key">Payload Digest (SHA-256)</div>
                <div className="kv-val">{incident.proposal.payload_digest}</div>

                <div className="kv-key">Expires At</div>
                <div className="kv-val">{new Date(incident.proposal.expires_at).toLocaleString()}</div>

                <div className="kv-key">Approval Wait</div>
                <div className="kv-val" style={{ fontFamily: "var(--font-mono)" }}>
                  {approvalWaitSeconds}s elapsed
                </div>

                <div className="kv-key">Expiry Countdown</div>
                <div className="kv-val">
                  {isExpired ? (
                    <span className="badge badge-red">EXPIRED</span>
                  ) : (
                    <span className="badge badge-amber" style={{ fontFamily: "var(--font-mono)" }}>
                      {countdownText} remaining
                    </span>
                  )}
                </div>
              </div>

              {isExpired && (
                <div className="alert alert-danger" style={{ marginTop: "0.5rem" }}>
                  <strong>Proposal Expired:</strong> Validity window (10 minutes) elapsed. Approval
                  is disabled to protect against stale mutations. Start a new investigation to gather current state.
                </div>
              )}

              <div className="btn-group">
                <button
                  className="btn btn-success"
                  onClick={() => handleDecision("approve")}
                  disabled={actionLoading || isExpired}
                >
                  Approve Repair Action
                </button>
                <button
                  className="btn btn-danger"
                  onClick={() => handleDecision("reject")}
                  disabled={actionLoading}
                >
                  Reject &amp; Halt
                </button>
              </div>
            </div>
          </section>
        )}

        {/* Execution Receipt Banner */}
        {executionReceipt && (
          <section
            className="panel full-width"
            style={{
              borderColor:
                executionReceipt.result === "applied"
                  ? "var(--accent-green)"
                  : "var(--accent-red)",
            }}
          >
            <div
              className={`alert ${
                executionReceipt.result === "applied" ? "alert-success" : "alert-danger"
              }`}
            >
              <div style={{ width: "100%" }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <strong>Execution Receipt ({executionReceipt.result})</strong>
                  <span
                    className={`badge ${
                      executionReceipt.result === "applied" ? "badge-green" : "badge-red"
                    }`}
                  >
                    {executionReceipt.result.toUpperCase()}
                  </span>
                </div>
                <div className="kv-table" style={{ marginTop: "0.5rem" }}>
                  <div className="kv-key">Execution Key</div>
                  <div className="kv-val">{executionReceipt.execution_key}</div>

                  <div className="kv-key">Revision Transition</div>
                  <div className="kv-val">
                    rev {executionReceipt.before_revision} &rarr; rev{" "}
                    {executionReceipt.after_revision}
                  </div>

                  <div className="kv-key">Payload Digest</div>
                  <div className="kv-val">{executionReceipt.payload_digest}</div>

                  <div className="kv-key">Recorded At</div>
                  <div className="kv-val">
                    {new Date(executionReceipt.recorded_at).toLocaleString()}
                  </div>
                </div>
              </div>
            </div>
          </section>
        )}

        {/* Outcome & Verification Banner */}
        {incident && incident.state === "verified_recovered" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-green)" }}>
            <div className="alert alert-success">
              <div style={{ width: "100%" }}>
                <strong>Functional Recovery Verified!</strong>
                <p style={{ marginTop: "0.25rem" }}>
                  Finite repair was executed and committed. The independent verifier successfully
                  submitted a brand-new checkout order and observed fulfillment through the sandbox
                  worker loop.
                </p>
                {incident.outcome &&
                  typeof incident.outcome === "object" &&
                  "verification" in incident.outcome && (
                    <div className="kv-table" style={{ marginTop: "0.5rem" }}>
                      <div className="kv-key">Verdict</div>
                      <div className="kv-val" style={{ color: "var(--accent-green)", fontWeight: 700 }}>
                        {String(
                          (incident.outcome.verification as Record<string, unknown>).verdict
                        ).toUpperCase()}
                      </div>
                      <div className="kv-key">Verified Order ID</div>
                      <div className="kv-val">
                        {String(
                          (incident.outcome.verification as Record<string, unknown>).order_id
                        )}
                      </div>
                      <div className="kv-key">Observed Status</div>
                      <div className="kv-val">
                        {String(
                          (incident.outcome.verification as Record<string, unknown>).observed_status
                        )}
                      </div>
                      <div className="kv-key">Evidence ID</div>
                      <div className="kv-val">
                        {String(
                          (incident.outcome.verification as Record<string, unknown>).evidence_id
                        )}
                      </div>
                    </div>
                  )}
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "verification_failed" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-red)" }}>
            <div className="alert alert-danger">
              <div style={{ width: "100%" }}>
                <strong>Verification Failed</strong>
                <p style={{ marginTop: "0.25rem" }}>
                  Repair was applied but independent checkout verification failed to achieve
                  fulfillment within the 10-second deadline.
                </p>
                {incident.outcome &&
                  typeof incident.outcome === "object" &&
                  "verification" in incident.outcome && (
                    <div className="kv-table" style={{ marginTop: "0.5rem" }}>
                      <div className="kv-key">Verdict</div>
                      <div className="kv-val" style={{ color: "var(--accent-red)", fontWeight: 700 }}>
                        {String(
                          (incident.outcome.verification as Record<string, unknown>).verdict
                        ).toUpperCase()}
                      </div>
                      <div className="kv-key">Order ID</div>
                      <div className="kv-val">
                        {String(
                          (incident.outcome.verification as Record<string, unknown>).order_id || "none"
                        )}
                      </div>
                      <div className="kv-key">Observed Status</div>
                      <div className="kv-val">
                        {String(
                          (incident.outcome.verification as Record<string, unknown>).observed_status
                        )}
                      </div>
                    </div>
                  )}
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "escalated" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-amber)" }}>
            <div className="alert alert-warning">
              <div style={{ width: "100%" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong>Incident Escalated (Unsupported Case / Upstream Condition)</strong>
                  <span className="badge badge-amber">ESCALATED</span>
                </div>
                <p style={{ marginTop: "0.25rem" }}>
                  The agent investigation diagnosed an external or non-repairable condition (e.g. upstream maintenance).
                  Per safety rules, no repair was proposed and zero execution claims or receipts were created.
                </p>
                {incident.outcome &&
                  typeof incident.outcome === "object" &&
                  "diagnosis" in incident.outcome && (
                    <div className="kv-table" style={{ marginTop: "0.5rem" }}>
                      <div className="kv-key">Diagnosis Summary</div>
                      <div className="kv-val">
                        {String(
                          (incident.outcome.diagnosis as Record<string, unknown>).summary || "Escalated by agent"
                        )}
                      </div>
                      <div className="kv-key">Uncertainty / Notes</div>
                      <div className="kv-val">
                        {String(
                          (incident.outcome.diagnosis as Record<string, unknown>).uncertainty || "None"
                        )}
                      </div>
                    </div>
                  )}
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "rejected" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-amber)" }}>
            <div className="alert alert-warning">
              <div style={{ width: "100%" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong>Proposal Rejected by Owner</strong>
                  <span className="badge badge-amber">REJECTED</span>
                </div>
                <p style={{ marginTop: "0.25rem" }}>
                  Owner rejected the proposed repair action. Exactly zero execution claims or mutations
                  were dispatched. Target sandbox revision remains unchanged. Safe to restart investigation or reset sandbox.
                </p>
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "cancelled" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-purple)" }}>
            <div className="alert alert-warning">
              <div style={{ width: "100%" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong>Incident Cancelled by Operator</strong>
                  <span className="badge badge-purple">CANCELLED</span>
                </div>
                <p style={{ marginTop: "0.25rem" }}>
                  Active investigation turn was terminated upon operator cancellation request.
                  Zero sandbox side effects or repair claims were dispatched. Safe to reset sandbox or start fresh checkout.
                </p>
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "expired" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-red)" }}>
            <div className="alert alert-danger">
              <div style={{ width: "100%" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong>Proposal Expired (TTL Exceeded)</strong>
                  <span className="badge badge-red">EXPIRED</span>
                </div>
                <p style={{ marginTop: "0.25rem" }}>
                  Proposal validity deadline (10 minutes) elapsed before owner authorization.
                  The target sandbox revision may have advanced. Action cannot be executed.
                  Start a new investigation to gather current target state.
                </p>
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "failed" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-red)" }}>
            <div className="alert alert-danger">
              <div style={{ width: "100%" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong>Investigation Failed / Provider Unavailable</strong>
                  <span className="badge badge-red">FAILED</span>
                </div>
                <p style={{ marginTop: "0.25rem" }}>
                  {incident.outcome &&
                  typeof incident.outcome === "object" &&
                  "error" in incident.outcome
                    ? String(incident.outcome.error)
                    : "Model provider (Ollama) is unreachable or timed out during diagnostic turn."}
                </p>
                <p style={{ marginTop: "0.25rem", fontSize: "0.8rem", color: "var(--text-secondary)" }}>
                  Next Action: Verify Ollama container status (`docker compose ps ollama`) and local endpoint connectivity, then start a new investigation.
                </p>
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "execution_unknown" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-amber)" }}>
            <div className="alert alert-warning">
              <div style={{ width: "100%" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong>Execution State Unknown (Unacknowledged Mutation)</strong>
                  <span className="badge badge-amber">UNKNOWN</span>
                </div>
                <p style={{ marginTop: "0.25rem" }}>
                  Mutation in flight or dispatch acknowledgment lost. Status of sandbox mutation is unconfirmed.
                  Per safety rules, automatic retries are forbidden to prevent duplicate side effects.
                </p>
                <p style={{ marginTop: "0.25rem", fontSize: "0.8rem", color: "var(--text-secondary)" }}>
                  Next Action: Click &apos;Reconcile Receipt Status&apos; below for a read-only GET lookup, or run narrow operator CLI:
                  <code style={{ display: "block", marginTop: "0.25rem" }}>
                    python -m incidentlab.cli resolve-unknown --incident {incident.id}
                  </code>
                </p>
                <button
                  className="btn btn-primary"
                  style={{ marginTop: "0.5rem" }}
                  onClick={handleReconcile}
                  disabled={actionLoading}
                >
                  {actionLoading ? "Reconciling..." : "Reconcile Receipt Status (Read-Only)"}
                </button>
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "closed_unknown" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-purple)" }}>
            <div className="alert alert-info">
              <div style={{ width: "100%" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong>Incident Closed (Unknown Outcome Resolved)</strong>
                  <span className="badge badge-purple">CLOSED UNKNOWN</span>
                </div>
                <p style={{ marginTop: "0.25rem" }}>
                  Operator confirmed quiescence and closed incident via CLI resolution procedure.
                  Active incident lock has been released. Inspect sandbox health before starting a new incident.
                </p>
              </div>
            </div>
          </section>
        )}

        {/* Diagnosis & Evidence Citations */}
        {incident && incident.diagnosis && (
          <section className="panel">
            <div className="panel-header">
              <h2>Agent Diagnosis</h2>
              <span className="badge badge-blue">
                {incident.diagnosis.confidence
                  ? `${incident.diagnosis.confidence.toUpperCase()} CONFIDENCE`
                  : "Grounded Findings"}
              </span>
            </div>

            <div className="form-group">
              <label>Diagnosis Summary</label>
              <p style={{ fontSize: "0.9rem", color: "var(--text-primary)" }}>
                {incident.diagnosis.summary || incident.diagnosis.hypothesis}
              </p>
            </div>

            {incident.diagnosis.decision && (
              <div className="form-group">
                <label>Recommended Decision</label>
                <p style={{ fontSize: "0.85rem", fontFamily: "var(--font-mono)", color: "var(--accent-blue)" }}>
                  {incident.diagnosis.decision}
                  {incident.diagnosis.repair && ` (${incident.diagnosis.repair.action})`}
                </p>
              </div>
            )}

            <div className="form-group">
              <label>Uncertainty Analysis</label>
              <p style={{ fontSize: "0.85rem", color: "var(--text-secondary)" }}>
                {incident.diagnosis.uncertainty || "None reported."}
              </p>
            </div>

            <div className="form-group">
              <label>Cited Evidence Items</label>
              <div className="citations-list">
                {(incident.diagnosis.evidence_ids || incident.diagnosis.cited_evidence_ids || []).map(
                  (evId) => (
                    <button
                      key={evId}
                      type="button"
                      className="citation-pill"
                      onClick={() => handleInspectEvidence(evId)}
                      aria-label={`Inspect evidence item ${evId}`}
                    >
                      {evId}
                    </button>
                  )
                )}
              </div>
            </div>

            <div className="form-group">
              <label>Cited Runbook Sections</label>
              <div className="citations-list">
                {(
                  incident.diagnosis.runbook_citations?.map(
                    (c) => `${c.runbook_id}:${c.section_id}`
                  ) ||
                  incident.diagnosis.cited_runbook_sections ||
                  []
                ).map((sec) => (
                  <span key={sec} className="citation-pill" style={{ cursor: "default" }}>
                    {sec}
                  </span>
                ))}
              </div>
            </div>
          </section>
        )}

        {/* Audit Event Timeline */}
        <section className={`panel ${incident && incident.diagnosis ? "" : "full-width"}`}>
          <div className="panel-header">
            <h2>Incident Audit Timeline</h2>
            <span className="badge badge-gray">{events.length} Events</span>
          </div>

          <div className="timeline">
            {events.length === 0 ? (
              <p style={{ color: "var(--text-muted)", fontSize: "0.85rem" }}>
                No events recorded yet. Start an investigation to observe the timeline.
              </p>
            ) : (
              events.map((ev) => (
                <div key={ev.seq} className="timeline-item">
                  <div className="timeline-header">
                    <span className="timeline-type">{ev.event_type}</span>
                    <span className="timeline-meta">
                      Seq #{ev.seq} • {new Date(ev.created_at).toLocaleTimeString()}
                      {ev.duration_ms !== null && ` • ${ev.duration_ms}ms`}
                      {ev.input_tokens !== null && ` • in:${ev.input_tokens}`}
                    </span>
                  </div>
                  <pre className="timeline-details">{JSON.stringify(ev.payload, null, 2)}</pre>
                </div>
              ))
            )}
          </div>
        </section>
      </div>

      {/* Evidence Inspector Modal */}
      {inspectingEvidence && (
        <div className="modal-overlay" onClick={() => setInspectingEvidence(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>Evidence: {inspectingEvidence.evidence_id}</h3>
              <button
                className="btn"
                style={{ padding: "0.2rem 0.5rem" }}
                onClick={() => setInspectingEvidence(null)}
              >
                ✕
              </button>
            </div>
            <div className="modal-body">
              <div className="kv-table">
                <div className="kv-key">Kind</div>
                <div className="kv-val">{inspectingEvidence.kind}</div>

                <div className="kv-key">Source</div>
                <div className="kv-val">
                  {inspectingEvidence.source} (v{inspectingEvidence.source_version})
                </div>

                <div className="kv-key">Observed At</div>
                <div className="kv-val">
                  {new Date(inspectingEvidence.observed_at).toLocaleString()}
                </div>
              </div>
              <div className="form-group" style={{ marginTop: "0.5rem" }}>
                <label>Sanitized Payload Data</label>
                <pre className="timeline-details" style={{ maxHeight: "300px" }}>
                  {JSON.stringify(inspectingEvidence.data, null, 2)}
                </pre>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
