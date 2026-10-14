"""Sandbox FastAPI application exposing checkout, diagnostics, and repairs."""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from incidentlab.db import get_sandbox_connection
from incidentlab.sandbox import service
from incidentlab.sandbox.schemas import (
    CheckoutAccepted,
    CheckoutRequest,
    DiagnosticsConfig,
    DiagnosticsHealth,
    DiagnosticsProbe,
    LogEntry,
    OperatorInjectRequest,
    OperatorInjectResponse,
    OperatorResetResponse,
    OrderView,
)
from incidentlab.sandbox.worker import run_worker_loop
from incidentlab.schemas import (
    RepairReceipt,
    RepairRequest,
)


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    il_sandbox_port: int = Field(default=8001)
    il_sandbox_database_url: str
    il_sandbox_read_token: str
    il_sandbox_write_token: str
    il_sandbox_admin_token: str
    il_log_level: str = Field(default="INFO")

    @classmethod
    def from_env(cls) -> SandboxSettings:
        required = [
            "IL_SANDBOX_DATABASE_URL",
            "IL_SANDBOX_READ_TOKEN",
            "IL_SANDBOX_WRITE_TOKEN",
            "IL_SANDBOX_ADMIN_TOKEN",
        ]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError(f"Missing required sandbox settings: {', '.join(missing)}")
        return cls(
            il_sandbox_port=int(os.environ.get("IL_SANDBOX_PORT", "8001")),
            il_sandbox_database_url=os.environ["IL_SANDBOX_DATABASE_URL"],
            il_sandbox_read_token=os.environ["IL_SANDBOX_READ_TOKEN"],
            il_sandbox_write_token=os.environ["IL_SANDBOX_WRITE_TOKEN"],
            il_sandbox_admin_token=os.environ["IL_SANDBOX_ADMIN_TOKEN"],
            il_log_level=os.environ.get("IL_LOG_LEVEL", "INFO"),
        )


def _check_bearer_token(authorization: str | None, expected_token: str, token_name: str) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": f"Valid {token_name} bearer token required",
                    "request_id": None,
                    "retryable": False,
                }
            },
        )
    token = authorization[len("Bearer ") :].strip()
    if not hmac.compare_digest(token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": f"Invalid {token_name} bearer token",
                    "request_id": None,
                    "retryable": False,
                }
            },
        )


def verify_read_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    settings: SandboxSettings = request.app.state.settings
    _check_bearer_token(authorization, settings.il_sandbox_read_token, "read")
    return "read"


def verify_write_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    settings: SandboxSettings = request.app.state.settings
    _check_bearer_token(authorization, settings.il_sandbox_write_token, "write")
    return "write"


def verify_read_or_write_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    settings: SandboxSettings = request.app.state.settings
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Valid read or write bearer token required",
                    "request_id": None,
                    "retryable": False,
                }
            },
        )
    token = authorization[len("Bearer ") :].strip()
    if hmac.compare_digest(token, settings.il_sandbox_read_token):
        return "read"
    if hmac.compare_digest(token, settings.il_sandbox_write_token):
        return "write"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": {
                "code": "unauthorized",
                "message": "Invalid read or write bearer token",
                "request_id": None,
                "retryable": False,
            }
        },
    )


def verify_admin_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    settings: SandboxSettings = request.app.state.settings
    _check_bearer_token(authorization, settings.il_sandbox_admin_token, "admin")
    return "admin"


