"""Strict Pydantic schemas for sandbox checkout, orders, diagnostics, and repairs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from incidentlab.schemas import BaseSchema


class CheckoutRequest(BaseSchema):
    request_key: UUID
    sku: Literal["SKU-1"] = "SKU-1"
    quantity: Literal[1] = 1


class CheckoutAccepted(BaseSchema):
    request_id: UUID
    order_id: UUID
    status: Literal["pending", "fulfilled"] = "pending"


class OrderView(BaseSchema):
    order_id: UUID
    request_id: UUID
    status: Literal["pending", "fulfilled"]
    created_at: datetime
    fulfilled_at: datetime | None = None


class DiagnosticsHealth(BaseSchema):
    status: str = "ok"
    timestamp: datetime
    target_revision: int
    database_reachable: bool


class DiagnosticsConfig(BaseSchema):
    endpoint_key: str
    worker_id: str
    worker_running: bool
    target_revision: int


class LogEntry(BaseSchema):
    seq: int
    timestamp: datetime
    request_id: UUID | None = None
    order_id: UUID | None = None
    event_kind: str
    severity: str = "INFO"
    message: str = Field(..., max_length=512)
    data: dict[str, Any] | None = None
    truncated: bool = False


class DiagnosticsProbe(BaseSchema):
    observed_at: datetime
    inventory_status: int
    inventory_reachable: bool
    order: OrderView | None = None


class OperatorResetResponse(BaseSchema):
    status: str = "reset"
    revision: int


class OperatorInjectRequest(BaseSchema):
    family: Literal["inventory_config", "worker_paused", "upstream_maintenance"]
    variant_seed: int = 1


class OperatorInjectResponse(BaseSchema):
    status: str = "injected"
    family: str
    revision: int
