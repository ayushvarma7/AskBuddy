"""
Nightly LLM-as-judge evaluation for Ask Buddy (self-tuning loop, point 4).

Replays the human-curated regression cases in tests/regression_evals.json
(the same file feedback_report.py --export-evals produces) through live
retrieval, then:

  1. Deterministic check — did retrieval surface at least one of the
     human-annotated `expected_sources`?  This is the hard pass/fail, the
     same assertion the pytest regression suite makes.
  2. LLM judge — a Gemini call rates whether the retrieved context is
     actually sufficient to answer the question (0.0–1.0) and why.  This is
     the soft quality signal that catches "retrieval found the doc but the
     text is too thin to answer" cases a source-overlap check misses.

Results are written to ask_buddy_eval_runs so quality is tracked run-over-run,
and a summary (with regression deltas vs the previous run) is posted to Slack.

Usage:
    uv run python -m src.ask_buddy.eval_runner            # run + post
    uv run python -m src.ask_buddy.eval_runner --dry-run  # run + print, no post
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("ask_buddy.eval_runner")

_EVAL_FILE = Path(__file__).resolve().parents[2] / "tests" / "regression_evals.json"


def _load_cases() -> list[dict]:
    """Load curated eval cases that a human annotated with expected_sources."""
    if not _EVAL_FILE.exists():
        return []
    try:
        data = json.loads(_EVAL_FILE.read_text())
    except json.JSONDecodeError:
        return []
    return [c for c in data if c.get("expected_sources")]


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

_JUDGE_PROMPT = """\
You are grading a retrieval system for a workplace policy assistant.

Question:
{question}

Retrieved context (policy chunks):
{context}

Rate, from 0.0 to 1.0, how sufficient this retrieved context is to fully and
correctly answer the question. 1.0 = everything needed is present; 0.0 = the
context is irrelevant or missing the answer. Respond as strict JSON only:
{{"score": <float 0-1>, "reason": "<one short sentence>"}}
"""


def _judge(question: str, chunks: list[dict]) -> tuple[float | None, str]:
    """Ask the LLM to rate context sufficiency. Returns (score, reason).
    Returns (None, ...) if the judge can't run — never raises."""
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None, "judge skipped: GOOGLE_API_KEY unset"
    context = "\n\n".join(
        f"[{c.get('source_filename')} — {c.get('section')}]\n{c.get('chunk_text', '')[:800]}"
        for c in chunks if "error" not in c
    ) or "(no chunks retrieved)"
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        model = os.environ.get("MODEL_NAME", "gemini-2.5-flash")
        llm = ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0)
        resp = llm.invoke(_JUDGE_PROMPT.format(question=question, context=context))
        text = resp.content if hasattr(resp, "content") else str(resp)
        # Tolerate code fences / stray prose around the JSON.
        start, end = text.find("{"), text.rfind("}")
        parsed = json.loads(text[start:end + 1]) if start != -1 and end != -1 else {}
        score = parsed.get("score")
        score = float(score) if score is not None else None
        if score is not None:
            score = max(0.0, min(1.0, score))
        return score, str(parsed.get("reason", ""))[:300]
    except Exception as e:
        log.warning("[eval] judge failed for %r: %s", question[:60], e)
        return None, f"judge error: {e}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_eval(dry_run: bool = False, post_fn=None) -> dict:
    """
    Run the full curated eval suite once. Returns a summary dict. Posts a Slack
    summary via `post_fn(channel, text)` (or a fresh WebClient) unless dry_run.
    """
    from .retrieve import _hybrid_retrieve_core
    from .agent import current_agent_config

    cases = _load_cases()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:6]
    agent_config = current_agent_config()

    results = []
    passed_count = 0
    scores: list[float] = []

    for case in cases:
        question = case["question"]
        expected = set(case.get("expected_sources", []))
        chunks = _hybrid_retrieve_core(question, top_k=8)
        got = {c.get("source_filename") for c in chunks if "error" not in c}
        passed = bool(got & expected)
        score, notes = _judge(question, chunks)
        if passed:
            passed_count += 1
        if score is not None:
            scores.append(score)

        results.append({
            "question": question, "passed": passed, "score": score,
            "got": got, "expected": expected, "notes": notes,
        })

        if not dry_run:
            try:
                from .db import insert_eval_result
                insert_eval_result(
                    run_id=run_id, question=question, passed=passed, score=score,
                    got_sources=", ".join(sorted(g for g in got if g)),
                    expected_srcs=", ".join(sorted(expected)),
                    judge_notes=notes, agent_config=agent_config,
                )
            except Exception as e:
                log.warning("[eval] could not persist result: %s", e)

    total = len(cases)
    avg_score = round(sum(scores) / len(scores), 2) if scores else None
    summary = {
        "run_id": run_id, "total": total, "passed": passed_count,
        "avg_score": avg_score, "results": results, "agent_config": agent_config,
    }

    text = _format_summary(summary)
    if dry_run:
        print(text)
    else:
        _post_summary(text, post_fn)
    return summary


