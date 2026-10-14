"""Ordered event timeline management and sensitive data redaction."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from incidentlab.schemas import EventItem, EventPage

SENSITIVE_KEY_SUBSTRINGS = (
    "token",
    "password",
    "secret",
    "auth",
    "credential",
    "private_key",
    "secret_key",
)


def redact_sensitive_data(val: Any) -> Any:
    """Recursively redact sensitive credentials and tokens before storage or serialization."""
    if isinstance(val, dict):
        redacted = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in SENSITIVE_KEY_SUBSTRINGS):
                # Don't redact non-sensitive keys that happen to contain substring if safe
                if k_lower in ("event_kind", "key_name"):
                    redacted[k] = redact_sensitive_data(v)
                else:
                    redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_sensitive_data(v)
        return redacted
    if isinstance(val, list):
        return [redact_sensitive_data(item) for item in val]
    return val


def append_event(
    cursor: Any,
    incident_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    duration_ms: int | None = None,
    token_count: int | None = None,
) -> int:
    """Append one ordered event to the incident timeline under row lock with SHA-256 hash chain."""
    # Ensure incident row is locked for atomic monotonic sequence allocation
    cursor.execute(
        "SELECT id FROM control.incidents WHERE id = %s FOR UPDATE",
        (str(incident_id),),
    )

    # Retrieve latest event payload to establish cryptographic prev_hash chain
    cursor.execute(
        """
        SELECT payload FROM control.events
        WHERE incident_id = %s
        ORDER BY seq DESC
        LIMIT 1
        """,
        (str(incident_id),),
    )
    prev_row = cursor.fetchone()
    if prev_row is None:
        prev_hash = "0" * 64
    else:
        prev_payload = prev_row[0]
        if isinstance(prev_payload, str):
            prev_payload = json.loads(prev_payload)
        if isinstance(prev_payload, dict) and "event_hash" in prev_payload:
            prev_hash = prev_payload["event_hash"]
        else:
            prev_hash = hashlib.sha256(
                json.dumps(prev_payload, sort_keys=True).encode("utf-8")
            ).hexdigest()

    cursor.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM control.events WHERE incident_id = %s",
        (str(incident_id),),
    )
    next_seq = cursor.fetchone()[0]

    safe_payload = redact_sensitive_data(payload)
    if not isinstance(safe_payload, dict):
        safe_payload = {"data": safe_payload}

    safe_payload["prev_hash"] = prev_hash
    event_hash = hashlib.sha256(
        json.dumps(safe_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    safe_payload["event_hash"] = event_hash

    now = datetime.now(UTC)

    cursor.execute(
        """
        INSERT INTO control.events (
            incident_id, seq, event_type, timestamp, payload, duration_ms, token_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(incident_id),
            next_seq,
            event_type,
            now,
            json.dumps(safe_payload),
            duration_ms,
            token_count,
        ),
    )
    return next_seq


def record_event(
    conn: Any,
    incident_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    duration_ms: int | None = None,
    token_count: int | None = None,
) -> int:
    """Convenience helper to record an event and commit transaction."""
    with conn.cursor() as cur:
        seq = append_event(
            cur,
            incident_id,
            event_type,
            payload,
            duration_ms=duration_ms,
            token_count=token_count,
        )
    conn.commit()
    return seq


def read_events(
    cursor: Any,
    incident_id: UUID,
    after_seq: int = 0,
    limit: int = 100,
) -> EventPage:
    """Read a page of ordered events for an incident starting after after_seq."""
    bounded_limit = min(max(1, limit), 100)
    cursor.execute(
        """
        SELECT seq, event_type, timestamp, payload, duration_ms, token_count
        FROM control.events
        WHERE incident_id = %s AND seq > %s
        ORDER BY seq ASC
        LIMIT %s
        """,
        (str(incident_id), after_seq, bounded_limit + 1),
    )
    rows = cursor.fetchall()
    has_more = len(rows) > bounded_limit
    items_to_return = rows[:bounded_limit]

    events = []
    max_seq = after_seq
    for r in items_to_return:
        seq, event_type, ts, payload, dur, tokens = r
        if isinstance(payload, str):
            payload = json.loads(payload)
        events.append(
            EventItem(
                seq=seq,
                event_type=event_type,
                timestamp=ts,
                payload=payload,
                duration_ms=dur,
                token_count=tokens,
            )
        )
        if seq > max_seq:
            max_seq = seq

    return EventPage(
        events=events,
        next_seq=max_seq,
        has_more=has_more,
    )
