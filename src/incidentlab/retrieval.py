"""PostgreSQL full-text keyword runbook retrieval."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from incidentlab.schemas import EvidenceItem


def index_runbooks(conn: psycopg.Connection, runbooks_dir: Path | None = None) -> int:
    """Index markdown runbook sections into control.runbook_chunks using full-text search."""
    if runbooks_dir is None:
        p = Path(__file__).resolve().parent.parent.parent / "data" / "runbooks"
        if p.exists():
            runbooks_dir = p
        else:
            runbooks_dir = Path("data/runbooks").resolve()

    count = 0
    with conn.cursor() as cur:
        for md_file in sorted(runbooks_dir.glob("*.md")):
            content = md_file.read_text(encoding="utf-8")
            runbook_id = md_file.stem

            # Extract source_version from header e.g. "(inventory v1)" or default "1"
            first_line = content.split("\n")[0]
            version_match = re.search(r"v(\d+)", first_line)
            source_version = version_match.group(1) if version_match else "1"

            sections = content.split("## ")
            for sec in sections[1:]:
                lines = sec.split("\n")
                section_id = lines[0].strip().lower()
                body = "\n".join(lines[1:]).strip()
                if not body:
                    continue

                chunk_id = f"{runbook_id}:{source_version}:{section_id}"
                content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
                vector_text = f"{runbook_id} {section_id} {body}"

                cur.execute(
                    """
                    INSERT INTO control.runbook_chunks (
                        chunk_id, runbook_id, source_version, section_id, content_hash, content, tsv
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, to_tsvector('english', %s)
                    )
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        content_hash = EXCLUDED.content_hash,
                        content = EXCLUDED.content,
                        tsv = EXCLUDED.tsv
                    """,
                    (
                        chunk_id,
                        runbook_id,
                        source_version,
                        section_id,
                        content_hash,
                        body,
                        vector_text,
                    ),
                )
                count += 1
        conn.commit()
    return count


def retrieve_runbooks(
    conn: psycopg.Connection,
    query: str,
    limit: int = 3,
) -> list[EvidenceItem]:
    """Retrieve ranked versioned runbook sections using PostgreSQL full-text rank."""
    limit = max(1, min(limit, 5))
    words = re.findall(r"\w+", query)
    if not words:
        return []

    tsquery_str = " | ".join(words)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT chunk_id, runbook_id, source_version, section_id, content,
                   ts_rank_cd(tsv, to_tsquery('english', %s)) AS rank
            FROM control.runbook_chunks
            WHERE tsv @@ to_tsquery('english', %s)
            ORDER BY rank DESC, chunk_id ASC
            LIMIT %s
            """,
            (tsquery_str, tsquery_str, limit),
        )
        rows = cur.fetchall()

    results: list[EvidenceItem] = []
    now = datetime.now(UTC)
    for r in rows:
        chunk_id, runbook_id, source_version, section_id, body, rank = r
        results.append(
            EvidenceItem(
                evidence_id=f"ev_rb_{chunk_id}",
                kind="runbook",
                observed_at=now,
                source=runbook_id,
                source_version=source_version,
                section_id=section_id,
                data={
                    "chunk_id": chunk_id,
                    "content": body,
                    "rank": float(rank),
                },
            )
        )
    return results
