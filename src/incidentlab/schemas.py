"""Strict shared Pydantic data schemas for IncidentLab."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BaseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Public Error Schema ---


class ApiErrorDetail(BaseSchema):
    code: str
    message: str
    request_id: str | None = None
    retryable: bool = False


class ApiErrorResponse(BaseSchema):
    error: ApiErrorDetail


# --- Evidence Schemas ---


EvidenceKind = Literal["health", "logs", "config", "probe", "runbook", "verification"]


class EvidenceItem(BaseSchema):
    evidence_id: str
    kind: EvidenceKind
    observed_at: datetime
    source: str
    source_version: str | None = None
    section_id: str | None = None
    data: dict[str, Any]


# --- Repair Payloads (Discriminated Union) ---


class SetInventoryEndpointPayload(BaseSchema):
    action: Literal["set_inventory_endpoint"] = "set_inventory_endpoint"
    endpoint_key: Literal["inventory-local"] = "inventory-local"


class ResumeOrderWorkerPayload(BaseSchema):
    action: Literal["resume_order_worker"] = "resume_order_worker"
    worker_id: Literal["checkout-worker"] = "checkout-worker"


RepairPayload = Annotated[
    SetInventoryEndpointPayload | ResumeOrderWorkerPayload,
    Field(discriminator="action"),
]


class RunbookCitation(BaseSchema):
    runbook_id: str
    source_version: str
    section_id: str


class Diagnosis(BaseSchema):
    summary: str = Field(min_length=1, max_length=1200)
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    runbook_citations: list[RunbookCitation] = Field(min_length=1, max_length=3)
    decision: Literal["propose_repair", "escalate"]
    repair: RepairPayload | None = None
    uncertainty: str = Field(default="", max_length=800)

    @model_validator(mode="after")
    def validate_decision_repair(self) -> Diagnosis:
        if self.decision == "escalate" and self.repair is not None:
            raise ValueError("Repair payload must be null when decision is escalate")
        if self.decision == "propose_repair" and self.repair is None:
            raise ValueError("Repair payload is required when decision is propose_repair")
        if self.confidence != "high" and not self.uncertainty.strip():
            raise ValueError("Uncertainty explanation is required when confidence is not high")
        return self


class Proposal(BaseSchema):
    proposal_id: UUID
    incident_id: UUID
    payload: RepairPayload
    payload_digest: str
    target_revision: int
    created_at: datetime
    expires_at: datetime
    evidence_ids: list[str]


class ApprovalDecision(BaseSchema):
    proposal_id: UUID
    payload_digest: str
    target_revision: int
    decision: Literal["approve", "reject"]


class ApprovalView(BaseSchema):
    proposal_id: UUID
    decision: str
    owner_id: str
    reviewed_digest: str
    reviewed_revision: int
    decided_at: datetime


class ExecutionClaim(BaseSchema):
    execution_key: UUID
    proposal_id: UUID
    incident_id: UUID
    state: Literal["claimed", "dispatching", "applied", "precondition_failed", "unknown"]
    payload_digest: str
    expected_revision: int
    payload: RepairPayload
    claimed_at: datetime
    dispatched_at: datetime | None = None
    receipt: dict[str, Any] | None = None


class RepairRequest(BaseSchema):
    execution_key: UUID
    incident_id: UUID
    proposal_id: UUID
    payload: RepairPayload
    payload_digest: str
    expected_revision: int


class RepairReceipt(BaseSchema):
    execution_key: UUID
    payload_digest: str
    result: Literal["applied", "precondition_failed"]
    before_revision: int
    after_revision: int
    recorded_at: datetime


class VerificationResult(BaseSchema):
    verdict: Literal["pass", "fail", "error"]
    request_id: UUID
    order_id: UUID | None = None
    observed_status: Literal["fulfilled", "pending", "unavailable", "unknown"]
    started_at: datetime
    finished_at: datetime
    evidence_id: str


# --- Incident & Event API Views ---


class StartIncident(BaseSchema):
    symptom: str = Field(min_length=1, max_length=2000)
    request_id: UUID
    order_id: UUID | None = None


class IncidentView(BaseSchema):
    id: UUID
    state: str
    symptom: str
    request_id: UUID
    order_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    active_ms: int
    diagnosis: Diagnosis | None = None
    proposal: Proposal | None = None
    outcome: dict[str, Any] | None = None


class EventItem(BaseSchema):
    seq: int
    event_type: str
    timestamp: datetime
    payload: dict[str, Any]
    duration_ms: int | None = None
    token_count: int | None = None


class EventPage(BaseSchema):
    events: list[EventItem]
    next_seq: int
    has_more: bool


# --- Owner Demo Routes Schemas ---


class DemoResetResponse(BaseSchema):
    status: str = "reset"
    revision: int


class DemoCheckoutRequest(BaseSchema):
    request_key: UUID | None = None


class DemoCheckoutResponse(BaseSchema):
    request_id: UUID
    order_id: UUID | None = None
    status: str
    error: str | None = None


class DemoFaultRequest(BaseSchema):
    family: Literal["inventory_config", "worker_paused", "upstream_maintenance"]
    variant_seed: int = 1


class DemoFaultResponse(BaseSchema):
    status: str = "injected"
    family: str
    revision: int
