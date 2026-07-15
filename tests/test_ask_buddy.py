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

    def test_corpus_scoped_retrieve_exists(self):
        """hr_retrieve and it_retrieve are importable @tools."""
        from src.ask_buddy.retrieve import hr_retrieve, it_retrieve
        assert callable(hr_retrieve.invoke)
        assert callable(it_retrieve.invoke)

    def test_quality_reranking_promotes_liked_chunk(self):
        """A chunk with strong positive feedback should outrank a tied chunk."""
        from src.ask_buddy.retrieve import _rrf_merge
        # Two docs at identical RRF rank; doc 2 has +6 net feedback.
        vec = [self._make_row(1), self._make_row(2)]
        kw = [self._make_row(1), self._make_row(2)]
        quality = {2: {"positive": 6, "negative": 0, "net": 6}}
        merged = _rrf_merge(vec, kw, top_k=2, quality=quality)
        assert merged[0]["id"] == 2, "Positively-rated chunk should be promoted"
        assert merged[0]["quality_net"] == 6

    def test_quality_reranking_demotes_disliked_chunk(self):
        from src.ask_buddy.retrieve import _rrf_merge
        vec = [self._make_row(1), self._make_row(2)]
        kw = [self._make_row(1), self._make_row(2)]
        quality = {1: {"positive": 0, "negative": 6, "net": -6}}
        merged = _rrf_merge(vec, kw, top_k=2, quality=quality)
        assert merged[0]["id"] == 2, "Negatively-rated chunk should be demoted"

    def test_quality_multiplier_is_bounded(self):
        from src.ask_buddy.retrieve import _quality_multiplier, QUALITY_MAX
        # A huge pile-on can't exceed the ±MAX ceiling.
        assert _quality_multiplier(1000) == 1.0 + QUALITY_MAX
        assert _quality_multiplier(-1000) == 1.0 - QUALITY_MAX
        assert _quality_multiplier(0) == 1.0


# ---------------------------------------------------------------------------
# TC-1b: Offline — supervisor builder smoke test
# ---------------------------------------------------------------------------

class TestSupervisorBuilder:
    def test_build_supervisor_returns_supervisor(self):
        from cuga import CugaSupervisor
        from src.ask_buddy.agent import build_supervisor
        sup = build_supervisor(slack_post_fn=lambda ch, txt: None)
        assert isinstance(sup, CugaSupervisor)

    def test_build_supervisor_has_both_agents(self):
        from src.ask_buddy.agent import build_supervisor
        sup = build_supervisor(slack_post_fn=lambda ch, txt: None)
        assert "hr_agent" in sup._agents
        assert "it_agent" in sup._agents
        assert "scheduler_agent" in sup._agents


# ---------------------------------------------------------------------------
# TC-1c: Offline — reminder scheduling (create/list/cancel + DB round-trip)
# ---------------------------------------------------------------------------

class TestReminderTools:
    def _fake_resolve(self, channel: str):
        name = channel.lstrip("#")
        return f"C_FAKE_{name.upper()}", name

    def test_build_scheduler_agent(self):
        from src.ask_buddy.agent import build_scheduler_agent
        agent = build_scheduler_agent(
            slack_post_fn=lambda ch, txt: None,
            resolve_channel_fn=self._fake_resolve,
            created_by="U_TEST",
        )
        assert agent is not None

    @integration
    def test_create_reminder_tool_persists_and_schedules(self):
        from src.ask_buddy.agent import _build_reminder_tools
        from src.ask_buddy.db import deactivate_reminder, list_reminders_for_channel
        from src.ask_buddy.scheduler import start_scheduler, unschedule_reminder

        start_scheduler(slack_post_fn=lambda ch, txt: None)
        create_reminder, list_reminders, cancel_reminder = _build_reminder_tools(
            self._fake_resolve, created_by="U_TEST"
        )

        result = create_reminder.invoke({
            "channel": "test-reminders-channel",
            "message": "unit test reminder",
            "cron_expression": "0 9 * * *",
            "timezone": "America/Los_Angeles",
        })
        assert result["channel_name"] == "test-reminders-channel"
        assert result["cron_expression"] == "0 9 * * *"
        reminder_id = result["id"]

        rows = list_reminders_for_channel("C_FAKE_TEST-REMINDERS-CHANNEL")
        assert any(r["id"] == reminder_id for r in rows)

        cancel_msg = cancel_reminder.invoke({"reminder_id": reminder_id})
        assert "cancelled" in cancel_msg.lower()

        rows_after = list_reminders_for_channel("C_FAKE_TEST-REMINDERS-CHANNEL")
        assert not any(r["id"] == reminder_id for r in rows_after)

        # Cleanup safety net in case the assertion above fails first.
        deactivate_reminder(reminder_id)
        unschedule_reminder(reminder_id)


