"""
Ask Buddy feedback analytics.

Usage:
    uv run python -m src.ask_buddy.feedback_report

Prints:
  - Overall % positive / % negative / % unrated
  - Most common negatively-rated questions
  - Most common positively-rated questions
  - Recent unrated responses (possible bot failures)
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

from .db import get_conn


def run_report() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:

            # ── 1. Overall summary ──────────────────────────────────────────
            cur.execute("""
                SELECT
                    COUNT(*)                                          AS total,
                    COUNT(*) FILTER (WHERE feedback = 'positive')    AS positive,
                    COUNT(*) FILTER (WHERE feedback = 'negative')    AS negative,
                    COUNT(*) FILTER (WHERE feedback IS NULL)         AS unrated
                FROM ask_buddy_feedback;
            """)
            row = dict(cur.fetchone())
            total    = row["total"] or 0
            positive = row["positive"] or 0
            negative = row["negative"] or 0
            unrated  = row["unrated"] or 0

            pct_pos = (positive / total * 100) if total else 0
            pct_neg = (negative / total * 100) if total else 0

            print("=" * 60)
            print("  Ask Buddy — Feedback Report")
            print("=" * 60)
            print(f"  Total responses  : {total}")
            print(f"  👍  Positive      : {positive}  ({pct_pos:.1f}%)")
            print(f"  👎  Negative      : {negative}  ({pct_neg:.1f}%)")
            print(f"  —   Unrated       : {unrated}")
            print()

            # ── 2. Most common negatively-rated questions ───────────────────
            cur.execute("""
                SELECT question, COUNT(*) AS hits
                FROM ask_buddy_feedback
                WHERE feedback = 'negative'
                GROUP BY question
                ORDER BY hits DESC, question
                LIMIT 10;
            """)
            neg_rows = [dict(r) for r in cur.fetchall()]

            print("  Top negatively-rated questions:")
            if neg_rows:
                for r in neg_rows:
                    print(f"    [{r['hits']}x] {r['question'][:100]}")
            else:
                print("    (none yet)")
            print()

            # ── 3. Most common positively-rated questions ───────────────────
            cur.execute("""
                SELECT question, COUNT(*) AS hits
                FROM ask_buddy_feedback
                WHERE feedback = 'positive'
                GROUP BY question
                ORDER BY hits DESC, question
                LIMIT 10;
            """)
            pos_rows = [dict(r) for r in cur.fetchall()]

            print("  Top positively-rated questions:")
            if pos_rows:
                for r in pos_rows:
                    print(f"    [{r['hits']}x] {r['question'][:100]}")
            else:
                print("    (none yet)")
            print()

            # ── 4. Recent unrated (possible silent failures) ────────────────
            cur.execute("""
                SELECT question, created_at
                FROM ask_buddy_feedback
                WHERE feedback IS NULL
                ORDER BY created_at DESC
                LIMIT 5;
            """)
            unrated_rows = [dict(r) for r in cur.fetchall()]

            print("  Recent unrated responses (last 5):")
            if unrated_rows:
                for r in unrated_rows:
                    ts = r["created_at"].strftime("%Y-%m-%d %H:%M")
                    print(f"    {ts}  {r['question'][:90]}")
            else:
                print("    (none)")
            print()

            # ── 5. Raw SQL you can run yourself ────────────────────────────
            print("  Raw SQL for ad-hoc queries:")
            print("    -- All feedback rows:")
            print("    SELECT * FROM ask_buddy_feedback ORDER BY created_at DESC;")
            print()
            print("    -- Negative rate by week:")
            print("    SELECT date_trunc('week', created_at) AS week,")
            print("           ROUND(100.0 * COUNT(*) FILTER (WHERE feedback='negative')")
            print("                 / NULLIF(COUNT(*) FILTER (WHERE feedback IS NOT NULL),0), 1) AS pct_neg")
            print("    FROM ask_buddy_feedback")
            print("    GROUP BY 1 ORDER BY 1 DESC;")
            print("=" * 60)


if __name__ == "__main__":
    run_report()
