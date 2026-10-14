"""FastAPI application lifecycle, owner authentication, and control routes."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg.errors
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from incidentlab.approvals import ApprovalError, claim_execution, decide_proposal
from incidentlab.config import Settings
from incidentlab.db import get_control_connection
from incidentlab.events import read_events, record_event
from incidentlab.executor import (
    CheckoutError,
    execute_claim,
    reconcile_execution,
    submit_checkout,
    wait_for_order_fulfillment,
)
from incidentlab.graph import run_investigation
from incidentlab.schemas import (
    ApprovalDecision,
    ApprovalView,
    DemoCheckoutRequest,
    DemoCheckoutResponse,
    DemoFaultRequest,
    DemoFaultResponse,
    DemoResetResponse,
    Diagnosis,
    EventPage,
    EvidenceItem,
    ExecutionClaim,
    ExecutionView,
    IncidentView,
    Proposal,
    StartIncident,
)
from incidentlab.verifier import verify_checkout

logger = logging.getLogger(__name__)


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

    def _load_incident_view(conn: Any, incident_id: UUID) -> IncidentView | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, state, symptom, request_id, order_id, created_at,
                       updated_at, active_ms, outcome
                FROM control.incidents
                WHERE id = %s
                """,
                (incident_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            outcome = row[8]
            if isinstance(outcome, str):
                outcome = json.loads(outcome)

            cur.execute(
                """
                SELECT id, incident_id, payload, payload_digest, target_revision,
                       created_at, expires_at, evidence_ids
                FROM control.proposals
                WHERE incident_id = %s
                """,
                (incident_id,),
            )
            p_row = cur.fetchone()
            proposal = None
            if p_row:
                p_payload = p_row[2] if isinstance(p_row[2], dict) else json.loads(p_row[2])
                p_evs = p_row[7] if isinstance(p_row[7], list) else json.loads(p_row[7])
                proposal = Proposal(
                    proposal_id=p_row[0],
                    incident_id=p_row[1],
                    payload=p_payload,
                    payload_digest=p_row[3],
                    target_revision=p_row[4],
                    created_at=p_row[5],
                    expires_at=p_row[6],
                    evidence_ids=p_evs,
                )

            diagnosis = None
            if isinstance(outcome, dict) and "diagnosis" in outcome:
                try:
                    diagnosis = Diagnosis.model_validate(outcome["diagnosis"])
                except Exception:
                    diagnosis = None

            return IncidentView(
                id=row[0],
                state=row[1],
                symptom=row[2],
                request_id=row[3],
                order_id=row[4],
                created_at=row[5],
                updated_at=row[6],
                active_ms=row[7],
                diagnosis=diagnosis,
                proposal=proposal,
                outcome=outcome,
            )

    @app.get(
        "/api/v1/incidents/{incident_id}",
        response_model=IncidentView,
        dependencies=[Depends(verify_owner_token)],
    )
    async def get_incident(incident_id: UUID, req: Request):
        cfg = req.app.state.settings
        try:
            with get_control_connection(cfg) as conn:
                view = _load_incident_view(conn, incident_id)
                if not view:
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
                return view
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

    @app.post(
        "/api/v1/incidents",
        response_model=IncidentView,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(verify_owner_token)],
    )
    async def create_incident(payload: StartIncident, req: Request):
        cfg = req.app.state.settings
        incident_id = uuid4()
        try:
            with get_control_connection(cfg) as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO control.incidents (id, state, symptom, request_id, order_id)
                            VALUES (%s, 'investigating', %s, %s, %s)
                            """,
                            (
                                incident_id,
                                payload.symptom,
                                payload.request_id,
                                payload.order_id,
                            ),
                        )
                    conn.commit()
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": {
                                "code": "active_incident_conflict",
                                "message": "Another active incident is already in progress",
                                "request_id": None,
                                "retryable": False,
                            }
                        },
                    )

                record_event(
                    conn,
                    incident_id,
                    "investigation_started",
                    {
                        "symptom": payload.symptom,
                        "request_id": str(payload.request_id),
                        "order_id": str(payload.order_id) if payload.order_id else None,
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
                        "message": f"Failed to initialize incident: {exc}",
                        "request_id": None,
                        "retryable": True,
                    }
                },
            ) from exc

        # Advance LangGraph investigation flow
        await run_investigation(incident_id, settings=cfg)

        with get_control_connection(cfg) as conn:
            view = _load_incident_view(conn, incident_id)
            if not view:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": {
                            "code": "incident_not_found",
                            "message": f"Incident {incident_id} not found after investigation",
                            "request_id": None,
                            "retryable": False,
                        }
                    },
                )
            return view

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

    @app.post(
        "/api/v1/incidents/{incident_id}/decision",
        response_model=ApprovalView,
        dependencies=[Depends(verify_owner_token)],
    )
    async def submit_decision(
        incident_id: UUID,
        decision_payload: ApprovalDecision,
        req: Request,
    ) -> ApprovalView:
        cfg = req.app.state.settings
        try:
            approval_view = await decide_proposal(
                incident_id=incident_id,
                owner_id="owner",
                decision=decision_payload,
                settings=cfg,
            )
            if decision_payload.decision == "approve":
                claim = await claim_execution(
                    incident_id=incident_id,
                    proposal_id=decision_payload.proposal_id,
                    settings=cfg,
                )

                if claim.state == "claimed":

                    async def _dispatch_and_verify(
                        c: ExecutionClaim, inc_id: UUID, s: Settings
                    ):
                        try:
                            receipt = await execute_claim(c, settings=s)
                            if receipt.result == "applied":
                                req_key = uuid4()
                                await verify_checkout(inc_id, req_key, settings=s)
                        except Exception as err:
                            logger.error("Background repair/verify failed: %s", err)

                    asyncio.create_task(_dispatch_and_verify(claim, incident_id, cfg))

            return approval_view
        except ApprovalError as err:
            raise HTTPException(
                status_code=err.status_code,
                detail={
                    "error": {
                        "code": err.code,
                        "message": err.message,
                        "request_id": None,
                        "retryable": False,
                    }
                },
            ) from err
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": {
                        "code": "approval_failed",
                        "message": f"Failed to record approval decision: {exc}",
                        "request_id": None,
                        "retryable": False,
                    }
                },
            ) from exc

    @app.post(
        "/api/v1/incidents/{incident_id}/reconcile",
        response_model=ExecutionView,
        dependencies=[Depends(verify_owner_token)],
    )
    async def reconcile_incident(
        incident_id: UUID,
        req: Request,
    ) -> ExecutionView:
        cfg = req.app.state.settings
        with get_control_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT execution_key
                    FROM control.executions
                    WHERE incident_id = %s
                    ORDER BY claimed_at DESC
                    LIMIT 1
                    """,
                    (incident_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={
                            "error": {
                                "code": "execution_not_found",
                                "message": f"No execution found for incident {incident_id}",
                                "request_id": None,
                                "retryable": False,
                            }
                        },
                    )
                exec_key = row[0]

        try:
            view = await reconcile_execution(exec_key, settings=cfg)
            if view.receipt and view.receipt.result == "applied":
                with get_control_connection(cfg) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT state FROM control.incidents WHERE id = %s",
                            (incident_id,),
                        )
                        inc_row = cur.fetchone()
                        if inc_row and inc_row[0] in ("verifying", "execution_unknown"):

                            async def _verify_bg(inc_id: UUID, s: Settings):
                                try:
                                    await verify_checkout(inc_id, uuid4(), settings=s)
                                except Exception as err:
                                    logger.error(
                                        "Reconcile background verification failed: %s",
                                        err,
                                    )

                            asyncio.create_task(_verify_bg(incident_id, cfg))

            return view
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": {
                        "code": "reconcile_failed",
                        "message": f"Failed to reconcile execution: {exc}",
                        "request_id": None,
                        "retryable": False,
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
                            'failed', 'rejected', 'expired', 'cancelled', 'closed_unknown'
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
        "/api/v1/demo/fault",
        response_model=DemoFaultResponse,
        dependencies=[Depends(verify_owner_token)],
    )
    async def demo_fault(payload: DemoFaultRequest, req: Request):
        cfg = req.app.state.settings
        # Refuse fault injection if a nonterminal incident exists
        try:
            with get_control_connection(cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, state
                        FROM control.incidents
                        WHERE state NOT IN (
                            'verified_recovered', 'verification_failed', 'escalated',
                            'failed', 'rejected', 'expired', 'cancelled', 'closed_unknown'
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
                                        f"Cannot inject fault while incident {row[0]} "
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

        # Forward fault injection to sandbox operator endpoint
        inject_url = f"{cfg.il_sandbox_base_url}/operator/inject"
        headers = {"Authorization": f"Bearer {cfg.il_sandbox_admin_token}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as ac:
                resp = await ac.post(
                    inject_url,
                    headers=headers,
                    json={"family": payload.family, "variant_seed": payload.variant_seed},
                )
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail={
                            "error": {
                                "code": "sandbox_fault_injection_failed",
                                "message": (
                                    f"Sandbox inject returned status "
                                    f"{resp.status_code}: {resp.text}"
                                ),
                                "request_id": None,
                                "retryable": False,
                            }
                        },
                    )
                data = resp.json()
                return DemoFaultResponse(
                    status=data.get("status", "injected"),
                    family=data.get("family", payload.family),
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