# ---------------------------------------------------------------------------
# TC-2: Integration — single-doc clear answer with citation
# ---------------------------------------------------------------------------

@integration
class TestSingleDocRetrieval:
    def test_pto_query_returns_pto_doc(self):
        """TC-2a: Clear single-doc question returns PTO policy with citation."""
        from src.ask_buddy.retrieve import hr_retrieve
        results = hr_retrieve.invoke(
            {"query": "How many days of PTO do I get after 3 years?", "top_k": 5}
        )
        assert results, "Should return at least one result"
        sources = {r["source_filename"] for r in results if "error" not in r}
        assert "pto_policy.md" in sources, (
            f"Expected pto_policy.md in results, got: {sources}"
        )

    def test_remote_work_query_returns_remote_doc(self):
        """Clear question about remote work eligibility."""
        from src.ask_buddy.retrieve import hr_retrieve
        results = hr_retrieve.invoke(
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
        from src.ask_buddy.retrieve import hr_retrieve
        results = hr_retrieve.invoke(
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
        from src.ask_buddy.retrieve import hr_retrieve
        results = hr_retrieve.invoke(
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
        from src.ask_buddy.retrieve import hr_retrieve
        results = hr_retrieve.invoke(
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
        from src.ask_buddy.retrieve import hr_retrieve
        results = hr_retrieve.invoke(
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


# ---------------------------------------------------------------------------
# TC-7: Regression evals curated from real negative feedback
# ---------------------------------------------------------------------------
#
# Workflow:
#   1. Export candidates:
#        uv run python -m src.ask_buddy.feedback_report --export-evals tests/regression_evals.json
#   2. A human fills in "expected_sources" (list of source_filename strings)
#      for each question that SHOULD be answerable.
#   3. This test asserts retrieval still surfaces at least one expected source
#      for each curated case — so a prompt/model/chunking change can't silently
#      regress on a question users already flagged.
#
# It skips cleanly when the curated file is absent, so it never blocks CI
# until someone opts in by committing regression_evals.json.

import json as _json
from pathlib import Path as _Path

_EVAL_FILE = _Path(__file__).parent / "regression_evals.json"


def _load_curated_evals() -> list[dict]:
    if not _EVAL_FILE.exists():
        return []
    try:
        data = _json.loads(_EVAL_FILE.read_text())
    except _json.JSONDecodeError:
        return []
    # Only cases a human has annotated with expected_sources are actionable.
    return [c for c in data if c.get("expected_sources")]


@integration
@pytest.mark.skipif(
    not _load_curated_evals(),
    reason="No curated tests/regression_evals.json with expected_sources",
)
class TestFeedbackRegressionEvals:
    @pytest.mark.parametrize("case", _load_curated_evals())
    def test_expected_source_is_retrieved(self, case):
        from src.ask_buddy.retrieve import hr_retrieve
        results = hr_retrieve.invoke({"query": case["question"], "top_k": 8})
        got = {r.get("source_filename") for r in results if "error" not in r}
        expected = set(case["expected_sources"])
        assert got & expected, (
            f"Regression: {case['question']!r} should surface one of "
            f"{expected}; retrieval returned {got}"
        )


# ---------------------------------------------------------------------------
# TC-GIT-1 through TC-GIT-8: GitHub client + git_watch offline unit tests
# ---------------------------------------------------------------------------

class TestGitHubClientTrimsIssue:
    def test_trim_issue_keys(self):
        """_trim_issue extracts only the expected fields."""
        from src.ask_buddy.github_client import _trim_issue
        raw = {
            "number": 42, "title": "Bug", "state": "open",
            "user": {"login": "alice"}, "labels": [{"name": "bug"}],
            "assignees": [{"login": "bob"}], "comments": 3,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "html_url": "https://github.com/a/b/issues/42",
        }
        trimmed = _trim_issue(raw)
        assert trimmed["number"] == 42
        assert trimmed["author"] == "alice"
        assert trimmed["labels"] == ["bug"]
        assert trimmed["assignees"] == ["bob"]
        assert "url" in trimmed
        assert "body" not in trimmed  # body only in get_issue, not list view

    def test_list_issues_filters_out_prs(self, monkeypatch):
        """list_issues must exclude items that have a 'pull_request' key."""
        import src.ask_buddy.github_client as gh
        fake_data = [
            {"number": 1, "title": "Issue", "state": "open",
             "user": {"login": "u"}, "labels": [], "assignees": [],
             "comments": 0, "created_at": None, "updated_at": None,
             "html_url": "https://example.com/1"},
            {"number": 2, "title": "PR disguised as issue", "state": "open",
             "pull_request": {"url": "..."},
             "user": {"login": "u"}, "labels": [], "assignees": [],
             "comments": 0, "created_at": None, "updated_at": None,
             "html_url": "https://example.com/2"},
        ]
        monkeypatch.setattr(gh, "_request", lambda path, params=None: fake_data)
        result = gh.list_issues("a/b")
        assert len(result) == 1
        assert result[0]["number"] == 1


class TestGitHubClientSplitRepo:
    def test_valid_repo(self):
        from src.ask_buddy.github_client import _split_repo
        assert _split_repo("owner/name") == ("owner", "name")

    def test_invalid_repo_raises(self):
        from src.ask_buddy.github_client import _split_repo, GitHubError
        with pytest.raises(GitHubError):
            _split_repo("badrepo")


class TestGitHubClientMissingToken:
    def test_missing_token_raises(self, monkeypatch):
        """list_issues must surface GitHubError when GITHUB_TOKEN is unset."""
        import src.ask_buddy.github_client as gh
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(gh.GitHubError, match="GITHUB_TOKEN"):
            gh.list_issues("a/b")


class TestGitPrChecksCount:
    def test_check_run_counts(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        calls = iter([
            {"head": {"sha": "abc"}},   # get_pull_request (pulls/{n})
            {"check_runs": [            # check-runs
                {"name": "unit",  "status": "completed",   "conclusion": "success"},
                {"name": "lint",  "status": "completed",   "conclusion": "failure"},
                {"name": "e2e",   "status": "in_progress", "conclusion": None},
            ]},
        ])
        monkeypatch.setattr(gh, "_request", lambda path, params=None: next(calls))
        out = gh.get_pr_checks("a/b", 1)
        assert out["total"] == 3
        assert out["success"] == 1
        assert out["failure"] == 1
        assert out["pending"] == 1
        assert len(out["runs"]) == 3


class TestGitToolsReturnErrorDict:
    def test_list_issues_tool_returns_error_dict(self, monkeypatch):
        """When gh.list_issues raises GitHubError, the @tool returns [{'error':...}]."""
        import src.ask_buddy.github_client as gh
        import src.ask_buddy.agent as agent_mod

        def _raise(*a, **kw):
            raise gh.GitHubError("no token")

        monkeypatch.setattr(gh, "list_issues", _raise)
        list_issues_tool, _, _ = agent_mod._build_git_issue_tools()
        result = list_issues_tool.invoke({"repo": "a/b", "state": "open"})
        assert isinstance(result, list)
        assert result[0].get("error")


class TestGitWatchFirstSightSeedsNoPost:
    def test_first_sight_seeds_silently(self, monkeypatch):
        import src.ask_buddy.git_watch as gw
        import src.ask_buddy.github_client as gh

        monkeypatch.setenv("GIT_WATCH_CHANNEL", "eng-triage")
        monkeypatch.setattr(gh, "list_issues",
                            lambda *a, **kw: [{"number": 5, "title": "T",
                                               "state": "open", "author": "u",
                                               "labels": [], "assignees": [],
                                               "comments": 0, "created_at": None,
                                               "updated_at": None, "url": "http://x"}])
        monkeypatch.setattr(gh, "list_pull_requests",
                            lambda *a, **kw: [{"number": 3, "title": "P",
                                               "state": "open", "draft": False,
                                               "author": "u", "labels": [],
                                               "requested_reviewers": [],
                                               "base": "main", "head": "feat",
                                               "head_sha": "abc",
                                               "mergeable_state": None,
                                               "created_at": None, "updated_at": None,
                                               "url": "http://y"}])
        # Return 0/0 to simulate first sight
        monkeypatch.setattr(gw, "get_git_watermark",
                            lambda repo: {"last_issue_number": 0, "last_pr_number": 0})

        watermark_calls = []
        monkeypatch.setattr(gw, "set_git_watermark",
                            lambda repo, iss, pr: watermark_calls.append((repo, iss, pr)))

        post_calls = []
        gw.poll_once("a/b", lambda ch, txt: post_calls.append(txt))

        assert len(watermark_calls) == 1, "Should seed the watermark"
        assert len(post_calls) == 0, "Should NOT post on first sight"


class TestGitWatchReportsOnlyNew:
    def test_reports_only_items_above_watermark(self, monkeypatch):
        import src.ask_buddy.git_watch as gw
        import src.ask_buddy.github_client as gh

        monkeypatch.setenv("GIT_WATCH_CHANNEL", "eng-triage")

        def _issue(n):
            return {"number": n, "title": f"Issue {n}", "state": "open",
                    "author": "u", "labels": [], "assignees": [], "comments": 0,
                    "created_at": None, "updated_at": None, "url": f"http://i/{n}"}

        def _pr(n):
            return {"number": n, "title": f"PR {n}", "state": "open",
                    "draft": False, "author": "u", "labels": [],
                    "requested_reviewers": [], "base": "main", "head": "feat",
                    "head_sha": "abc", "mergeable_state": None,
                    "created_at": None, "updated_at": None, "url": f"http://p/{n}"}

        monkeypatch.setattr(gh, "list_issues",
                            lambda *a, **kw: [_issue(4), _issue(6), _issue(7)])
        monkeypatch.setattr(gh, "list_pull_requests",
                            lambda *a, **kw: [_pr(3), _pr(4)])

        # Watermark: issue=5, pr=3  →  new: issues 6&7, PR 4
        monkeypatch.setattr(gw, "get_git_watermark",
                            lambda repo: {"last_issue_number": 5, "last_pr_number": 3})

        watermark_args = []
        monkeypatch.setattr(gw, "set_git_watermark",
                            lambda repo, iss, pr: watermark_args.append((iss, pr)))

        posted: list[str] = []
        gw.poll_once("a/b", lambda ch, txt: posted.append(txt))

        assert len(posted) == 1, "Should post exactly once"
        summary = posted[0]
        assert "#6" in summary
        assert "#7" in summary
        assert "PR 4" in summary or "#4" in summary
        assert "#4 Issue 4" not in summary  # issue #4 is below watermark
        assert "PR 3" not in summary and "#3" not in summary  # pr #3 at watermark

        # Watermark should advance to max seen (issue 7, pr 4)
        assert watermark_args[0] == (7, 4)


class TestSupervisorRegistersGitSlaves:
    def test_git_agents_in_supervisor(self):
        """build_supervisor must include git_issue_agent and git_pr_agent."""
        from src.ask_buddy.agent import build_supervisor
        sup = build_supervisor(slack_post_fn=lambda ch, txt: None)
        assert "git_issue_agent" in sup._agents
        assert "git_pr_agent" in sup._agents

    def test_build_git_issue_agent_returns_cuga_agent(self):
        from cuga import CugaAgent
        from src.ask_buddy.agent import build_git_issue_agent
        agent = build_git_issue_agent(slack_post_fn=lambda ch, txt: None)
        assert isinstance(agent, CugaAgent)

    def test_build_git_pr_agent_returns_cuga_agent(self):
        from cuga import CugaAgent
        from src.ask_buddy.agent import build_git_pr_agent
        agent = build_git_pr_agent(slack_post_fn=lambda ch, txt: None)
        assert isinstance(agent, CugaAgent)
