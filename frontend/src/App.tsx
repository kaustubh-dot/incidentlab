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
  const [incident, setIncident] = useState<IncidentView | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

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

  // Poll incident and events
  const pollIncident = useCallback(async () => {
    if (!token || !incident || isTerminal(incident.state)) return;

    try {
      // 1. Fetch latest incident view
      const updated = await getIncident(token, incident.id);
      setIncident(updated);

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
      console.warn("Polling error:", err);
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
          `Checkout failed as expected: status=${res.status}, error=${res.error}`
        );
        setRequestId(res.request_id);
        if (res.error) {
          setSymptom(`Checkout failed: ${res.error}`);
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
    setEvents([]);
    nextSeqRef.current = 0;

    try {
      const reqIdToUse = requestId.trim() || crypto.randomUUID();
      const newInc = await startIncident(token, {
        symptom: symptom.trim(),
        request_id: reqIdToUse,
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
      await reconcileIncident(token, incident.id);
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
              onChange={(e) => setSelectedFamily(e.target.value)}
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
            </div>
          </form>
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

                <div className="kv-key">Target Revision</div>
                <div className="kv-val">{incident.proposal.target_revision}</div>

                <div className="kv-key">Payload Digest</div>
                <div className="kv-val">{incident.proposal.payload_digest}</div>

                <div className="kv-key">Expires At</div>
                <div className="kv-val">{new Date(incident.proposal.expires_at).toLocaleString()}</div>
              </div>

              <div className="btn-group">
                <button
                  className="btn btn-success"
                  onClick={() => handleDecision("approve")}
                  disabled={actionLoading}
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

        {/* Outcome & Verification Banner */}
        {incident && incident.state === "verified_recovered" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-green)" }}>
            <div className="alert alert-success">
              <div>
                <strong>Functional Recovery Verified!</strong>
                <p style={{ marginTop: "0.25rem" }}>
                  Finite repair was executed and committed. The independent verifier successfully
                  submitted a brand-new checkout order and observed fulfillment through the sandbox
                  worker loop.
                </p>
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "verification_failed" && (
          <section className="panel full-width" style={{ borderColor: "var(--accent-red)" }}>
            <div className="alert alert-danger">
              <div>
                <strong>Verification Failed</strong>
                <p style={{ marginTop: "0.25rem" }}>
                  Repair was applied but independent checkout verification failed to achieve
                  fulfillment within the 10-second deadline.
                </p>
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "rejected" && (
          <section className="panel full-width">
            <div className="alert alert-warning">
              <div>
                <strong>Proposal Rejected</strong>
                <p style={{ marginTop: "0.25rem" }}>
                  Owner rejected the proposed repair. Exactly zero execution claims dispatched.
                </p>
              </div>
            </div>
          </section>
        )}

        {incident && incident.state === "execution_unknown" && (
          <section className="panel full-width">
            <div className="alert alert-warning">
              <div>
                <strong>Execution State Unknown</strong>
                <p style={{ marginTop: "0.25rem" }}>
                  Mutation in flight or lost acknowledgment. Run receipt reconciliation to inspect
                  durable target state.
                </p>
                <button
                  className="btn btn-primary"
                  style={{ marginTop: "0.5rem" }}
                  onClick={handleReconcile}
                  disabled={actionLoading}
                >
                  Reconcile Receipt Status
                </button>
              </div>
            </div>
          </section>
        )}

        {/* Diagnosis & Evidence Citations */}
        {incident && incident.diagnosis && (
          <section className="panel">
            <div className="panel-header">
              <h2>Agent Diagnosis</h2>
              <span className="badge badge-blue">Grounded Findings</span>
            </div>

            <div className="form-group">
              <label>Hypothesis</label>
              <p style={{ fontSize: "0.9rem", color: "var(--text-primary)" }}>
                {incident.diagnosis.hypothesis}
              </p>
            </div>

            <div className="form-group">
              <label>Uncertainty Analysis</label>
              <p style={{ fontSize: "0.85rem", color: "var(--text-secondary)" }}>
                {incident.diagnosis.uncertainty}
              </p>
            </div>

            <div className="form-group">
              <label>Cited Evidence Items</label>
              <div className="citations-list">
                {incident.diagnosis.cited_evidence_ids.map((evId) => (
                  <button
                    key={evId}
                    type="button"
                    className="citation-pill"
                    onClick={() => handleInspectEvidence(evId)}
                  >
                    {evId}
                  </button>
                ))}
              </div>
            </div>

            <div className="form-group">
              <label>Cited Runbook Sections</label>
              <div className="citations-list">
                {incident.diagnosis.cited_runbook_sections.map((sec) => (
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
