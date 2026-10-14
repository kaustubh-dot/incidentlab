"""FastAPI application lifecycle, owner authentication, and control routes."""

from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from incidentlab.config import Settings
from incidentlab.db import get_control_connection
from incidentlab.events import read_events
from incidentlab.executor import CheckoutError, submit_checkout, wait_for_order_fulfillment
from incidentlab.schemas import (
    DemoCheckoutRequest,
    DemoCheckoutResponse,
    DemoResetResponse,
    EventPage,
    EvidenceItem,
    IncidentView,
)


def get_current_settings(request: Request) -> Settings:
    return request.app.state.settings


def verify_owner_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """Verify owner bearer token with constant-time comparison."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Valid owner bearer token required",
                    "request_id": None,
                    "retryable": False,
                }
            },
        )

    token = authorization[len("Bearer ") :].strip()
    settings: Settings = request.app.state.settings
    if not hmac.compare_digest(token, settings.il_owner_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid owner bearer token",
                    "request_id": None,
                    "retryable": False,
                }
            },
        )
    return "owner"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI control application."""
    if settings is None:
        try:
            settings = Settings.from_env()
        except Exception:
            # Allow creating app without env during unit tests
            pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup checks
        yield
        # Shutdown cleanup

    app = FastAPI(
        title="IncidentLab Control API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException):
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "http_error",
                    "message": str(exc.detail),
                    "request_id": None,
                    "retryable": False,
                }
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(_: Request, exc: Exception):
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An internal server error occurred",
                    "request_id": None,
                    "retryable": False,
                }
            },
        )

    # --- Unauthenticated Liveness ---

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "control"}

    # --- Authenticated Incident and Evidence Routes ---

    @app.get(
        "/api/v1/incidents/{incident_id}",
        response_model=IncidentView,
        dependencies=[Depends(verify_owner_token)],
    )
    async def get_incident(incident_id: UUID, req: Request):
        cfg = req.app.state.settings
        try:
            with get_control_connection(cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, state, symptom, request_id, order_id, created_at,
                               updated_at, active_ms, outcome
                        FROM control.incidents
                        WHERE id = %s
                        """,
                        (str(incident_id),),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail={
                                "error": {
                                    "code": "incident_not_found",
                                    "message": f"Incident {incident_id} not found",
                                    "request_id": None,
                                    "retryable": False,
                                }
                            },
                        )
                    outcome = row[8]
                    if isinstance(outcome, str):
                        outcome = json.loads(outcome)
                    return IncidentView(
                        id=row[0],
                        state=row[1],
                        symptom=row[2],
                        request_id=row[3],
                        order_id=row[4],
                        created_at=row[5],
                        updated_at=row[6],
                        active_ms=row[7],
                        diagnosis=None,
                        proposal=None,
                        outcome=outcome,
                    )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": {
                        "code": "database_unavailable",
                        "message": f"Failed to read incident: {exc}",
                        "request_id": None,
                        "retryable": True,
                    }
                },
            ) from exc

    @app.get(
        "/api/v1/incidents/{incident_id}/events",
        response_model=EventPage,
        dependencies=[Depends(verify_owner_token)],
    )
    async def get_incident_events(
        incident_id: UUID,
        req: Request,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=100),
    ):
        cfg = req.app.state.settings
        try:
            with get_control_connection(cfg) as conn:
                with conn.cursor() as cur:
                    return read_events(cur, incident_id, after_seq=after_seq, limit=limit)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": {
                        "code": "database_unavailable",
                        "message": f"Failed to read events: {exc}",
                        "request_id": None,
                        "retryable": True,
                    }
                },
            ) from exc

    @app.get(
        "/api/v1/incidents/{incident_id}/evidence/{evidence_id}",
        response_model=EvidenceItem,
        dependencies=[Depends(verify_owner_token)],
    )
    async def get_incident_evidence(incident_id: UUID, evidence_id: str, req: Request):
        cfg = req.app.state.settings
        try:
            with get_control_connection(cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT evidence_id, kind, source, source_version, section_id,
                               observed_at, data
                        FROM control.evidence
                        WHERE incident_id = %s AND evidence_id = %s
                        """,
                        (str(incident_id), evidence_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail={
                                "error": {
                                    "code": "evidence_not_found",
                                    "message": f"Evidence {evidence_id} not found",
                                    "request_id": None,
                                    "retryable": False,
                                }
                            },
                        )
                    data = row[6]
                    if isinstance(data, str):
                        data = json.loads(data)
                    return EvidenceItem(
                        evidence_id=row[0],
                        kind=row[1],
                        source=row[2],
                        source_version=row[3],
                        section_id=row[4],
                        observed_at=row[5],
                        data=data,
                    )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": {
                        "code": "database_unavailable",
                        "message": f"Failed to read evidence: {exc}",
                        "request_id": None,
                        "retryable": True,
                    }
                },
            ) from exc

    # --- Owner Demo Controls ---

    @app.post(
        "/api/v1/demo/reset",
        response_model=DemoResetResponse,
        dependencies=[Depends(verify_owner_token)],
    )
    async def demo_reset(req: Request):
        cfg = req.app.state.settings
        # Refuse reset if a nonterminal incident exists
        try:
            with get_control_connection(cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, state
                        FROM control.incidents
                        WHERE state NOT IN (
                            'verified_recovered', 'verification_failed', 'escalated',
                            'rejected', 'cancelled', 'closed_unknown'
                        )
                        LIMIT 1
                        """
                    )
                    row = cur.fetchone()
                    if row:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "error": {
                                    "code": "active_incident_exists",
                                    "message": (
                                        f"Cannot reset demo while incident {row[0]} "
                                        f"is in active state {row[1]}"
                                    ),
                                    "request_id": None,
                                    "retryable": False,
                                }
                            },
                        )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": {
                        "code": "database_unavailable",
                        "message": f"Failed to query incidents: {exc}",
                        "request_id": None,
                        "retryable": True,
                    }
                },
            ) from exc

        # Forward reset to sandbox operator endpoint
        reset_url = f"{cfg.il_sandbox_base_url}/operator/reset"
        headers = {"Authorization": f"Bearer {cfg.il_sandbox_admin_token}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as ac:
                resp = await ac.post(reset_url, headers=headers)
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail={
                            "error": {
                                "code": "sandbox_reset_failed",
                                "message": (
                                    f"Sandbox reset returned status {resp.status_code}: {resp.text}"
                                ),
                                "request_id": None,
                                "retryable": False,
                            }
                        },
                    )
                data = resp.json()
                return DemoResetResponse(
                    status=data.get("status", "reset"),
                    revision=data["revision"],
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error": {
                        "code": "sandbox_unavailable",
                        "message": f"Failed to communicate with sandbox: {exc}",
                        "request_id": None,
                        "retryable": True,
                    }
                },
            ) from exc

    @app.post(
        "/api/v1/demo/checkout",
        response_model=DemoCheckoutResponse,
        dependencies=[Depends(verify_owner_token)],
    )
    async def demo_checkout(req: Request, payload: DemoCheckoutRequest | None = None):
        cfg = req.app.state.settings
        request_key = payload.request_key if (payload and payload.request_key) else uuid4()

        try:
            accepted = await submit_checkout(request_key, settings=cfg)
        except CheckoutError as exc:
            req_id_val = UUID(exc.request_id) if exc.request_id else uuid4()
            return DemoCheckoutResponse(
                request_id=req_id_val,
                order_id=None,
                status="failed",
                error=exc.message,
            )

        try:
            order = await wait_for_order_fulfillment(
                accepted.order_id, settings=cfg, timeout_seconds=10.0
            )
            return DemoCheckoutResponse(
                request_id=accepted.request_id,
                order_id=order.order_id,
                status=order.status,
            )
        except Exception as exc:
            return DemoCheckoutResponse(
                request_id=accepted.request_id,
                order_id=accepted.order_id,
                status="pending",
                error=f"Polling error: {exc}",
            )

    return app


# Module-level default application for uvicorn
app = create_app()
