"""Independent functional verification of checkout recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb

from incidentlab.config import Settings
from incidentlab.db import get_control_connection
from incidentlab.events import record_event
from incidentlab.executor import (
    CheckoutError,
    submit_checkout,
    wait_for_order_fulfillment,
)
from incidentlab.schemas import VerificationResult


async def verify_checkout(
    incident_id: UUID,
    request_key: UUID,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> VerificationResult:
    """Verify that a fresh checkout request reaches 'fulfilled' status.

    Only an actual verified pass plus applied receipt sets state to 'verified_recovered'.
    """
    if settings is None:
        settings = Settings.from_env()

    evidence_id = f"ev:{incident_id}:verif_{uuid4().hex[:6]}"
    started_at = datetime.now(UTC)

    order_id: UUID | None = None
    observed_status: str = "unknown"
    verdict: str = "fail"
    request_id = uuid4()

    try:
        accepted = await submit_checkout(request_key, settings=settings, client=client)
        request_id = accepted.request_id
        order_id = accepted.order_id

        # Poll order for fulfillment (up to 10 seconds, polling every 250ms)
        order = await wait_for_order_fulfillment(
            order_id,
            settings=settings,
            timeout_seconds=10.0,
            poll_interval=0.25,
            client=client,
        )
        if order.status == "fulfilled":
            verdict = "pass"
            observed_status = "fulfilled"
        else:
            verdict = "fail"
            observed_status = order.status
    except CheckoutError as chk_err:
        verdict = "fail"
        observed_status = "unavailable"
        if chk_err.request_id:
            try:
                request_id = UUID(chk_err.request_id)
            except Exception:
                pass
    except TimeoutError:
        verdict = "fail"
        observed_status = "pending"
    except Exception:
        verdict = "error"
        observed_status = "unknown"

    finished_at = datetime.now(UTC)

    result = VerificationResult(
        verdict=verdict,
        request_id=request_id,
        order_id=order_id,
        observed_status=observed_status,
        started_at=started_at,
        finished_at=finished_at,
        evidence_id=evidence_id,
    )

    # Persist verification evidence and update incident state under control lock
    with get_control_connection(settings) as conn:
        with conn.cursor() as cur:
            # Insert evidence into control.evidence
            cur.execute(
                """
                INSERT INTO control.evidence (
                    incident_id, evidence_id, kind, source,
                    observed_at, data
                )
                VALUES (%s, %s, 'verification', 'verify_checkout', %s, %s)
                ON CONFLICT (incident_id, evidence_id) DO NOTHING
                """,
                (incident_id, evidence_id, finished_at, Jsonb(result.model_dump(mode="json"))),
            )

            # Record event
            record_event(
                conn,
                incident_id,
                "verification.finished",
                result.model_dump(mode="json"),
            )

            # Update incident state
            if verdict == "pass":
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'verified_recovered', updated_at = NOW(),
                        outcome = %s
                    WHERE id = %s
                    """,
                    (
                        Jsonb(
                            {
                                "status": "verified_recovered",
                                "verification": result.model_dump(mode="json"),
                            }
                        ),
                        incident_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE control.incidents
                    SET state = 'verification_failed', updated_at = NOW(),
                        outcome = %s
                    WHERE id = %s
                    """,
                    (
                        Jsonb(
                            {
                                "status": "verification_failed",
                                "verification": result.model_dump(mode="json"),
                            }
                        ),
                        incident_id,
                    ),
                )
            conn.commit()

    return result
