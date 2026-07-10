"""
Weekly Ask Buddy feedback digest → Slack.

Posts a summary of the last 7 days of negative feedback and doc gaps to a
Slack channel so whoever owns the HR corpus sees problems without having to
run the CLI report themselves.

Usage:
    uv run python -m src.ask_buddy.feedback_digest            # post to Slack
    uv run python -m src.ask_buddy.feedback_digest --dry-run  # print, don't post
    uv run python -m src.ask_buddy.feedback_digest --days 30  # custom window

Required environment:
    SLACK_BOT_TOKEN            xoxb-...
    ASK_BUDDY_DIGEST_CHANNEL   channel id (e.g. C0123ABCD) to post the digest to
    ASK_BUDDY_DB_DSN           postgres dsn

Schedule it (not automatic) via cron / launchd, e.g. Mondays at 09:00:
    0 9 * * 1  cd /path/to/AskBuddy && uv run python -m src.ask_buddy.feedback_digest
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from .db import get_conn, get_low_quality_chunks, get_negative_feedback_rows


def _collect(days: int) -> dict:
    """Gather the digest figures for the trailing `days`-day window."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    now()                                                    AS window_end,
                    now() - (%(days)s || ' days')::interval                  AS window_start,
                    COUNT(*) FILTER (WHERE feedback IS NOT NULL)            AS rated,
                    COUNT(*) FILTER (WHERE feedback = 'negative')           AS negative,
                    COUNT(*) FILTER (WHERE is_refusal AND feedback='negative') AS doc_gaps
                FROM ask_buddy_feedback
                WHERE created_at >= now() - (%(days)s || ' days')::interval;
                """,
                {"days": days},
            )
            totals = dict(cur.fetchone())

            cur.execute(
                """
                SELECT question, COUNT(*) AS hits
                FROM ask_buddy_feedback
                WHERE feedback = 'negative' AND is_refusal = TRUE
                  AND created_at >= now() - (%(days)s || ' days')::interval
                GROUP BY question ORDER BY hits DESC, question LIMIT 5;
                """,
                {"days": days},
            )
            doc_gap_questions = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT question, COUNT(*) AS hits
                FROM ask_buddy_feedback
                WHERE feedback = 'negative' AND is_refusal = FALSE
                  AND created_at >= now() - (%(days)s || ' days')::interval
                GROUP BY question ORDER BY hits DESC, question LIMIT 5;
                """,
                {"days": days},
            )
            wrong_answers = [dict(r) for r in cur.fetchall()]

    return {
        "window_start": totals["window_start"],
        "window_end": totals["window_end"],
        "rated": totals["rated"] or 0,
        "negative": totals["negative"] or 0,
        "doc_gaps": totals["doc_gaps"] or 0,
        "doc_gap_questions": doc_gap_questions,
        "wrong_answers": wrong_answers,
        "review_chunks": get_low_quality_chunks(min_negative=3),
    }


def _format_date_range(start, end) -> str:
    """e.g. 'Jul 3 – Jul 10, 2026' (or 'Jul 3 – 10, 2026' when same month)."""
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%b %-d')}–{end.strftime('%-d, %Y')}"
    if start.year == end.year:
        return f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"
    return f"{start.strftime('%b %-d, %Y')} – {end.strftime('%b %-d, %Y')}"


def _format_blocks(d: dict, days: int) -> list[dict]:
    rated = d["rated"]
    neg = d["negative"]
    pct = (neg / rated * 100) if rated else 0
    date_range = _format_date_range(d["window_start"], d["window_end"])

    def _lines(rows, empty):
        if not rows:
            return f"_{empty}_"
        return "\n".join(f"• [{r['hits']}×] {r['question'][:120]}" for r in rows)

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "📋 Ask Buddy — weekly feedback digest"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f"*{date_range}* ({days} days)\n"
                           f"• Rated answers: *{rated}*\n"
                           f"• Negative: *{neg}* ({pct:.0f}%)\n"
                           f"• Doc gaps (refused but wanted): *{d['doc_gaps']}*")}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": "*🕳️ Doc gaps — HR topics we couldn't answer:*\n"
                          + _lines(d["doc_gap_questions"], "none in this window")}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": "*👎 Answers marked wrong/unclear:*\n"
                          + _lines(d["wrong_answers"], "none in this window")}},
    ]

    if d["review_chunks"]:
        chunk_lines = "\n".join(
            f"• {c['source_filename']} — {c['section'][:50]} "
            f"({c['negative']}👎/{c['positive']}👍)"
            for c in d["review_chunks"][:5]
        )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "*🔧 Source text to review (3+ 👎):*\n" + chunk_lines},
        })

    return blocks


def run_digest(days: int = 7, dry_run: bool = False) -> None:
    data = _collect(days)
    blocks = _format_blocks(data, days)
    date_range = _format_date_range(data["window_start"], data["window_end"])
    fallback = (f"Ask Buddy weekly digest ({date_range}): {data['negative']} negative, "
                f"{data['doc_gaps']} doc gaps.")

    if dry_run:
        import json
        print(fallback)
        print(json.dumps(blocks, indent=2, ensure_ascii=False))
        return

    channel = os.environ.get("ASK_BUDDY_DIGEST_CHANNEL")
    if not channel:
        print("ERROR: ASK_BUDDY_DIGEST_CHANNEL is not set. Add it to .env "
              "(the channel id to post the digest to), or use --dry-run.",
              file=sys.stderr)
        sys.exit(1)

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    from slack_sdk import WebClient
    client = WebClient(token=token)
    client.chat_postMessage(channel=channel, text=fallback, blocks=blocks)
    print(f"Posted weekly digest to {channel}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Post the Ask Buddy feedback digest to Slack")
    parser.add_argument("--days", type=int, default=7, help="Trailing window in days (default 7)")
    parser.add_argument("--dry-run", action="store_true", help="Print the digest instead of posting")
    args = parser.parse_args()
    run_digest(days=args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
