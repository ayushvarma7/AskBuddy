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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_available() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY")) and bool(
        os.environ.get("ASK_BUDDY_DB_DSN")
    )


_requires_live_env = pytest.mark.skipif(
    not _env_available(),
    reason="Integration tests require GOOGLE_API_KEY and ASK_BUDDY_DB_DSN",
)


def integration(obj):
    """
    Mark a test as needing a live Postgres and real API credentials.

    Applies two things, and both are needed:

    * ``pytest.mark.integration`` so ``-m "not integration"`` actually
      deselects it. This used to be a bare skipif, which meant the documented
      offline command silently ran every DB test anyway — and passed only
      because an unset DSN made the skipif fire. Setting a placeholder DSN
      (as CI does) turned that into a wall of failures.
    * the skipif, so a plain ``pytest`` run on a machine without the
      environment still skips instead of failing.
    """
    return pytest.mark.integration(_requires_live_env(obj))


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
    @pytest.mark.parametrize("case", _load_curated_evals(),
                             ids=lambda c: c["question"][:50])
    def test_expected_source_is_retrieved(self, case):
        from src.ask_buddy.retrieve import retrieve_tool_for
        # Each case records its corpus: an IT document is unreachable through
        # hr_retrieve, so testing every case against HR would fail by design.
        corpus = case.get("corpus", "hr")
        results = retrieve_tool_for(corpus).invoke(
            {"query": case["question"], "top_k": 8})
        got = {r.get("source_filename") for r in results if "error" not in r}
        expected = set(case["expected_sources"])
        assert got & expected, (
            f"Regression: {case['question']!r} should surface one of "
            f"{expected} via {corpus}_retrieve; retrieval returned {got}"
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
        # Return -1/-1 to simulate first sight (never polled sentinel)
        monkeypatch.setattr(gw, "get_git_watermark",
                            lambda repo: {"last_issue_number": -1, "last_pr_number": -1})

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


# ---------------------------------------------------------------------------
# Git digest — offline unit tests
# ---------------------------------------------------------------------------

class TestGitDigestBuild:
    def _issue(self, number, labels=None, state="open"):
        return {"number": number, "title": f"issue {number}", "state": state,
                "author": "alice", "labels": labels or [], "assignees": [],
                "comments": 0, "created_at": None, "updated_at": None,
                "url": f"http://x/{number}"}

    def _pr(self, number, draft=False, requested_reviewers=None, state="open"):
        return {"number": number, "title": f"pr {number}", "state": state,
                "draft": draft, "author": "bob", "labels": [],
                "requested_reviewers": requested_reviewers or [],
                "base": "main", "head": f"feature-{number}", "head_sha": "abc",
                "mergeable_state": "clean", "created_at": None,
                "updated_at": None, "url": f"http://y/{number}"}

    def test_build_digest_counts_and_labels(self, monkeypatch):
        import src.ask_buddy.git_digest as gd
        import src.ask_buddy.github_client as gh

        open_issues = [self._issue(1, ["bug"]), self._issue(2, ["bug"]),
                       self._issue(3, ["docs"])]
        closed_issues = [self._issue(4, state="closed")]
        open_prs = [self._pr(10), self._pr(11, draft=True),
                    self._pr(12, requested_reviewers=["carol"])]
        closed_prs = [self._pr(13, state="closed")]

        monkeypatch.setattr(gh, "list_issues",
                            lambda repo, state="open", limit=100:
                            open_issues if state == "open" else closed_issues)
        monkeypatch.setattr(gh, "list_pull_requests",
                            lambda repo, state="open", limit=100:
                            open_prs if state == "open" else closed_prs)

        text = gd.build_digest("acme/widgets")

        assert "Open: *3*" in text and "Closed: *1*" in text
        assert "`bug` ×2" in text
        assert "Drafts (1): #11" in text
        assert "Waiting for reviewer (1): #10" in text
        assert "Open: *3*   |   Closed/Merged: *1*" in text

    def test_build_digest_no_open_prs(self, monkeypatch):
        import src.ask_buddy.git_digest as gd
        import src.ask_buddy.github_client as gh

        monkeypatch.setattr(gh, "list_issues", lambda repo, state="open", limit=100: [])
        monkeypatch.setattr(gh, "list_pull_requests", lambda repo, state="open", limit=100: [])

        text = gd.build_digest("acme/widgets")
        assert "No open PRs" in text
        assert "No labels on open issues" in text

    def test_post_digest_handles_github_error(self, monkeypatch):
        import src.ask_buddy.git_digest as gd
        import src.ask_buddy.github_client as gh

        monkeypatch.setenv("GIT_WATCH_CHANNEL", "eng-triage")

        def _raise(*a, **kw):
            raise gh.GitHubError("boom")
        monkeypatch.setattr(gd, "build_digest", _raise)

        posted = []
        gd.post_digest("acme/widgets", lambda ch, txt: posted.append((ch, txt)))
        assert posted == []   # never posts on error; also must not raise

    def test_post_digest_skips_without_channel(self, monkeypatch):
        import src.ask_buddy.git_digest as gd
        monkeypatch.delenv("GIT_WATCH_CHANNEL", raising=False)
        posted = []
        gd.post_digest("acme/widgets", lambda ch, txt: posted.append((ch, txt)))
        assert posted == []

    def test_digest_times_parsing(self, monkeypatch):
        import src.ask_buddy.git_digest as gd
        monkeypatch.setenv("GIT_DIGEST_TIMES", "9:00, 17:30")
        assert gd._digest_times() == ["9:00", "17:30"]

    def test_digest_disabled_without_token(self, monkeypatch):
        import src.ask_buddy.git_digest as gd
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GIT_WATCH_REPOS", "acme/widgets")
        monkeypatch.setenv("GIT_WATCH_CHANNEL", "eng-triage")
        assert gd.start_git_digest(lambda ch, txt: None) is None


# ---------------------------------------------------------------------------
# Phase 2 + 3 tests — PR extras and write actions
# ---------------------------------------------------------------------------

class TestGitHubClientPRExtras:
    def test_get_pr_files_trims_fields(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        monkeypatch.setattr(gh, "_request", lambda path, params=None: [
            {"filename": "a.py", "status": "modified", "additions": 5,
             "deletions": 2, "changes": 7, "patch": "@@ -1,2 +1,5 @@ ..."},
        ])
        out = gh.get_pr_files("acme/widgets", 1)
        assert out == [{"filename": "a.py", "status": "modified",
                        "additions": 5, "deletions": 2, "changes": 7}]
        assert "patch" not in out[0]

    def test_merge_status_clean_pr_no_blockers(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        monkeypatch.setattr(gh, "get_pull_request", lambda repo, n: {
            "draft": False, "mergeable_state": "clean"})
        monkeypatch.setattr(gh, "get_pr_reviews", lambda repo, n: [
            {"reviewer": "carol", "state": "APPROVED", "submitted_at": None}])
        monkeypatch.setattr(gh, "get_pr_checks", lambda repo, n: {
            "total": 2, "success": 2, "failure": 0, "pending": 0, "runs": []})

        status = gh.get_pr_merge_status("acme/widgets", 1)
        assert status["mergeable"] is True
        assert status["approved_count"] == 1
        assert status["changes_requested_count"] == 0
        assert status["checks_passed"] is True
        assert status["blocking_reasons"] == []

    def test_merge_status_blocked_by_changes_requested_and_failing_ci(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        monkeypatch.setattr(gh, "get_pull_request", lambda repo, n: {
            "draft": False, "mergeable_state": "unstable"})
        monkeypatch.setattr(gh, "get_pr_reviews", lambda repo, n: [
            {"reviewer": "dave", "state": "CHANGES_REQUESTED", "submitted_at": None}])
        monkeypatch.setattr(gh, "get_pr_checks", lambda repo, n: {
            "total": 3, "success": 1, "failure": 1, "pending": 1, "runs": []})

        status = gh.get_pr_merge_status("acme/widgets", 1)
        assert status["changes_requested_count"] == 1
        assert status["checks_passed"] is False
        assert "1 reviewer(s) requested changes" in status["blocking_reasons"]
        assert "1 CI check(s) failing" in status["blocking_reasons"]
        assert "1 CI check(s) still pending" in status["blocking_reasons"]

    def test_merge_status_latest_review_per_reviewer_wins(self, monkeypatch):
        """A reviewer who first requested changes then approved counts only as APPROVED."""
        import src.ask_buddy.github_client as gh
        monkeypatch.setattr(gh, "get_pull_request", lambda repo, n: {
            "draft": False, "mergeable_state": "clean"})
        monkeypatch.setattr(gh, "get_pr_reviews", lambda repo, n: [
            {"reviewer": "erin", "state": "CHANGES_REQUESTED", "submitted_at": "2026-01-01"},
            {"reviewer": "erin", "state": "APPROVED", "submitted_at": "2026-01-02"},
        ])
        monkeypatch.setattr(gh, "get_pr_checks", lambda repo, n: {
            "total": 0, "success": 0, "failure": 0, "pending": 0, "runs": []})

        status = gh.get_pr_merge_status("acme/widgets", 1)
        assert status["approved_count"] == 1
        assert status["changes_requested_count"] == 0


class TestGitPRToolsExtras:
    def test_pr_tools_include_files_and_merge_status(self):
        from src.ask_buddy.agent import _build_git_pr_tools
        tools = _build_git_pr_tools()
        names = {t.name for t in tools}
        assert "get_pr_files" in names
        assert "get_pr_merge_status" in names


class TestGitHubClientWriteActions:
    def test_add_issue_comment(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        monkeypatch.setattr(gh, "_request_write",
                            lambda method, path, json_body=None:
                            {"html_url": "http://x/comment", "created_at": "2026-01-01"})
        out = gh.add_issue_comment("acme/widgets", 1, "hello")
        assert out == {"url": "http://x/comment", "created_at": "2026-01-01"}

    def test_set_issue_state_rejects_bad_value(self):
        import src.ask_buddy.github_client as gh
        with pytest.raises(gh.GitHubError):
            gh.set_issue_state("acme/widgets", 1, "not-a-state")

    def test_merge_pull_request_rejects_bad_method(self):
        import src.ask_buddy.github_client as gh
        with pytest.raises(gh.GitHubError):
            gh.merge_pull_request("acme/widgets", 1, merge_method="bogus")

    def test_write_permission_error_message(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        import httpx as _httpx

        class FakeResp:
            status_code = 403
            text = "Forbidden"
            headers = {}
            def json(self): return {}

        monkeypatch.setattr(_httpx, "request", lambda *a, **kw: FakeResp())
        with pytest.raises(gh.GitHubError, match="Read-and-write"):
            gh.add_issue_comment("acme/widgets", 1, "hello")


class TestGitAgentWriteTools:
    def test_git_issue_agent_has_write_tools(self):
        from src.ask_buddy.agent import (
            _build_git_dangerous_tools, _build_git_write_tools,
        )
        write_tools = {t.name for t in _build_git_write_tools()}
        dangerous_tools = {t.name for t in _build_git_dangerous_tools()}
        assert "add_issue_comment" in write_tools
        assert "add_labels" in write_tools
        assert "assign_users" in write_tools
        assert "create_issue" in write_tools
        assert "set_issue_state" in dangerous_tools

    def test_git_pr_agent_has_merge_and_file_tools(self):
        from src.ask_buddy.agent import _build_git_pr_tools, _build_git_dangerous_tools
        pr_tools = {t.name for t in _build_git_pr_tools()}
        dangerous_tools = {t.name for t in _build_git_dangerous_tools()}
        assert "get_pr_files" in pr_tools
        assert "get_pr_merge_status" in pr_tools
        assert "merge_pull_request" in dangerous_tools
        assert "set_issue_state" in dangerous_tools


# ---------------------------------------------------------------------------
# Phase B tests — pending-approval registry and sweep
# ---------------------------------------------------------------------------

class TestPendingApprovalRegistry:
    def test_stash_and_pop_roundtrip(self):
        import src.ask_buddy.slack_listener as sl
        sl._stash_pending_approval("t1", supervisor="FAKE", channel="C1",
                                   user_text="merge PR #1", agent_config="cfg",
                                   retrieved_chunk_ids=[1, 2])
        popped = sl._pop_pending_approval("t1")
        assert popped["supervisor"] == "FAKE"
        assert popped["channel"] == "C1"
        assert popped["retrieved_chunk_ids"] == [1, 2]
        # Popped once — a second pop must return None (already consumed).
        assert sl._pop_pending_approval("t1") is None

    def test_pop_missing_thread_returns_none(self):
        import src.ask_buddy.slack_listener as sl
        assert sl._pop_pending_approval("does-not-exist") is None

    def test_stash_refuses_to_clobber_a_pending_approval(self):
        """Two concurrent messages from one user share a thread_id. Replacing
        the first stash would strand the Confirm/Cancel buttons already showing
        in Slack, so the second request must be refused instead."""
        import src.ask_buddy.slack_listener as sl
        assert sl._stash_pending_approval("dup", supervisor="FIRST", channel="C1",
                                          user_text="merge PR #1", agent_config="cfg",
                                          retrieved_chunk_ids=[]) is True
        assert sl._stash_pending_approval("dup", supervisor="SECOND", channel="C1",
                                          user_text="close issue #9", agent_config="cfg",
                                          retrieved_chunk_ids=[]) is False
        # The original is intact and still resumable.
        popped = sl._pop_pending_approval("dup")
        assert popped["supervisor"] == "FIRST"
        assert popped["user_text"] == "merge PR #1"

    def test_stash_carries_thread_ts_for_the_reply(self):
        import src.ask_buddy.slack_listener as sl
        sl._stash_pending_approval("t-thread", supervisor="F", channel="C",
                                   user_text="x", agent_config="cfg",
                                   retrieved_chunk_ids=[], thread_ts="1700000000.001")
        assert sl._pop_pending_approval("t-thread")["thread_ts"] == "1700000000.001"

    def test_stash_thread_ts_defaults_to_none_for_dms(self):
        import src.ask_buddy.slack_listener as sl
        sl._stash_pending_approval("t-dm", supervisor="F", channel="D1",
                                   user_text="x", agent_config="cfg",
                                   retrieved_chunk_ids=[])
        assert sl._pop_pending_approval("t-dm")["thread_ts"] is None

    def test_sweep_evicts_only_expired(self):
        import src.ask_buddy.slack_listener as sl
        import time
        sl._stash_pending_approval("fresh", supervisor="F", channel="C",
                                   user_text="x", agent_config="cfg",
                                   retrieved_chunk_ids=[])
        sl._stash_pending_approval("stale", supervisor="S", channel="C",
                                   user_text="x", agent_config="cfg",
                                   retrieved_chunk_ids=[])
        # Force the "stale" entry's created_at into the past.
        with sl._pending_approvals_lock:
            sl._pending_approvals["stale"]["created_at"] = time.time() - 3600
        sl._sweep_expired_approvals()
        assert sl._pop_pending_approval("fresh") is not None
        assert sl._pop_pending_approval("stale") is None

    def test_build_approval_blocks_carries_thread_id(self):
        import json
        import src.ask_buddy.slack_listener as sl
        blocks = sl._build_approval_blocks("thread-xyz", "confirm?")
        actions_block = next(b for b in blocks if b["type"] == "actions")
        for el in actions_block["elements"]:
            assert json.loads(el["value"])["thread_id"] == "thread-xyz"


# ---------------------------------------------------------------------------
# Phase D tests — GITHUB_DEFAULT_REPO prompt block
# ---------------------------------------------------------------------------

class TestDefaultRepoBlock:
    def test_empty_when_unset(self, monkeypatch):
        from src.ask_buddy.agent import _default_repo_block
        monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
        assert _default_repo_block() == ""

    def test_includes_repo_when_set(self, monkeypatch):
        from src.ask_buddy.agent import _default_repo_block
        monkeypatch.setenv("GITHUB_DEFAULT_REPO", "acme/widgets")
        block = _default_repo_block()
        assert "acme/widgets" in block


# ---------------------------------------------------------------------------
# Phase E tests — GitHub identity linking and resolution
# ---------------------------------------------------------------------------

class TestGitIdentityLinking:
    def test_resolve_my_github_login_no_user_context(self):
        from src.ask_buddy.agent import _build_git_identity_tools
        (resolve,) = _build_git_identity_tools(None)
        assert "error" in resolve.invoke({})

    def test_resolve_my_github_login_unlinked(self, monkeypatch):
        import src.ask_buddy.agent as agent_mod
        monkeypatch.setattr(agent_mod, "get_github_login", lambda uid: None)
        (resolve,) = agent_mod._build_git_identity_tools("U123")
        result = resolve.invoke({})
        assert "hasn't linked" in result

    def test_resolve_my_github_login_linked(self, monkeypatch):
        import src.ask_buddy.agent as agent_mod
        monkeypatch.setattr(agent_mod, "get_github_login", lambda uid: "octocat")
        (resolve,) = agent_mod._build_git_identity_tools("U123")
        assert resolve.invoke({}) == "octocat"


class TestGitIdentityDB:
    @integration
    def test_link_and_get_roundtrip(self):
        from src.ask_buddy.db import (
            init_git_identities_schema, link_github_identity, get_github_login,
        )
        init_git_identities_schema()
        link_github_identity("U_TEST_IDENTITY", "octocat")
        assert get_github_login("U_TEST_IDENTITY") == "octocat"
        # Re-link should upsert, not duplicate.
        link_github_identity("U_TEST_IDENTITY", "octocat2")
        assert get_github_login("U_TEST_IDENTITY") == "octocat2"


# ---------------------------------------------------------------------------
# Reliability & Correctness fixes — new tests
# ---------------------------------------------------------------------------

class TestSupervisorCache:
    def test_same_thread_id_returns_same_object(self):
        import src.ask_buddy.slack_listener as sl
        dummy_post = lambda ch, txt: None
        dummy_resolve = lambda ch: (ch, ch)
        sup1 = sl._get_or_build_supervisor(
            "test-thread-cache", dummy_post, dummy_resolve, "U1")
        sup2 = sl._get_or_build_supervisor(
            "test-thread-cache", dummy_post, dummy_resolve, "U1")
        assert sup1 is sup2, "Should return the cached supervisor"

    def test_different_thread_ids_build_separate_supervisors(self):
        import src.ask_buddy.slack_listener as sl
        dummy_post = lambda ch, txt: None
        dummy_resolve = lambda ch: (ch, ch)
        sup_a = sl._get_or_build_supervisor(
            "cache-thread-A", dummy_post, dummy_resolve, "U1")
        sup_b = sl._get_or_build_supervisor(
            "cache-thread-B", dummy_post, dummy_resolve, "U2")
        assert sup_a is not sup_b

    def test_evict_removes_only_stale_entries(self):
        import src.ask_buddy.slack_listener as sl
        import time
        with sl._supervisor_cache_lock:
            sl._supervisor_cache["stale-sup"] = {"supervisor": "S", "last_used": time.time() - 7200}
            sl._supervisor_cache["fresh-sup"] = {"supervisor": "F", "last_used": time.time()}
        sl._evict_idle_supervisors()
        with sl._supervisor_cache_lock:
            assert "stale-sup" not in sl._supervisor_cache
            assert "fresh-sup" in sl._supervisor_cache
            # cleanup
            sl._supervisor_cache.pop("fresh-sup", None)


class TestApprovalBlocksWithSummary:
    def test_action_summary_appears_in_footer(self):
        import src.ask_buddy.slack_listener as sl
        blocks = sl._build_approval_blocks("tid", "confirm?", action_summary="merge PR #5")
        text_parts = [
            el.get("text", "")
            for b in blocks
            for el in (b.get("elements") or [{"text": b.get("text", {}).get("text", "")}])
        ]
        combined = " ".join(str(p) for p in text_parts)
        assert "merge PR #5" in combined

    def test_expiry_time_appears_in_footer(self):
        import src.ask_buddy.slack_listener as sl
        blocks = sl._build_approval_blocks("tid2", "confirm?")
        context_blocks = [b for b in blocks if b["type"] == "context"]
        assert context_blocks, "Expected a context block with expiry"
        footer_text = context_blocks[0]["elements"][0]["text"]
        assert "Expires" in footer_text


class TestGitHubClientRetry:
    def test_retries_on_500_then_succeeds(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        import httpx
        call_count = [0]

        class FakeResp:
            def __init__(self, status, body=""):
                self.status_code = status
                self.text = body
                self.headers = {}
            def json(self):
                return {"items": []}

        def fake_get(*a, **kw):
            call_count[0] += 1
            if call_count[0] < 3:
                return FakeResp(500, "oops")
            return FakeResp(200)

        monkeypatch.setattr(gh, "_RETRY_BASE_SLEEP", 0)   # no real sleep in tests
        monkeypatch.setattr(httpx, "get", fake_get)
        result = gh._request("/search/issues", {"q": "test"})
        assert call_count[0] == 3
        assert result == {"items": []}

    def test_rate_limit_window_blocks_subsequent_calls(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        import time
        monkeypatch.setattr(gh, "_rate_limit_reset_epoch", time.time() + 3600)
        with pytest.raises(gh.GitHubError, match="rate limit"):
            gh._check_rate_limit_window()
        # cleanup so other tests aren't affected
        monkeypatch.setattr(gh, "_rate_limit_reset_epoch", 0.0)


class TestEmbedderSingleton:
    def test_embedder_is_reused_across_calls(self, monkeypatch):
        import src.ask_buddy.retrieve as ret
        # Reset module-level singleton so this test is self-contained
        monkeypatch.setattr(ret, "_embedder", None)
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

        creation_count = [0]
        original_cls = __import__(
            "langchain_google_genai", fromlist=["GoogleGenerativeAIEmbeddings"]
        ).GoogleGenerativeAIEmbeddings

        class CountingEmbedder(original_cls):
            def __init__(self, *a, **kw):
                creation_count[0] += 1
                # Don't call super().__init__ to avoid real network calls
        import langchain_google_genai as lggai
        monkeypatch.setattr(lggai, "GoogleGenerativeAIEmbeddings", CountingEmbedder)
        monkeypatch.setattr(ret, "GoogleGenerativeAIEmbeddings", CountingEmbedder)

        ret._get_embedder()
        ret._get_embedder()
        assert creation_count[0] == 1, "Embedder should be constructed only once"
        monkeypatch.setattr(ret, "_embedder", None)  # cleanup


class TestNewGitHubClientFunctions:
    def test_remove_label_calls_delete(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        calls = []
        monkeypatch.setattr(
            gh, "_request_write",
            lambda method, path, json_body=None: calls.append((method, path)) or [{"name": "other"}]
        )
        result = gh.remove_label("a/b", 1, "bug")
        assert calls[0][0] == "DELETE"
        assert "labels/bug" in calls[0][1]
        assert result["labels_remaining"] == ["other"]

    def test_unassign_users_calls_delete(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        calls = []
        monkeypatch.setattr(
            gh, "_request_write",
            lambda method, path, json_body=None: calls.append((method, path)) or {"assignees": []}
        )
        gh.unassign_users("a/b", 1, ["alice"])
        assert calls[0][0] == "DELETE"
        assert "assignees" in calls[0][1]

    def test_create_issue_returns_number_and_url(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        monkeypatch.setattr(
            gh, "_request_write",
            lambda method, path, json_body=None: {
                "number": 99, "html_url": "http://x/99", "state": "open"
            }
        )
        result = gh.create_issue("a/b", "New bug", body="details")
        assert result["number"] == 99
        assert result["url"] == "http://x/99"

    def test_create_pull_request_returns_pr_dict(self, monkeypatch):
        import src.ask_buddy.github_client as gh
        monkeypatch.setattr(
            gh, "_request_write",
            lambda method, path, json_body=None: {
                "number": 42, "title": "My PR", "state": "open",
                "draft": False, "user": {"login": "alice"}, "labels": [],
                "requested_reviewers": [], "base": {"ref": "main"},
                "head": {"ref": "feat", "sha": "abc"},
                "mergeable_state": None, "created_at": None,
                "updated_at": None, "html_url": "http://x/42", "body": "",
            }
        )
        result = gh.create_pull_request("a/b", "My PR", "feat", "main")
        assert result["number"] == 42
        assert result["url"] == "http://x/42"


class TestGitWatchRateLimitSkip:
    def test_rate_limit_error_logs_warning_not_generic(self, monkeypatch, caplog):
        import logging
        import src.ask_buddy.git_watch as gw
        import src.ask_buddy.github_client as gh

        monkeypatch.setenv("GIT_WATCH_CHANNEL", "eng")

        def _raise_rate_limit(*a, **kw):
            raise gh.GitHubError("GitHub rate limit active — resets in ~120s.")

        monkeypatch.setattr(gh, "list_issues", _raise_rate_limit)
        with caplog.at_level(logging.WARNING, logger="ask_buddy.git_watch"):
            gw.poll_once("a/b", lambda ch, txt: None)

        assert any("rate limited" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Event routing and threaded replies
# ---------------------------------------------------------------------------

class TestDirectMessageGuard:
    """The 'message' event also fires for channels once the app subscribes to
    message.channels — the handler must filter on channel_type itself rather
    than trusting the Slack event subscription to stay narrow."""

    def test_dm_is_handled(self):
        import src.ask_buddy.slack_listener as sl
        assert sl._is_direct_message({"channel_type": "im", "text": "hi"})

    def test_public_channel_message_is_ignored(self):
        import src.ask_buddy.slack_listener as sl
        assert not sl._is_direct_message({"channel_type": "channel", "text": "hi"})

    def test_private_channel_message_is_ignored(self):
        import src.ask_buddy.slack_listener as sl
        assert not sl._is_direct_message({"channel_type": "group", "text": "hi"})

    def test_group_dm_is_ignored(self):
        import src.ask_buddy.slack_listener as sl
        assert not sl._is_direct_message({"channel_type": "mpim", "text": "hi"})

    def test_missing_channel_type_is_ignored(self):
        import src.ask_buddy.slack_listener as sl
        assert not sl._is_direct_message({"text": "hi"})


class TestThreadKwargs:
    def test_thread_ts_is_passed_through(self):
        import src.ask_buddy.slack_listener as sl
        assert sl._thread_kwargs("1700000000.001") == {"thread_ts": "1700000000.001"}

    def test_none_yields_no_kwarg(self):
        import src.ask_buddy.slack_listener as sl
        assert sl._thread_kwargs(None) == {}

    def test_empty_string_yields_no_kwarg(self):
        import src.ask_buddy.slack_listener as sl
        assert sl._thread_kwargs("") == {}


class TestPromptFingerprint:
    """agent_config must distinguish a prompt edit from a model swap."""

    def test_fingerprint_is_short_stable_hex(self):
        from src.ask_buddy.agent import prompt_fingerprint
        first = prompt_fingerprint()
        assert len(first) == 8
        assert all(c in "0123456789abcdef" for c in first)
        # Deterministic across calls in one process.
        assert prompt_fingerprint() == first

    def test_fingerprint_is_part_of_agent_config(self):
        from src.ask_buddy.agent import current_agent_config, prompt_fingerprint
        assert current_agent_config().endswith(f":p{prompt_fingerprint()}")

    def test_agent_config_tracks_provider_and_model(self, monkeypatch):
        from src.ask_buddy.agent import current_agent_config
        monkeypatch.setenv("AGENT_SETTING_CONFIG", "settings.google.toml")
        monkeypatch.setenv("MODEL_NAME", "gemini-2.5-flash")
        config = current_agent_config()
        assert config.startswith("settings.google.toml:gemini-2.5-flash:p")

    def test_editing_a_prompt_changes_the_fingerprint(self, monkeypatch):
        """Guards the whole point: a prompt change must be visible in the
        config string the feedback report groups by."""
        import src.ask_buddy.agent as agent_mod
        before = agent_mod.prompt_fingerprint()
        monkeypatch.setattr(agent_mod, "SUPERVISOR_PROMPT",
                            agent_mod.SUPERVISOR_PROMPT + "\nAn extra rule.")
        assert agent_mod.prompt_fingerprint() != before


# ---------------------------------------------------------------------------
# Corpus registry — the derived artifacts
# ---------------------------------------------------------------------------

class TestCorpusRegistryDerivation:
    """Everything a corpus needs is generated from its registry entry, so a
    new domain can't be half-wired."""

    def test_a_retrieval_tool_exists_per_corpus(self):
        from src.ask_buddy.corpora import CORPORA
        from src.ask_buddy.retrieve import RETRIEVE_TOOLS
        assert set(RETRIEVE_TOOLS) == {c.name for c in CORPORA}

    def test_tool_names_match_the_registry(self):
        from src.ask_buddy.corpora import CORPORA
        from src.ask_buddy.retrieve import retrieve_tool_for
        for corpus in CORPORA:
            assert retrieve_tool_for(corpus.name).name == corpus.tool_name

    def test_tool_docstring_carries_the_topics_for_routing(self):
        """The LLM picks a tool from its description, so the topics must be in
        there."""
        from src.ask_buddy.corpora import by_name
        from src.ask_buddy.retrieve import retrieve_tool_for
        corpus = by_name("it")
        description = retrieve_tool_for("it").description
        assert "VPN" in description
        assert corpus.label in description

    def test_named_bindings_still_point_at_the_generated_tools(self):
        from src.ask_buddy.retrieve import RETRIEVE_TOOLS, hr_retrieve, it_retrieve
        assert hr_retrieve is RETRIEVE_TOOLS["hr"]
        assert it_retrieve is RETRIEVE_TOOLS["it"]

    def test_a_prompt_exists_per_corpus(self):
        from src.ask_buddy.agent import DOMAIN_PROMPTS
        from src.ask_buddy.corpora import CORPORA
        assert set(DOMAIN_PROMPTS) == {c.name for c in CORPORA}

    def test_every_domain_prompt_carries_the_shared_guardrails(self):
        """The anti-hallucination block must not be something a new corpus can
        forget to include."""
        from src.ask_buddy.agent import DOMAIN_PROMPTS
        for name, prompt in DOMAIN_PROMPTS.items():
            assert "Never answer from general knowledge." in prompt, name
            assert "No results found in our documents" in prompt, name
            assert "Source(s):" in prompt, name

    def test_domain_prompt_names_its_own_tool(self):
        from src.ask_buddy.agent import DOMAIN_PROMPTS
        from src.ask_buddy.corpora import CORPORA
        for corpus in CORPORA:
            assert corpus.tool_name in DOMAIN_PROMPTS[corpus.name]

    def test_supervisor_prompt_routes_to_every_corpus(self):
        """A corpus the supervisor doesn't mention is unreachable."""
        from src.ask_buddy.agent import SUPERVISOR_PROMPT
        from src.ask_buddy.corpora import CORPORA
        for corpus in CORPORA:
            assert corpus.agent_name in SUPERVISOR_PROMPT

    def test_supervisor_registers_an_agent_per_corpus(self):
        from src.ask_buddy.agent import build_supervisor
        from src.ask_buddy.corpora import CORPORA
        supervisor = build_supervisor(slack_post_fn=lambda ch, txt: None)
        for corpus in CORPORA:
            assert corpus.agent_name in supervisor._agents

    def test_domain_agent_gets_only_its_own_retrieval_tool(self, monkeypatch):
        """Corpus isolation is enforced by the tool list, not just the prompt.

        Captures what build_domain_agent passes to CugaAgent rather than
        reaching into the constructed agent, so this doesn't depend on CUGA's
        internal attribute names.
        """
        import src.ask_buddy.agent as agent_mod
        from src.ask_buddy.corpora import by_name

        captured: dict = {}

        class RecordingAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(agent_mod, "CugaAgent", RecordingAgent)
        agent_mod.build_domain_agent(by_name("it"), slack_post_fn=lambda ch, txt: None)

        names = {t.name for t in captured["tools"]}
        assert names == {"it_retrieve", "post_slack_message"}
        assert captured["enable_knowledge"] is False

    def test_domain_agent_prompt_is_its_own(self, monkeypatch):
        import src.ask_buddy.agent as agent_mod
        from src.ask_buddy.corpora import by_name

        captured: dict = {}

        class RecordingAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(agent_mod, "CugaAgent", RecordingAgent)
        agent_mod.build_domain_agent(by_name("hr"), slack_post_fn=lambda ch, txt: None)
        assert "hr_retrieve" in captured["special_instructions"]
        assert "it_retrieve" not in captured["special_instructions"]


# ---------------------------------------------------------------------------
# Integration: the SQL added for citation validation and approval auditing
# ---------------------------------------------------------------------------
#
# These exist because the DDL and queries below can only be verified against a
# real Postgres. CI's integration job runs them after ingest.

@integration
class TestCitationSchemaAndQueries:
    def test_citation_status_column_exists(self):
        from src.ask_buddy.db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'ask_buddy_feedback'
                      AND column_name = 'citation_status';
                """)
                assert cur.fetchone() is not None

    def test_get_known_sources_returns_ingested_metadata(self):
        from src.ask_buddy.db import get_known_sources
        known = get_known_sources(force_refresh=True)
        assert known, "corpus appears empty — run ingest first"
        filenames = {f for f, _ in known}
        assert "pto_policy.md" in filenames
        assert all(isinstance(f, str) and isinstance(s, str) for f, s in known)

    def test_known_sources_validate_a_real_citation(self):
        """End-to-end: metadata from the DB satisfies the validator."""
        from src.ask_buddy.citations import STATUS_OK, validate_citations
        from src.ask_buddy.db import get_known_sources
        known = get_known_sources(force_refresh=True)
        filename, section = sorted(known)[0]
        answer = f"Some answer.\n\nSource(s): {filename} — {section}"
        assert validate_citations(answer, known).status == STATUS_OK

    def test_known_sources_reject_a_fabricated_citation(self):
        from src.ask_buddy.citations import STATUS_UNKNOWN_FILE, validate_citations
        from src.ask_buddy.db import get_known_sources
        known = get_known_sources(force_refresh=True)
        answer = "Some answer.\n\nSource(s): definitely_not_a_real_doc.md — Nope"
        assert validate_citations(answer, known).status == STATUS_UNKNOWN_FILE

    def test_known_sources_cache_is_reused(self):
        from src.ask_buddy.db import get_known_sources
        first = get_known_sources(force_refresh=True)
        assert get_known_sources() == first

    def test_citation_status_counts_query_runs(self):
        from src.ask_buddy.db import get_citation_status_counts
        rows = get_citation_status_counts()
        assert isinstance(rows, list)
        for row in rows:
            assert "citation_status" in row and "hits" in row

    def test_bad_citation_rows_query_runs(self):
        from src.ask_buddy.db import get_bad_citation_rows
        assert isinstance(get_bad_citation_rows(limit=5), list)

    def test_chunk_quality_cache_and_refresh(self):
        from src.ask_buddy.db import get_chunk_quality
        cached = get_chunk_quality()
        assert isinstance(cached, dict)
        assert isinstance(get_chunk_quality(force_refresh=True), dict)


@integration
class TestPendingApprovalPersistence:
    def _clean(self, thread_id):
        from src.ask_buddy.db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ask_buddy_pending_approvals "
                            "WHERE thread_id = %s;", (thread_id,))

    def test_insert_then_resolve_roundtrip(self):
        from src.ask_buddy.db import (
            get_unresolved_approvals, insert_pending_approval,
            resolve_pending_approval,
        )
        thread_id = "test-approval-roundtrip"
        self._clean(thread_id)
        try:
            row_id = insert_pending_approval(
                thread_id=thread_id, channel="C1", user_text="merge PR #1",
                user_id="U1", thread_ts="1700000000.001", agent_config="cfg",
            )
            assert row_id is not None
            assert any(r["thread_id"] == thread_id for r in get_unresolved_approvals())

            assert resolve_pending_approval(thread_id, "confirmed", resolved_by="U1")
            assert not any(r["thread_id"] == thread_id
                           for r in get_unresolved_approvals())
            # A second resolve finds nothing left to close.
            assert not resolve_pending_approval(thread_id, "cancelled")
        finally:
            self._clean(thread_id)

    def test_only_one_unresolved_row_per_thread(self):
        """The partial unique index mirrors the in-memory no-clobber rule."""
        from src.ask_buddy.db import insert_pending_approval
        thread_id = "test-approval-dup"
        self._clean(thread_id)
        try:
            first = insert_pending_approval(thread_id=thread_id, channel="C1",
                                            user_text="merge PR #1")
            second = insert_pending_approval(thread_id=thread_id, channel="C1",
                                             user_text="close issue #9")
            assert first is not None
            assert second is None      # rejected, not raised
        finally:
            self._clean(thread_id)

    def test_orphaning_closes_everything_out(self):
        from src.ask_buddy.db import (
            get_unresolved_approvals, insert_pending_approval,
            orphan_unresolved_approvals,
        )
        thread_id = "test-approval-orphan"
        self._clean(thread_id)
        try:
            insert_pending_approval(thread_id=thread_id, channel="C1",
                                    user_text="merge PR #2")
            orphaned = orphan_unresolved_approvals()
            assert any(r["thread_id"] == thread_id for r in orphaned)
            assert get_unresolved_approvals() == []
        finally:
            self._clean(thread_id)


@integration
class TestKeywordSearchUsesOrSemantics:
    """
    Guards a defect that made hybrid retrieval silently vector-only.

    plainto_tsquery ANDs every lexeme, so a natural-language question
    ("How many days of PTO do I get after 5 years of service?") produced
    'mani' & 'day' & 'pto' & ... and matched no chunk at all — the exact shape
    of question Slack sends. The keyword half of RRF contributed nothing.
    """

    QUESTION = "How many days of PTO do I get after 5 years of service?"

    def test_natural_language_question_matches_something(self):
        from src.ask_buddy.retrieve import _keyword_search
        rows = _keyword_search(self.QUESTION, pool=20, corpus="hr")
        assert rows, "keyword search returned nothing for a natural question"

    def test_expected_document_ranks_first(self):
        from src.ask_buddy.retrieve import _keyword_search
        rows = _keyword_search(self.QUESTION, pool=20, corpus="hr")
        assert rows[0]["source_filename"] == "pto_policy.md"

    def test_ranking_still_discriminates(self):
        """OR semantics must not flatten the ranking — a chunk matching more
        terms has to outrank one matching fewer."""
        from src.ask_buddy.retrieve import _keyword_search
        rows = _keyword_search(self.QUESTION, pool=20, corpus="hr")
        scores = [float(r["score"]) for r in rows]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] > scores[-1], "all results scored identically"

    def test_corpus_filter_still_applies(self):
        from src.ask_buddy.retrieve import _keyword_search
        rows = _keyword_search("password rotation VPN policy", pool=20, corpus="it")
        assert rows
        assert {r["source_filename"] for r in rows} == {"it_security_policy.md"}

    def test_all_stopword_query_returns_empty_not_an_error(self):
        """to_tsquery('') raises a syntax error; the coalesce guard covers it."""
        from src.ask_buddy.retrieve import _keyword_search
        assert _keyword_search("the and of", pool=20, corpus="hr") == []

    def test_empty_query_returns_empty_not_an_error(self):
        from src.ask_buddy.retrieve import _keyword_search
        assert _keyword_search("", pool=20, corpus="hr") == []
