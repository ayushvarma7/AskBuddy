"""
Ask Buddy feedback analytics.

Usage:
    uv run python -m src.ask_buddy.feedback_report
    uv run python -m src.ask_buddy.feedback_report --cluster
    uv run python -m src.ask_buddy.feedback_report --export-evals evals.json

Prints:
  - Overall % positive / % negative / % unrated
  - Refusal breakdown (and 👎 refusals = doc-gap candidates)
  - Negative-feedback reason breakdown
  - Content-review queue (chunks with repeated thumbs-down)
  - Most common negatively- / positively-rated questions
  - Negative rate by agent config (A/B)
  - Recent unrated responses (possible silent failures)

Optional:
  --cluster        group negatively-rated questions by embedding similarity
  --export-evals   dump negatively-rated questions as regression eval candidates
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from .db import get_conn, get_low_quality_chunks, get_negative_feedback_rows


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _print_summary(cur) -> None:
    cur.execute("""
        SELECT
            COUNT(*)                                        AS total,
            COUNT(*) FILTER (WHERE feedback = 'positive')   AS positive,
            COUNT(*) FILTER (WHERE feedback = 'negative')   AS negative,
            COUNT(*) FILTER (WHERE feedback IS NULL)        AS unrated
        FROM ask_buddy_feedback;
    """)
    row = dict(cur.fetchone())
    total = row["total"] or 0
    positive = row["positive"] or 0
    negative = row["negative"] or 0
    unrated = row["unrated"] or 0
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


def _print_refusals(cur) -> None:
    """Refusals split out: a 👎 on a refusal means 'this should be answerable'."""
    cur.execute("""
        SELECT
            COUNT(*)                                       AS refusals,
            COUNT(*) FILTER (WHERE feedback = 'negative')  AS should_have_answered,
            COUNT(*) FILTER (WHERE feedback = 'positive')  AS correct_refusals
        FROM ask_buddy_feedback
        WHERE is_refusal = TRUE;
    """)
    r = dict(cur.fetchone())
    print("  Refusals (\"No results found\"):")
    print(f"    Total refusals            : {r['refusals'] or 0}")
    print(f"    👎 should've been answered : {r['should_have_answered'] or 0}   <- doc-gap candidates")
    print(f"    👍 correctly refused       : {r['correct_refusals'] or 0}")
    print()

    # The actual doc-gap questions worth adding to the corpus.
    cur.execute("""
        SELECT question, COUNT(*) AS hits
        FROM ask_buddy_feedback
        WHERE is_refusal = TRUE AND feedback = 'negative'
        GROUP BY question
        ORDER BY hits DESC, question
        LIMIT 10;
    """)
    gaps = [dict(x) for x in cur.fetchall()]
    print("  Top doc-gap questions (refused but users wanted an answer):")
    if gaps:
        for g in gaps:
            print(f"    [{g['hits']}x] {g['question'][:90]}")
    else:
        print("    (none yet)")
    print()


def _print_reasons(cur) -> None:
    cur.execute("""
        SELECT COALESCE(feedback_reason, '(no reason given)') AS reason,
               COUNT(*) AS hits
        FROM ask_buddy_feedback
        WHERE feedback = 'negative'
        GROUP BY reason
        ORDER BY hits DESC;
    """)
    rows = [dict(x) for x in cur.fetchall()]
    print("  👎 reasons:")
    if rows:
        for r in rows:
            print(f"    {r['hits']:>3}x  {r['reason']}")
    else:
        print("    (none yet)")
    print()


def _print_content_review_queue() -> None:
    chunks = get_low_quality_chunks(min_negative=3)
    print("  Content-review queue (chunks with 3+ 👎):")
    if chunks:
        for c in chunks:
            preview = " ".join(c["chunk_text"].split())[:70]
            print(f"    [{c['negative']}👎/{c['positive']}👍] "
                  f"{c['source_filename']} — {c['section'][:40]}")
            print(f"        \"{preview}…\"")
    else:
        print("    (no chunks with repeated negative feedback)")
    print()


def _print_top_questions(cur) -> None:
    cur.execute("""
        SELECT question, COUNT(*) AS hits
        FROM ask_buddy_feedback
        WHERE feedback = 'negative' AND is_refusal = FALSE
        GROUP BY question
        ORDER BY hits DESC, question
        LIMIT 10;
    """)
    neg = [dict(x) for x in cur.fetchall()]
    print("  Top negatively-rated answers (non-refusal):")
    if neg:
        for r in neg:
            print(f"    [{r['hits']}x] {r['question'][:90]}")
    else:
        print("    (none yet)")
    print()

    cur.execute("""
        SELECT question, COUNT(*) AS hits
        FROM ask_buddy_feedback
        WHERE feedback = 'positive'
        GROUP BY question
        ORDER BY hits DESC, question
        LIMIT 10;
    """)
    pos = [dict(x) for x in cur.fetchall()]
    print("  Top positively-rated questions:")
    if pos:
        for r in pos:
            print(f"    [{r['hits']}x] {r['question'][:90]}")
    else:
        print("    (none yet)")
    print()


def _print_config_ab(cur) -> None:
    cur.execute("""
        SELECT COALESCE(agent_config, '(untagged)') AS config,
               COUNT(*) FILTER (WHERE feedback IS NOT NULL)      AS rated,
               COUNT(*) FILTER (WHERE feedback = 'negative')     AS negative
        FROM ask_buddy_feedback
        GROUP BY config
        ORDER BY rated DESC;
    """)
    rows = [dict(x) for x in cur.fetchall()]
    print("  Negative rate by agent config (A/B):")
    if any(r["rated"] for r in rows):
        for r in rows:
            rated = r["rated"] or 0
            neg = r["negative"] or 0
            pct = (neg / rated * 100) if rated else 0
            print(f"    {pct:5.1f}%  ({neg}/{rated})  {r['config']}")
    else:
        print("    (no rated responses yet)")
    print()


def _print_recent_unrated(cur) -> None:
    cur.execute("""
        SELECT question, created_at
        FROM ask_buddy_feedback
        WHERE feedback IS NULL
        ORDER BY created_at DESC
        LIMIT 5;
    """)
    rows = [dict(x) for x in cur.fetchall()]
    print("  Recent unrated responses (last 5):")
    if rows:
        for r in rows:
            ts = r["created_at"].strftime("%Y-%m-%d %H:%M")
            print(f"    {ts}  {r['question'][:80]}")
    else:
        print("    (none)")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Optional: embedding-similarity clustering of negative questions
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def cluster_negative_questions(threshold: float = 0.80) -> None:
    """
    Greedy-cluster distinct negatively-rated questions by embedding
    similarity, surfacing topic clusters that literal string-grouping
    misses. Costs one embedding call per distinct question.
    """
    from .retrieve import _embed_query

    rows = get_negative_feedback_rows()
    questions = sorted({r["question"].strip() for r in rows if r["question"].strip()})
    print("  Negative-question clusters (embedding similarity):")
    if len(questions) < 2:
        print("    (need at least 2 distinct negative questions to cluster)")
        print()
        return

    embeddings = {q: _embed_query(q) for q in questions}

    clusters: list[list[str]] = []
    for q in questions:
        placed = False
        for cluster in clusters:
            if _cosine(embeddings[q], embeddings[cluster[0]]) >= threshold:
                cluster.append(q)
                placed = True
                break
        if not placed:
            clusters.append([q])

    clusters.sort(key=len, reverse=True)
    for i, cluster in enumerate(clusters, start=1):
        print(f"    Cluster {i} ({len(cluster)} question(s)):")
        for q in cluster:
            print(f"       - {q[:85]}")
    print()


# ---------------------------------------------------------------------------
# Optional: export negative questions as regression eval candidates
# ---------------------------------------------------------------------------

def export_eval_candidates(path: Path) -> None:
    """
    Write negatively-rated questions to a JSON file as regression eval
    candidates. Each entry is a question a human should curate an expected
    answer for, then add to the eval suite so it can't silently regress.
    """
    rows = get_negative_feedback_rows()
    seen: set[str] = set()
    candidates = []
    for r in rows:
        q = r["question"].strip()
        if not q or q in seen:
            continue
        seen.add(q)
        candidates.append({
            "question": q,
            "was_refusal": r["is_refusal"],
            "reason": r["feedback_reason"],
            "sources_cited": r["sources_cited"],
            "expected_answer": "",     # <- fill in during curation
            "expected_sources": [],    # <- fill in during curation
        })
    path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False))
    print(f"Exported {len(candidates)} eval candidate(s) to {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_report(cluster: bool = False) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            _print_summary(cur)
            _print_refusals(cur)
            _print_reasons(cur)
    # These open their own connections.
    _print_content_review_queue()
    with get_conn() as conn:
        with conn.cursor() as cur:
            _print_top_questions(cur)
            _print_config_ab(cur)
    if cluster:
        cluster_negative_questions()
    with get_conn() as conn:
        with conn.cursor() as cur:
            _print_recent_unrated(cur)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask Buddy feedback analytics")
    parser.add_argument(
        "--cluster",
        action="store_true",
        help="Cluster negative questions by embedding similarity (uses the embedder)",
    )
    parser.add_argument(
        "--export-evals",
        type=Path,
        metavar="PATH",
        help="Export negatively-rated questions as JSON regression eval candidates",
    )
    args = parser.parse_args()

    if args.export_evals:
        export_eval_candidates(args.export_evals)
        return

    run_report(cluster=args.cluster)


if __name__ == "__main__":
    main()
