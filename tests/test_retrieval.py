"""Tests for PostgreSQL full-text keyword runbook retrieval."""

from __future__ import annotations

import pytest

from incidentlab.db import get_control_connection


@pytest.mark.integration
def test_index_and_retrieve_runbooks(live_settings):
    """Verify runbook indexing and keyword retrieval return relevant ranked sections."""
    from incidentlab.retrieval import index_runbooks, retrieve_runbooks

    with get_control_connection(live_settings) as conn:
        # Index runbooks from data/runbooks
        indexed_count = index_runbooks(conn)
        assert indexed_count >= 10  # 3 runbooks * 5 sections = 15 sections

        # Test query for inventory fault
        results = retrieve_runbooks(conn, query="inventory unavailable endpoint legacy", limit=3)
        assert len(results) >= 1
        assert len(results) <= 3
        # Top result should cite inventory runbook
        assert results[0].source == "inventory"
        assert results[0].source_version == "1"
        assert results[0].section_id in ("symptoms", "diagnostics", "repair")
        assert results[0].kind == "runbook"
        assert "evidence_id" in results[0].model_dump()

        # Test query for worker fault
        worker_results = retrieve_runbooks(
            conn, query="worker pending orders not fulfilled", limit=3
        )
        assert len(worker_results) >= 1
        assert worker_results[0].source == "worker"
        assert worker_results[0].source_version == "1"

        # Test query for maintenance fault
        upstream_results = retrieve_runbooks(
            conn, query="upstream maintenance scheduled downtime", limit=3
        )
        assert len(upstream_results) >= 1
        assert upstream_results[0].source == "upstream"
        assert upstream_results[0].source_version == "1"


def test_chunk_token_bounds():
    """Verify all runbook sections in data/runbooks are bounded to at most 200 tokens."""
    from pathlib import Path

    from incidentlab.model import count_request_tokens

    runbook_dir = Path(__file__).resolve().parent.parent / "data" / "runbooks"
    for md_file in runbook_dir.glob("*.md"):
        content = md_file.read_text(encoding="utf-8")
        sections = content.split("## ")
        for sec in sections[1:]:
            lines = sec.split("\n")
            sec_name = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            tok_count = count_request_tokens([{"role": "user", "content": body}])
            assert tok_count <= 200, (
                f"Section {sec_name} in {md_file.name} has {tok_count} tokens (> 200)"
            )
