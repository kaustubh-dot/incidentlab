/**
 * TypeScript type definitions mirroring IncidentLab backend OpenAPI / Pydantic models.
 */

export type IncidentState =
  | "created"
  | "investigating"
  | "awaiting_approval"
  | "approved"
  | "executing"
  | "verifying"
  | "verified_recovered"
  | "verification_failed"
  | "escalated"
  | "failed"
  | "rejected"
  | "expired"
  | "cancelled"
  | "execution_unknown"
  | "closed_unknown"
  | "stale";

export type RepairAction = "set_inventory_endpoint" | "resume_order_worker";

export interface SetInventoryEndpointPayload {
  action: "set_inventory_endpoint";
  endpoint_key: string;
}

export interface ResumeOrderWorkerPayload {
  action: "resume_order_worker";
  worker_id: string;
}

export type RepairPayload = SetInventoryEndpointPayload | ResumeOrderWorkerPayload;

export interface Proposal {
  proposal_id: string;
  incident_id: string;
  payload: RepairPayload;
  payload_digest: string;
  target_revision: number;
  created_at: string;
  expires_at: string;
  evidence_ids: string[];
}

export interface RunbookCitation {
  runbook_id: string;
  source_version: string;
  section_id: string;
}

export interface Diagnosis {
  summary?: string;
  confidence?: "high" | "medium" | "low";
  evidence_ids?: string[];
  runbook_citations?: RunbookCitation[];
  decision?: "propose_repair" | "escalate";
  repair?: RepairPayload | null;
  uncertainty?: string;
  hypothesis?: string;
  cited_evidence_ids?: string[];
  cited_runbook_sections?: string[];
}

export interface IncidentView {
  id: string;
  state: IncidentState;
  symptom: string;
  request_id: string;
  order_id: string | null;
  created_at: string;
  updated_at: string;
  active_ms: number;
  diagnosis: Diagnosis | null;
  proposal: Proposal | null;
  outcome: Record<string, unknown> | null;
}

export interface StartIncidentRequest {
  symptom: string;
  request_id: string;
  order_id?: string | null;
}

export interface ApprovalDecision {
  proposal_id: string;
  payload_digest: string;
  target_revision: number;
  decision: "approve" | "reject";
}

export interface ApprovalView {
  proposal_id: string;
  decision: "approve" | "reject";
  owner_id: string;
  reviewed_digest: string;
  reviewed_revision: number;
  decided_at: string;
}

export interface RepairReceipt {
  execution_key: string;
  payload_digest: string;
  result: "applied" | "precondition_failed";
  before_revision: number;
  after_revision: number;
  recorded_at: string;
}

export interface ExecutionView {
  execution_key: string;
  proposal_id: string;
  incident_id: string;
  state: "claimed" | "dispatching" | "applied" | "precondition_failed" | "unknown";
  payload_digest: string;
  expected_revision: number;
  payload: RepairPayload;
  claimed_at: string;
  dispatched_at: string | null;
  receipt: RepairReceipt | null;
  reconciled: boolean;
  error: string | null;
}

export interface EventItem {
  seq: number;
  event_type: string;
  timestamp: string;
  payload: Record<string, unknown>;
  duration_ms?: number | null;
  token_count?: number | null;
}

export interface EventPage {
  incident_id: string;
  events: EventItem[];
  next_seq: number;
  has_more: boolean;
}

export interface EvidenceItem {
  evidence_id: string;
  kind: "health" | "logs" | "config" | "probe" | "runbook" | "verification";
  source: string;
  source_version: string;
  section_id: string | null;
  observed_at: string;
  data: Record<string, unknown>;
}

export interface DemoResetResponse {
  status: string;
  revision: number;
}

export interface DemoFaultResponse {
  status: string;
  family: string;
  revision: number;
}

export interface DemoCheckoutResponse {
  request_id: string;
  order_id: string | null;
  status: string;
  error?: string | null;
}

export interface ApiError {
  error: {
    code: string;
    message: string;
    request_id: string | null;
    retryable: boolean;
  };
}