def create_sandbox_app(settings: SandboxSettings | None = None) -> FastAPI:
    if settings is None:
        try:
            settings = SandboxSettings.from_env()
        except Exception:
            pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        shutdown_event = asyncio.Event()
        worker_task = None
        if hasattr(app.state, "settings") and app.state.settings.il_sandbox_database_url:
            def get_conn():
                return get_sandbox_connection(app.state.settings.il_sandbox_database_url)
            worker_task = asyncio.create_task(run_worker_loop(get_conn, shutdown_event))
        yield
        shutdown_event.set()
        if worker_task:
            await worker_task

    app = FastAPI(
        title="IncidentLab Sandbox",
        version="0.1.0",
        lifespan=lifespan,
    )
    if settings is not None:
        app.state.settings = settings

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "error",
                    "message": str(exc.detail),
                    "request_id": None,
                    "retryable": False,
                }
            },
        )

    # --- Unauthenticated Health Probe ---

    @app.get("/healthz", response_model=dict[str, str])
    async def healthz():
        return {"status": "ok"}

    # --- Dependency Mock Routes (loopback protected) ---

    @app.get("/dependency/inventory/{sku}")
    async def dependency_inventory(sku: str, request: Request):
        if sku != "SKU-1":
            raise HTTPException(status_code=404, detail="Unknown SKU")
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            state = service.get_target_state(conn)
            if state["maintenance"]:
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={
                        "error": "upstream_maintenance",
                        "detail": "Upstream inventory is currently undergoing maintenance",
                    },
                )
        return {"sku": "SKU-1", "available": True, "stock": 100}

    @app.get("/dependency/unavailable/{sku}")
    async def dependency_unavailable(sku: str):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "service_unavailable",
                "detail": "Legacy inventory endpoint is deprecated and unavailable",
            },
        )

    # --- Business Route: Checkout ---

    @app.post("/checkout", status_code=status.HTTP_202_ACCEPTED, response_model=CheckoutAccepted)
    async def checkout(
        payload: CheckoutRequest,
        request: Request,
        _auth: str = Depends(verify_write_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            accepted, error_resp, status_code = await service.create_checkout(
                conn=conn,
                request_key=payload.request_key,
                sku=payload.sku,
                quantity=payload.quantity,
                port=settings.il_sandbox_port,
            )
            if error_resp:
                raise HTTPException(
                    status_code=status_code,
                    detail=error_resp.model_dump(),
                )
            return accepted

    # --- Order Readback ---

    @app.get("/orders/{order_id}", response_model=OrderView)
    async def get_order(
        order_id: UUID,
        request: Request,
        _auth: str = Depends(verify_read_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            order = service.get_order(conn, order_id)
            if not order:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": {
                            "code": "order_not_found",
                            "message": f"Order {order_id} not found",
                            "request_id": None,
                            "retryable": False,
                        }
                    },
                )
            return order

    # --- Diagnostics ---

    @app.get("/diagnostics/health", response_model=DiagnosticsHealth)
    async def diagnostics_health(
        request: Request,
        _auth: str = Depends(verify_read_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            return service.get_diagnostics_health(conn)

    @app.get("/diagnostics/config", response_model=DiagnosticsConfig)
    async def diagnostics_config(
        request: Request,
        _auth: str = Depends(verify_read_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            return service.get_diagnostics_config(conn)

    @app.get("/diagnostics/logs", response_model=list[LogEntry])
    async def diagnostics_logs(
        request: Request,
        request_id: UUID | None = Query(default=None),
        limit: int = Query(default=40, ge=1, le=40),
        _auth: str = Depends(verify_read_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            return service.get_diagnostics_logs(conn, request_id=request_id, limit=limit)

    @app.get("/diagnostics/probe", response_model=DiagnosticsProbe)
    async def diagnostics_probe(
        request: Request,
        order_id: UUID | None = Query(default=None),
        _auth: str = Depends(verify_read_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            state = service.get_target_state(conn)
            endpoint_key = state["endpoint_key"]

            status_code, _ = await service.check_inventory_endpoint(
                endpoint_key, port=settings.il_sandbox_port
            )
            order_view = None
            if order_id:
                order_view = service.get_order(conn, order_id)

            return DiagnosticsProbe(
                observed_at=datetime.now(UTC),
                inventory_status=status_code,
                inventory_reachable=(status_code == 200),
                order=order_view,
            )

    # --- Repairs ---

    @app.post("/repairs", response_model=RepairReceipt)
    async def apply_repair(
        repair_req: RepairRequest,
        request: Request,
        _auth: str = Depends(verify_write_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            try:
                receipt, _ = service.apply_repair(conn, repair_req)
                return receipt
            except ValueError as ve:
                if str(ve) == "execution_key_conflict":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": {
                                "code": "execution_key_conflict",
                                "message": "Execution key already used with different digest",
                                "request_id": None,
                                "retryable": False,
                            }
                        },
                    )
                raise

    @app.get("/repairs/{execution_key}", response_model=RepairReceipt)
    async def get_repair(
        execution_key: UUID,
        request: Request,
        _auth: str = Depends(verify_read_or_write_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            receipt = service.get_repair_receipt(conn, execution_key)
            if not receipt:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": {
                            "code": "receipt_not_found",
                            "message": f"Receipt for {execution_key} not found",
                            "request_id": None,
                            "retryable": False,
                        }
                    },
                )
            return receipt

    # --- Operator Scenario Controls ---

    @app.post("/operator/reset", response_model=OperatorResetResponse)
    async def operator_reset(
        request: Request,
        _auth: str = Depends(verify_admin_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            new_rev = service.reset_sandbox(conn)
            return OperatorResetResponse(status="reset", revision=new_rev)

    @app.post("/operator/inject", response_model=OperatorInjectResponse)
    async def operator_inject(
        inject_req: OperatorInjectRequest,
        request: Request,
        _auth: str = Depends(verify_admin_token),
    ):
        settings: SandboxSettings = request.app.state.settings
        with get_sandbox_connection(settings.il_sandbox_database_url) as conn:
            new_rev = service.inject_fault(
                conn, inject_req.family, inject_req.variant_seed
            )
            return OperatorInjectResponse(
                status="injected",
                family=inject_req.family,
                revision=new_rev,
            )

    return app


app = create_sandbox_app()