def _format_summary(summary: dict) -> str:
    total = summary["total"]
    if total == 0:
        return ("🧪 *Ask Buddy nightly eval* — no curated cases found. "
                "Export some with `feedback_report --export-evals tests/regression_evals.json` "
                "and fill in `expected_sources`.")
    passed = summary["passed"]
    pct = (passed / total * 100) if total else 0
    avg = summary["avg_score"]

    # Regression delta vs the previous run.
    delta_line = ""
    try:
        from .db import get_eval_run_history
        history = get_eval_run_history(limit=2)
        prev = next((h for h in history if h["run_id"] != summary["run_id"]), None)
        if prev and prev["total"]:
            prev_pct = prev["passed"] / prev["total"] * 100
            diff = pct - prev_pct
            arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "±")
            delta_line = f"  ({arrow}{abs(diff):.0f}pt vs previous run)"
    except Exception:
        pass

    lines = [
        "🧪 *Ask Buddy — nightly retrieval eval*",
        f"• Pass rate: *{passed}/{total}* ({pct:.0f}%){delta_line}",
        f"• Avg judge score: *{avg if avg is not None else 'n/a'}*",
        f"• Config: `{summary['agent_config']}`",
    ]
    failures = [r for r in summary["results"] if not r["passed"]]
    if failures:
        lines.append("\n*Failing cases (retrieval missed expected source):*")
        for r in failures[:8]:
            lines.append(f"  • {r['question'][:80]} — got {sorted(g for g in r['got'] if g) or '∅'}")
    return "\n".join(lines)


def _post_summary(text: str, post_fn=None) -> None:
    if post_fn is not None:
        channel = (os.environ.get("ASK_BUDDY_EVAL_CHANNEL")
                   or os.environ.get("ASK_BUDDY_DIGEST_CHANNEL")
                   or os.environ.get("GIT_WATCH_CHANNEL"))
        if channel:
            post_fn(channel, text)
        else:
            log.info("[eval] no eval channel configured; skipping post.\n%s", text)
        return

    channel = os.environ.get("ASK_BUDDY_EVAL_CHANNEL") or os.environ.get("ASK_BUDDY_DIGEST_CHANNEL")
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not channel or not token:
        log.info("[eval] no channel/token; printing summary instead.\n%s", text)
        print(text)
        return
    from slack_sdk import WebClient
    WebClient(token=token).chat_postMessage(channel=channel, text=text)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def start_eval_scheduler(slack_post_fn):
    """Start the nightly eval scheduler. No-op (returns None) unless curated
    cases exist and GOOGLE_API_KEY is set. Cron via ASK_BUDDY_EVAL_CRON
    (default 03:00 daily, America/Los_Angeles)."""
    if not _load_cases():
        log.info("[eval] no curated regression_evals.json — nightly eval disabled.")
        return None
    if not os.environ.get("GOOGLE_API_KEY"):
        log.info("[eval] GOOGLE_API_KEY unset — nightly eval disabled.")
        return None

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    cron = os.environ.get("ASK_BUDDY_EVAL_CRON", "0 3 * * *").strip()
    tz = os.environ.get("ASK_BUDDY_EVAL_TIMEZONE", "America/Los_Angeles").strip()
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: run_eval(dry_run=False, post_fn=slack_post_fn),
        trigger=CronTrigger.from_crontab(cron, timezone=tz),
        id="ask-buddy-nightly-eval",
        replace_existing=True,
    )
    scheduler.start()
    log.info("[eval] nightly eval scheduled via cron '%s' %s", cron, tz)
    return scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask Buddy nightly LLM-judge eval")
    parser.add_argument("--dry-run", action="store_true", help="Run + print, don't post or persist")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run_eval(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
