"""Ordered event timeline management and sensitive data redaction."""

from __future__ import annotations

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
    """Append one ordered event to the incident timeline under row lock."""
    # Ensure incident row is locked for atomic monotonic sequence allocation
    cursor.execute(
        "SELECT id FROM control.incidents WHERE id = %s FOR UPDATE",
        (str(incident_id),),
    )

    cursor.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM control.events WHERE incident_id = %s",
        (str(incident_id),),
    )
    next_seq = cursor.fetchone()[0]

    safe_payload = redact_sensitive_data(payload)
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
