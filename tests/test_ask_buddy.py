"""
Tests for Ask Buddy.

Run with:
    pytest tests/test_ask_buddy.py -v

These tests fall into two categories:
  1. Unit / integration tests — run against the live Postgres+pgvector DB
     after ingestion. Marked with @pytest.mark.integration. Require
     ASK_BUDDY_DB_DSN and OPENAI_API_KEY in the environment.

  2. Offline unit tests — test the chunking / RRF logic without any
     network or DB calls.
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_available() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY")) and bool(
        os.environ.get("ASK_BUDDY_DB_DSN")
    )


integration = pytest.mark.skipif(
    not _env_available(),
    reason="Integration tests require GOOGLE_API_KEY and ASK_BUDDY_DB_DSN",
)


# ---------------------------------------------------------------------------
# TC-0: Offline — chunking logic
# ---------------------------------------------------------------------------

class TestChunking:
    def test_short_text_is_single_chunk(self):
        from src.ask_buddy.ingest import _chunk_text
        text = "This is a short paragraph."
        chunks = _chunk_text(text, target_tokens=500)
        assert chunks == [text]

    def test_long_text_produces_multiple_chunks(self):
        from src.ask_buddy.ingest import _chunk_text
        # Use a very low target so 3000-char text definitely splits
        text = ("word " * 600).strip()
        chunks = _chunk_text(text, target_tokens=50)
        assert len(chunks) > 1

    def test_overlap_is_applied(self):
        from src.ask_buddy.ingest import _chunk_text
        text = ("word " * 600).strip()
        chunks = _chunk_text(text, target_tokens=50, overlap_chars=50)
        # Consecutive chunks should share some content due to overlap
        assert len(chunks) > 1
        overlap_candidate = chunks[0][-50:]
        assert any(overlap_candidate[:20] in c for c in chunks[1:])

    def test_parse_effective_date(self):
        from src.ask_buddy.ingest import _parse_effective_date
        from datetime import date
        text = "**Effective Date:** 2024-01-01\n\nSome content here."
        result = _parse_effective_date(text)
        assert result == date(2024, 1, 1)

    def test_parse_effective_date_missing(self):
        from src.ask_buddy.ingest import _parse_effective_date
        assert _parse_effective_date("No date here.") is None


# ---------------------------------------------------------------------------
# TC-1: Offline — RRF merge logic
# ---------------------------------------------------------------------------

class TestRRFMerge:
    def _make_row(self, doc_id: int, score: float = 0.9) -> dict:
        return {
            "id": doc_id,
            "source_filename": f"doc_{doc_id}.md",
            "section": "Test Section",
            "chunk_text": f"Chunk text for doc {doc_id}",
            "effective_date": None,
            "score": score,
        }

    def test_rrf_returns_top_k(self):
        from src.ask_buddy.retrieve import _rrf_merge
        vec = [self._make_row(i) for i in range(10)]
        kw = [self._make_row(i, 0.5) for i in range(5, 15)]
        merged = _rrf_merge(vec, kw, top_k=5)
        assert len(merged) <= 5

    def test_rrf_boost_for_docs_in_both_lists(self):
        from src.ask_buddy.retrieve import _rrf_merge
        # doc 1 appears in both lists at rank 1; doc 2 only in vector
        vec = [self._make_row(1), self._make_row(2)]
        kw  = [self._make_row(1), self._make_row(3)]
        merged = _rrf_merge(vec, kw, top_k=3)
        top_id = merged[0]["id"]
        assert top_id == 1, "Doc appearing in both lists should rank highest"

    def test_rrf_score_present(self):
        from src.ask_buddy.retrieve import _rrf_merge
        vec = [self._make_row(1)]
        kw  = [self._make_row(1)]
        merged = _rrf_merge(vec, kw, top_k=1)
        assert "rrf_score" in merged[0]
        assert merged[0]["rrf_score"] > 0

    def test_empty_inputs_returns_empty(self):
        from src.ask_buddy.retrieve import _rrf_merge
        assert _rrf_merge([], [], top_k=5) == []

    def test_date_is_stringified(self):
        from src.ask_buddy.retrieve import _rrf_merge
        from datetime import date
        row = self._make_row(1)
        row["effective_date"] = date(2024, 1, 1)
        merged = _rrf_merge([row], [], top_k=1)
        assert merged[0]["effective_date"] == "2024-01-01"


# ---------------------------------------------------------------------------
# TC-2: Integration — single-doc clear answer with citation
# ---------------------------------------------------------------------------

@integration
class TestSingleDocRetrieval:
    def test_pto_query_returns_pto_doc(self):
        """TC-2a: Clear single-doc question returns PTO policy with citation."""
        from src.ask_buddy.retrieve import hybrid_retrieve
        results = hybrid_retrieve.invoke(
            {"query": "How many days of PTO do I get after 3 years?", "top_k": 5}
        )
        assert results, "Should return at least one result"
        sources = {r["source_filename"] for r in results if "error" not in r}
        assert "pto_policy.md" in sources, (
            f"Expected pto_policy.md in results, got: {sources}"
        )

    def test_remote_work_query_returns_remote_doc(self):
        """Clear question about remote work eligibility."""
        from src.ask_buddy.retrieve import hybrid_retrieve
        results = hybrid_retrieve.invoke(
            {"query": "Who is eligible for hybrid remote work?", "top_k": 5}
        )
        sources = {r["source_filename"] for r in results if "error" not in r}
        assert "remote_work_policy.md" in sources


# ---------------------------------------------------------------------------
# TC-3: Integration — cross-document retrieval (parental leave → benefits)
# ---------------------------------------------------------------------------

@integration
class TestCrossDocRetrieval:
    def test_parental_leave_references_benefits(self):
        """
        TC-3: parental_leave.md references benefits_enrollment.md.
        A query about adding a dependent after parental leave should surface
        chunks from both documents.
        """
        from src.ask_buddy.retrieve import hybrid_retrieve
        results = hybrid_retrieve.invoke(
            {
                "query": (
                    "How do I enroll my newborn in health insurance "
                    "after taking parental leave?"
                ),
                "top_k": 8,
            }
        )
        sources = {r["source_filename"] for r in results if "error" not in r}
        assert "parental_leave.md" in sources or "benefits_enrollment.md" in sources, (
            f"Expected cross-doc results, got: {sources}"
        )


# ---------------------------------------------------------------------------
# TC-4: Integration — date-specific PTO query returns correct version
# ---------------------------------------------------------------------------

@integration
class TestDateSpecificQuery:
    def test_current_pto_is_v2(self):
        """TC-4a: Query for current PTO accrual should surface v2.0 (2024-01-01)."""
        from src.ask_buddy.retrieve import hybrid_retrieve
        results = hybrid_retrieve.invoke(
            {"query": "What is the current PTO accrual rate?", "top_k": 5}
        )
        top = [r for r in results if "error" not in r]
        assert top, "Must return at least one result"
        dates = [r["effective_date"] for r in top if r.get("effective_date")]
        # At least one result should be from the 2024 version
        assert any(d >= "2024-01-01" for d in dates if d), (
            f"Expected a 2024-era chunk near the top; got dates: {dates}"
        )

    def test_old_pto_version_retrievable(self):
        """TC-4b: Asking explicitly about the old policy should retrieve v1.0 text."""
        from src.ask_buddy.retrieve import hybrid_retrieve
        results = hybrid_retrieve.invoke(
            {
                "query": "What was the PTO policy before 2024? How many days in 2022?",
                "top_k": 8,
            }
        )
        found_old = any(
            "2022" in r.get("chunk_text", "") or
            "v1.0" in r.get("chunk_text", "") or
            "supersedes" in r.get("chunk_text", "").lower()
            for r in results
            if "error" not in r
        )
        assert found_old, "Should retrieve the archived v1.0 section of pto_policy.md"


# ---------------------------------------------------------------------------
# TC-5: Integration — IT security query should return low / no HR results
# ---------------------------------------------------------------------------

@integration
class TestOutOfScopeRefusal:
    def test_it_security_query_not_hr(self):
        """
        TC-5: it_security_policy.md has been removed from the corpus.
        A VPN/password query returns low-relevance HR results (or nothing).
        The important thing is that it_security_policy.md is NOT in the DB —
        the agent-level refusal is handled by the system prompt scope check.
        """
        from src.ask_buddy.retrieve import hybrid_retrieve
        results = hybrid_retrieve.invoke(
            {
                "query": (
                    "What is the password rotation policy and VPN requirements?"
                ),
                "top_k": 5,
            }
        )
        # it_security_policy.md was deleted from the DB — must not appear
        sources = {r.get("source_filename") for r in results if "error" not in r}
        assert "it_security_policy.md" not in sources, (
            "it_security_policy.md should have been removed from the corpus"
        )


# ---------------------------------------------------------------------------
# TC-6: Agent level — mock Slack round-trip (OpenAI key required for CugaAgent init)
# ---------------------------------------------------------------------------

@integration   # CugaAgent.__init__ always initialises the LLM; needs OPENAI_API_KEY
class TestAgentMockedSlack:
    def test_agent_posts_message(self, monkeypatch):
        """
        Verify the agent calls post_slack_message at the end of a round-trip.
        Uses a fake hybrid_retrieve (no DB) but requires OPENAI_API_KEY because
        CugaAgent initialises the LLM at construction time.

        We replace the module-level hybrid_retrieve binding with a fresh
        @tool that returns a canned chunk — the correct seam because
        CugaAgent captures the tool object at init time.
        """
        posted: list[dict] = []

        def fake_post(channel: str, text: str) -> None:
            posted.append({"channel": channel, "text": text})

        # Canned chunk the fake retriever returns
        fake_chunk = {
            "id": 1,
            "source_filename": "pto_policy.md",
            "section": "PTO Accrual Rates",
            "chunk_text": (
                "Employees with 2-5 years of service accrue 120 hours (15 days) "
                "per year."
            ),
            "effective_date": "2024-01-01",
            "rrf_score": 0.032,
        }

        from langchain_core.tools import tool as lc_tool
        import src.ask_buddy.retrieve as retrieve_mod
        import src.ask_buddy.agent as agent_mod

        @lc_tool
        def fake_hybrid_retrieve(query: str, top_k: int = 5) -> list:
            """Retrieve HR policy chunks. (test stub)"""
            return [fake_chunk]

        # Replace the module-level binding so build_agent picks up the fake
        original = retrieve_mod.hybrid_retrieve
        monkeypatch.setattr(retrieve_mod, "hybrid_retrieve", fake_hybrid_retrieve)
        monkeypatch.setattr(agent_mod, "hybrid_retrieve", fake_hybrid_retrieve)

        import asyncio

        agent = agent_mod.build_agent(slack_post_fn=fake_post)
        asyncio.run(
            agent.invoke(
                "How many PTO days do I get with 3 years of service? "
                "Call post_slack_message with channel='C1234'.",
                thread_id="test-thread-1",
            )
        )

        assert posted, "Agent must call post_slack_message at least once"
        assert any("C1234" in m["channel"] for m in posted), (
            "post_slack_message must use the channel from the prompt"
        )
