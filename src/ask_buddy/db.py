"""Database connection and schema management for Ask Buddy."""

from __future__ import annotations

import os
import threading
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager


# ---------------------------------------------------------------------------
# Feedback table DDL (shared by init_schema and init_feedback_schema)
# ---------------------------------------------------------------------------
#
# retrieved_chunk_ids : hr_chunks.id[] surfaced for this answer — lets us score
#                       chunk quality from downstream feedback.
# is_refusal          : TRUE when the answer was the "No results found" message.
# feedback_reason     : category chosen in the 👎 modal (wrong_info, no_source, …).
# agent_config        : which LLM/agent config produced the answer (for A/B).
_FEEDBACK_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_feedback (
        id                  SERIAL PRIMARY KEY,
        response_id         TEXT        NOT NULL UNIQUE,
        question            TEXT        NOT NULL,
        answer_text         TEXT        NOT NULL,
        sources_cited       TEXT        NOT NULL DEFAULT '',
        feedback            TEXT        CHECK (feedback IN ('positive','negative')),
        user_id             TEXT,
        retrieved_chunk_ids INTEGER[],
        is_refusal          BOOLEAN     NOT NULL DEFAULT FALSE,
        feedback_reason     TEXT,
        agent_config        TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- Idempotent upgrade path for databases created before these columns existed.
    ALTER TABLE ask_buddy_feedback
        ADD COLUMN IF NOT EXISTS retrieved_chunk_ids INTEGER[],
        ADD COLUMN IF NOT EXISTS is_refusal          BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS feedback_reason     TEXT,
        ADD COLUMN IF NOT EXISTS agent_config        TEXT;

    CREATE INDEX IF NOT EXISTS ask_buddy_feedback_response_idx
        ON ask_buddy_feedback (response_id);
    CREATE INDEX IF NOT EXISTS ask_buddy_feedback_feedback_idx
        ON ask_buddy_feedback (feedback);
    CREATE INDEX IF NOT EXISTS ask_buddy_feedback_refusal_idx
        ON ask_buddy_feedback (is_refusal);
"""


# ---------------------------------------------------------------------------
# Reminders table DDL (broadcast reminders scheduled via scheduler.py)
# ---------------------------------------------------------------------------
#
# cron_expression : standard 5-field cron syntax (minute hour day month dow),
#                   evaluated in `timezone`. Recurring by design — a reminder
#                   fires every time the cron schedule matches.
# active          : FALSE once cancelled; kept for history instead of deleted.
_REMINDERS_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_reminders (
        id              SERIAL PRIMARY KEY,
        channel_id      TEXT        NOT NULL,
        channel_name    TEXT,
        message         TEXT        NOT NULL,
        cron_expression TEXT        NOT NULL,
        timezone        TEXT        NOT NULL DEFAULT 'America/Los_Angeles',
        created_by      TEXT,
        active          BOOLEAN     NOT NULL DEFAULT TRUE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS ask_buddy_reminders_active_idx
        ON ask_buddy_reminders (active);
"""


# ---------------------------------------------------------------------------
# Git watch state (proactive triage dedup — one row per watched repo)
# ---------------------------------------------------------------------------
# last_issue_number / last_pr_number : highest number already reported to Slack.
#   On each poll we only report items with number > the stored high-water mark.
#   -1 is the sentinel meaning "never seeded" (a real repo can have 0 PRs,
#   so 0 is a valid watermark — not a safe sentinel).
_GIT_WATCH_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_git_watch (
        repo               TEXT        PRIMARY KEY,   -- 'owner/name'
        last_issue_number  INTEGER     NOT NULL DEFAULT -1,
        last_pr_number     INTEGER     NOT NULL DEFAULT -1,
        last_polled_at     TIMESTAMPTZ
    );
"""


# ---------------------------------------------------------------------------
# Git identity table DDL (Slack user -> GitHub login mapping)
# ---------------------------------------------------------------------------

_GIT_IDENTITIES_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_git_identities (
        slack_user_id  TEXT PRIMARY KEY,
        github_login   TEXT NOT NULL,
        linked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- Reverse lookup (github_login -> slack_user_id) for the stale-PR nudger.
    CREATE INDEX IF NOT EXISTS ask_buddy_git_identities_login_idx
        ON ask_buddy_git_identities (lower(github_login));
"""


# ---------------------------------------------------------------------------
# Git digest history (point 3 — weekly trend deltas, not just a snapshot)
# ---------------------------------------------------------------------------
# One row per digest run per repo. Lets the digest report deltas ("+3 open
# issues since last week", "review latency down 1.2 days") instead of a
# point-in-time snapshot.
_GIT_DIGEST_HISTORY_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_git_digest_history (
        id                       SERIAL PRIMARY KEY,
        repo                     TEXT        NOT NULL,
        open_issues              INTEGER     NOT NULL DEFAULT 0,
        closed_issues            INTEGER     NOT NULL DEFAULT 0,
        open_prs                 INTEGER     NOT NULL DEFAULT 0,
        closed_prs               INTEGER     NOT NULL DEFAULT 0,
        draft_prs                INTEGER     NOT NULL DEFAULT 0,
        review_needed            INTEGER     NOT NULL DEFAULT 0,
        avg_review_latency_hours REAL,
        captured_at              TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS ask_buddy_git_digest_history_repo_idx
        ON ask_buddy_git_digest_history (repo, captured_at DESC);
"""


# ---------------------------------------------------------------------------
# Stale-PR nudge dedup (point 3 — don't re-nudge the same PR every poll)
# ---------------------------------------------------------------------------
_PR_NUDGE_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_pr_nudges (
        repo            TEXT        NOT NULL,
        pr_number       INTEGER     NOT NULL,
        last_nudged_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (repo, pr_number)
    );
"""


# ---------------------------------------------------------------------------
# Per-user memory (point 4 — durable personalization facts)
# ---------------------------------------------------------------------------
# Small freeform facts the bot has learned about a Slack user (office, team,
# leave track, …). Injected into the supervisor prompt so answers personalise
# without re-asking. Kept deliberately tiny and user-scoped.
_USER_MEMORY_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_user_memory (
        id             SERIAL PRIMARY KEY,
        slack_user_id  TEXT        NOT NULL,
        fact           TEXT        NOT NULL,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS ask_buddy_user_memory_user_idx
        ON ask_buddy_user_memory (slack_user_id, created_at DESC);
"""


# ---------------------------------------------------------------------------
# Nightly eval runs (point 4 — LLM-as-judge regression tracking over time)
# ---------------------------------------------------------------------------
_EVAL_RUNS_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_eval_runs (
        id            SERIAL PRIMARY KEY,
        run_id        TEXT        NOT NULL,
        question      TEXT        NOT NULL,
        passed        BOOLEAN     NOT NULL DEFAULT FALSE,
        score         REAL,
        got_sources   TEXT,
        expected_srcs TEXT,
        judge_notes   TEXT,
        agent_config  TEXT,
        run_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS ask_buddy_eval_runs_run_idx
        ON ask_buddy_eval_runs (run_id);
    CREATE INDEX IF NOT EXISTS ask_buddy_eval_runs_time_idx
        ON ask_buddy_eval_runs (run_at DESC);
"""


def _dsn() -> str:
    dsn = os.environ.get("ASK_BUDDY_DB_DSN")
    if not dsn:
        raise RuntimeError(
            "ASK_BUDDY_DB_DSN environment variable is not set. "
            "Example: postgresql://askbuddy:secret@localhost:5432/askbuddy"
        )
    return dsn


# ---------------------------------------------------------------------------
# Connection pool — shared across all threads in the bot process.
# Initialised lazily on first use so the module is importable without a DB.
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

_POOL_MIN = 2
_POOL_MAX = int(os.environ.get("ASK_BUDDY_DB_POOL_MAX", "10"))


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:   # double-checked locking
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    _POOL_MIN, _POOL_MAX,
                    _dsn(),
                    cursor_factory=psycopg2.extras.RealDictCursor,
                )
    return _pool


@contextmanager
def get_conn():
    """Yield a pooled psycopg2 connection with autocommit disabled.
    Returns the connection to the pool on exit (commit on success,
    rollback on exception)."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_schema() -> None:
    """Create the pgvector extension, hr_chunks, and ask_buddy_feedback tables."""
    ddl = """
    CREATE EXTENSION IF NOT EXISTS vector;

    CREATE TABLE IF NOT EXISTS hr_chunks (
        id               SERIAL PRIMARY KEY,
        source_filename  TEXT        NOT NULL,
        section          TEXT        NOT NULL DEFAULT '',
        chunk_text       TEXT        NOT NULL,
        embedding        vector(768),
        effective_date   DATE,
        corpus           TEXT        NOT NULL DEFAULT 'hr',
        bm25_tsvector    tsvector
            GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED
    );

    ALTER TABLE hr_chunks
        ADD COLUMN IF NOT EXISTS corpus TEXT NOT NULL DEFAULT 'hr';

    CREATE INDEX IF NOT EXISTS hr_chunks_embedding_idx
        ON hr_chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);

    CREATE INDEX IF NOT EXISTS hr_chunks_fts_idx
        ON hr_chunks USING GIN (bm25_tsvector);

    CREATE INDEX IF NOT EXISTS hr_chunks_corpus_idx
        ON hr_chunks (corpus);

    -- Feedback table: one row per posted answer (rated or not)
    """ + _FEEDBACK_TABLE_DDL + """
    -- Tracks the content hash of each ingested source file so re-running
    -- the ingest pipeline can skip files that haven't changed.
    CREATE TABLE IF NOT EXISTS hr_ingested_files (
        source_filename  TEXT        NOT NULL,
        corpus           TEXT        NOT NULL DEFAULT 'hr',
        content_hash     TEXT        NOT NULL,
        chunk_count      INTEGER     NOT NULL,
        ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (source_filename, corpus)
    );

    ALTER TABLE hr_ingested_files
        ADD COLUMN IF NOT EXISTS corpus TEXT NOT NULL DEFAULT 'hr';
    -- Migrate from old single-column PK to composite PK (idempotent).
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'hr_ingested_files_pkey'
              AND conrelid = 'hr_ingested_files'::regclass
              AND array_length(conkey, 1) > 1
        ) THEN
            ALTER TABLE hr_ingested_files DROP CONSTRAINT IF EXISTS hr_ingested_files_pkey;
            ALTER TABLE hr_ingested_files ADD PRIMARY KEY (source_filename, corpus);
        END IF;
    END $$;

    -- Broadcast reminders scheduled via the in-process scheduler
    """ + _REMINDERS_TABLE_DDL + """
    -- Git watch state for proactive triage dedup
    """ + _GIT_WATCH_TABLE_DDL + """
    -- Slack user -> GitHub login identity mapping
    """ + _GIT_IDENTITIES_TABLE_DDL + """
    -- Git digest history (trend deltas)
    """ + _GIT_DIGEST_HISTORY_DDL + """
    -- Stale-PR nudge dedup
    """ + _PR_NUDGE_TABLE_DDL + """
    -- Per-user memory
    """ + _USER_MEMORY_TABLE_DDL + """
    -- Nightly eval runs
    """ + _EVAL_RUNS_TABLE_DDL + """
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
    print("Schema initialised.")


def init_git_identities_schema() -> None:
    """Idempotent: create just the git-identities table. Safe at bot startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_GIT_IDENTITIES_TABLE_DDL)


def link_github_identity(slack_user_id: str, github_login: str) -> None:
    """Upsert the Slack user -> GitHub login mapping."""
    sql = """
        INSERT INTO ask_buddy_git_identities (slack_user_id, github_login, linked_at)
        VALUES (%(slack_user_id)s, %(github_login)s, now())
        ON CONFLICT (slack_user_id) DO UPDATE
            SET github_login = EXCLUDED.github_login, linked_at = now();
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"slack_user_id": slack_user_id, "github_login": github_login})


def get_github_login(slack_user_id: str) -> str | None:
    """Return the linked GitHub login for a Slack user, or None if unlinked."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT github_login FROM ask_buddy_git_identities WHERE slack_user_id = %s;",
                (slack_user_id,),
            )
            row = cur.fetchone()
            return row["github_login"] if row else None


def get_slack_user_for_github_login(github_login: str) -> str | None:
    """Reverse of get_github_login: GitHub login -> Slack user id (case-insensitive).
    Used by the stale-PR nudger to DM the human behind a requested reviewer."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slack_user_id FROM ask_buddy_git_identities "
                "WHERE lower(github_login) = lower(%s) LIMIT 1;",
                (github_login,),
            )
            row = cur.fetchone()
            return row["slack_user_id"] if row else None


def all_linked_slack_users() -> list[dict]:
    """Return every (slack_user_id, github_login) mapping — used by the
    per-user proactive digest to know who to DM."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slack_user_id, github_login FROM ask_buddy_git_identities "
                "ORDER BY linked_at;"
            )
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Git digest history (trend deltas)
# ---------------------------------------------------------------------------

def init_git_digest_history_schema() -> None:
    """Idempotent: create just the digest-history table. Safe at bot startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_GIT_DIGEST_HISTORY_DDL)


def insert_digest_snapshot(repo: str, stats: dict) -> None:
    """Persist one digest snapshot so the next run can compute deltas.
    `stats` keys mirror the digest columns (missing keys default to 0/NULL)."""
    sql = """
        INSERT INTO ask_buddy_git_digest_history
            (repo, open_issues, closed_issues, open_prs, closed_prs,
             draft_prs, review_needed, avg_review_latency_hours)
        VALUES
            (%(repo)s, %(open_issues)s, %(closed_issues)s, %(open_prs)s,
             %(closed_prs)s, %(draft_prs)s, %(review_needed)s,
             %(avg_review_latency_hours)s);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "repo": repo,
                "open_issues": stats.get("open_issues", 0),
                "closed_issues": stats.get("closed_issues", 0),
                "open_prs": stats.get("open_prs", 0),
                "closed_prs": stats.get("closed_prs", 0),
                "draft_prs": stats.get("draft_prs", 0),
                "review_needed": stats.get("review_needed", 0),
                "avg_review_latency_hours": stats.get("avg_review_latency_hours"),
            })


def get_last_digest_snapshot(repo: str) -> dict | None:
    """Return the most recent stored snapshot for a repo, or None if none yet."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT open_issues, closed_issues, open_prs, closed_prs, "
                "draft_prs, review_needed, avg_review_latency_hours, captured_at "
                "FROM ask_buddy_git_digest_history "
                "WHERE repo = %s ORDER BY captured_at DESC LIMIT 1;",
                (repo,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# Stale-PR nudge dedup
# ---------------------------------------------------------------------------

def init_pr_nudge_schema() -> None:
    """Idempotent: create just the PR-nudge dedup table. Safe at bot startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_PR_NUDGE_TABLE_DDL)


def get_last_nudge_epoch(repo: str, pr_number: int) -> float | None:
    """Return the epoch seconds of the last nudge for a PR, or None if never."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT extract(epoch FROM last_nudged_at) AS epoch "
                "FROM ask_buddy_pr_nudges WHERE repo = %s AND pr_number = %s;",
                (repo, pr_number),
            )
            row = cur.fetchone()
            return float(row["epoch"]) if row and row["epoch"] is not None else None


def record_pr_nudge(repo: str, pr_number: int) -> None:
    """Upsert last_nudged_at = now() for a PR."""
    sql = """
        INSERT INTO ask_buddy_pr_nudges (repo, pr_number, last_nudged_at)
        VALUES (%(repo)s, %(pr)s, now())
        ON CONFLICT (repo, pr_number) DO UPDATE SET last_nudged_at = now();
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"repo": repo, "pr": pr_number})


# ---------------------------------------------------------------------------
# Per-user memory
# ---------------------------------------------------------------------------

def init_user_memory_schema() -> None:
    """Idempotent: create just the user-memory table. Safe at bot startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_USER_MEMORY_TABLE_DDL)


def add_user_memory(slack_user_id: str, fact: str) -> int:
    """Store one fact about a user. De-dups exact repeats. Returns the row id
    (or the existing id when the fact is a verbatim repeat)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM ask_buddy_user_memory "
                "WHERE slack_user_id = %s AND fact = %s LIMIT 1;",
                (slack_user_id, fact),
            )
            existing = cur.fetchone()
            if existing:
                return existing["id"]
            cur.execute(
                "INSERT INTO ask_buddy_user_memory (slack_user_id, fact) "
                "VALUES (%s, %s) RETURNING id;",
                (slack_user_id, fact),
            )
            return cur.fetchone()["id"]


def get_user_memories(slack_user_id: str, limit: int = 10) -> list[str]:
    """Return a user's most recent stored facts (newest first)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fact FROM ask_buddy_user_memory WHERE slack_user_id = %s "
                "ORDER BY created_at DESC LIMIT %s;",
                (slack_user_id, limit),
            )
            return [r["fact"] for r in cur.fetchall()]


def clear_user_memory(slack_user_id: str) -> int:
    """Delete all stored facts for a user. Returns the number of rows removed."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ask_buddy_user_memory WHERE slack_user_id = %s;",
                (slack_user_id,),
            )
            return cur.rowcount


# ---------------------------------------------------------------------------
# Nightly eval runs
# ---------------------------------------------------------------------------

def init_eval_runs_schema() -> None:
    """Idempotent: create just the eval-runs table. Safe at bot startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_EVAL_RUNS_TABLE_DDL)


def insert_eval_result(run_id: str, question: str, passed: bool,
                       score: float | None, got_sources: str,
                       expected_srcs: str, judge_notes: str,
                       agent_config: str | None = None) -> None:
    """Record one question's result from a nightly eval run."""
    sql = """
        INSERT INTO ask_buddy_eval_runs
            (run_id, question, passed, score, got_sources, expected_srcs,
             judge_notes, agent_config)
        VALUES
            (%(run_id)s, %(question)s, %(passed)s, %(score)s, %(got_sources)s,
             %(expected_srcs)s, %(judge_notes)s, %(agent_config)s);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "run_id": run_id, "question": question, "passed": passed,
                "score": score, "got_sources": got_sources,
                "expected_srcs": expected_srcs, "judge_notes": judge_notes,
                "agent_config": agent_config,
            })


def get_eval_run_history(limit: int = 10) -> list[dict]:
    """Return per-run pass-rate summaries, newest run first. Used to spot
    regressions across nightly runs."""
    sql = """
        SELECT run_id,
               MIN(run_at)                                    AS run_at,
               COUNT(*)                                       AS total,
               COUNT(*) FILTER (WHERE passed)                 AS passed,
               AVG(score)                                     AS avg_score
        FROM ask_buddy_eval_runs
        GROUP BY run_id
        ORDER BY run_at DESC
        LIMIT %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [dict(r) for r in cur.fetchall()]


def init_reminders_schema() -> None:
    """Idempotent: create just the reminders table. Safe to call at bot startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_REMINDERS_TABLE_DDL)


def init_git_watch_schema() -> None:
    """Idempotent: create just the git-watch table. Safe to call at bot startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_GIT_WATCH_TABLE_DDL)


def get_git_watermark(repo: str) -> dict:
    """Return {'last_issue_number', 'last_pr_number'} for a repo.
    Returns -1/-1 when the repo has never been polled (sentinel for first-sight)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_issue_number, last_pr_number "
                "FROM ask_buddy_git_watch WHERE repo = %s;", (repo,))
            row = cur.fetchone()
            if row is None:
                return {"last_issue_number": -1, "last_pr_number": -1}
            return {"last_issue_number": row["last_issue_number"],
                    "last_pr_number": row["last_pr_number"]}


def set_git_watermark(repo: str, last_issue_number: int, last_pr_number: int) -> None:
    """Upsert the high-water marks + last_polled_at=now() for a repo."""
    sql = """
        INSERT INTO ask_buddy_git_watch
            (repo, last_issue_number, last_pr_number, last_polled_at)
        VALUES (%(repo)s, %(iss)s, %(pr)s, now())
        ON CONFLICT (repo) DO UPDATE
            SET last_issue_number = EXCLUDED.last_issue_number,
                last_pr_number    = EXCLUDED.last_pr_number,
                last_polled_at    = now();
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"repo": repo, "iss": last_issue_number, "pr": last_pr_number})


def get_ingested_hashes(corpus: str = "hr") -> dict[str, str]:
    """Return {source_filename: content_hash} for previously ingested files in a corpus."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_filename, content_hash FROM hr_ingested_files WHERE corpus = %s;",
                (corpus,),
            )
            return {r["source_filename"]: r["content_hash"] for r in cur.fetchall()}


def upsert_ingested_file(source_filename: str, content_hash: str, chunk_count: int,
                         corpus: str = "hr") -> None:
    """Record (or update) the content hash + chunk count for a source file."""
    sql = """
        INSERT INTO hr_ingested_files (source_filename, corpus, content_hash, chunk_count, ingested_at)
        VALUES (%(source_filename)s, %(corpus)s, %(content_hash)s, %(chunk_count)s, now())
        ON CONFLICT (source_filename, corpus) DO UPDATE
            SET content_hash = EXCLUDED.content_hash,
                chunk_count  = EXCLUDED.chunk_count,
                ingested_at  = now();
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "source_filename": source_filename,
                "corpus": corpus,
                "content_hash": content_hash,
                "chunk_count": chunk_count,
            })


def delete_chunks_for_file(source_filename: str, corpus: str = "hr") -> None:
    """Remove all hr_chunks rows for a given source file in a corpus."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM hr_chunks WHERE source_filename = %s AND corpus = %s;",
                (source_filename, corpus),
            )


def delete_ingested_file_record(source_filename: str, corpus: str = "hr") -> None:
    """Remove the tracking row for a source file in a corpus."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM hr_ingested_files WHERE source_filename = %s AND corpus = %s;",
                (source_filename, corpus),
            )


def init_feedback_schema() -> None:
    """
    Idempotent: create (and migrate) only the feedback table.
    Safe to call at bot startup — runs the same ADD COLUMN IF NOT EXISTS
    migration so an existing table gains the new columns automatically.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_FEEDBACK_TABLE_DDL)


def get_chunk_quality() -> dict[int, dict[str, int]]:
    """
    Return per-chunk feedback tallies keyed by hr_chunks.id:

        {chunk_id: {"positive": int, "negative": int, "net": int}}

    A chunk's counts sum the sentiment of every rated answer that cited it
    (a chunk can appear in many answers). Used to bias retrieval ranking and
    to surface low-quality source text for human review. Refusals are ignored
    since they cite no chunks.
    """
    sql = """
        SELECT chunk_id,
               COUNT(*) FILTER (WHERE feedback = 'positive') AS positive,
               COUNT(*) FILTER (WHERE feedback = 'negative') AS negative
        FROM ask_buddy_feedback,
             LATERAL unnest(retrieved_chunk_ids) AS chunk_id
        WHERE feedback IS NOT NULL
          AND retrieved_chunk_ids IS NOT NULL
        GROUP BY chunk_id;
    """
    quality: dict[int, dict[str, int]] = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for r in cur.fetchall():
                pos = int(r["positive"])
                neg = int(r["negative"])
                quality[int(r["chunk_id"])] = {
                    "positive": pos,
                    "negative": neg,
                    "net": pos - neg,
                }
    return quality


def get_low_quality_chunks(min_negative: int = 3) -> list[dict]:
    """
    Return chunks that have accumulated >= min_negative thumbs-down across
    the answers that cited them, joined to their source text for review.

    Each dict: chunk_id, source_filename, section, chunk_text, positive,
    negative, net — ordered worst (most negative) first. This is the
    content-review queue: the underlying HR doc text may be wrong/ambiguous.
    """
    sql = """
        SELECT c.id                AS chunk_id,
               c.source_filename,
               c.section,
               c.chunk_text,
               q.positive,
               q.negative,
               q.positive - q.negative AS net
        FROM (
            SELECT chunk_id,
                   COUNT(*) FILTER (WHERE feedback = 'positive') AS positive,
                   COUNT(*) FILTER (WHERE feedback = 'negative') AS negative
            FROM ask_buddy_feedback,
                 LATERAL unnest(retrieved_chunk_ids) AS chunk_id
            WHERE feedback IS NOT NULL
              AND retrieved_chunk_ids IS NOT NULL
            GROUP BY chunk_id
        ) q
        JOIN hr_chunks c ON c.id = q.chunk_id
        WHERE q.negative >= %(min_negative)s
        ORDER BY q.negative DESC, net ASC;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"min_negative": min_negative})
            return [dict(r) for r in cur.fetchall()]


def get_positive_examples(limit: int = 3) -> list[dict]:
    """
    Return the most-liked distinct (question, answer_text) pairs for use as
    few-shot examples in the system prompt. Excludes refusals and answers
    with no Source(s) line so only well-formed exemplars are surfaced.
    """
    sql = """
        SELECT DISTINCT ON (question) question, answer_text,
               COUNT(*) OVER (PARTITION BY question) AS likes
        FROM ask_buddy_feedback
        WHERE feedback = 'positive'
          AND is_refusal = FALSE
          AND answer_text ILIKE '%%Source(s):%%'
        ORDER BY question, likes DESC
        LIMIT %(limit)s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"limit": limit})
            return [dict(r) for r in cur.fetchall()]


def get_feedback_summary() -> dict:
    """Overall feedback counts for the App Home dashboard."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)                                      AS total,
                       COUNT(*) FILTER (WHERE feedback = 'positive')  AS positive,
                       COUNT(*) FILTER (WHERE feedback = 'negative')  AS negative,
                       COUNT(*) FILTER (WHERE feedback IS NULL)       AS unrated
                FROM ask_buddy_feedback;
            """)
            r = dict(cur.fetchone())
            return {k: int(r[k] or 0) for k in ("total", "positive", "negative", "unrated")}


def count_positive_examples() -> int:
    """Number of well-formed positive exemplars available (non-refusal, with a
    Source(s) line). Drives the 'auto' few-shot count so the prompt grows as
    the bot accumulates good answers."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT question) AS n FROM ask_buddy_feedback "
                "WHERE feedback = 'positive' AND is_refusal = FALSE "
                "AND answer_text ILIKE '%%Source(s):%%';"
            )
            return int(cur.fetchone()["n"] or 0)


def get_config_quality() -> list[dict]:
    """
    Per-agent-config quality: rated count and negative rate. Used to auto-
    recommend the best-performing LLM/agent config. Only configs with a
    meaningful sample are actionable, so the caller applies a min-sample gate.
    """
    sql = """
        SELECT COALESCE(agent_config, '(untagged)')         AS config,
               COUNT(*) FILTER (WHERE feedback IS NOT NULL)  AS rated,
               COUNT(*) FILTER (WHERE feedback = 'negative') AS negative,
               COUNT(*) FILTER (WHERE feedback = 'positive') AS positive
        FROM ask_buddy_feedback
        GROUP BY config
        ORDER BY rated DESC;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = []
            for r in cur.fetchall():
                rated = int(r["rated"] or 0)
                neg = int(r["negative"] or 0)
                rows.append({
                    "config": r["config"],
                    "rated": rated,
                    "negative": neg,
                    "positive": int(r["positive"] or 0),
                    "negative_rate": (neg / rated) if rated else 0.0,
                })
            return rows


def get_negative_feedback_rows(since_days: int | None = None) -> list[dict]:
    """
    Return negatively-rated rows (question, answer, reason, sources, refusal,
    created_at), optionally limited to the last `since_days`. Used by the
    report clustering / eval export and the weekly digest.
    """
    where = "feedback = 'negative'"
    params: dict = {}
    if since_days is not None:
        where += " AND created_at >= now() - (%(since_days)s || ' days')::interval"
        params["since_days"] = since_days
    sql = f"""
        SELECT question, answer_text, feedback_reason, sources_cited,
               is_refusal, created_at
        FROM ask_buddy_feedback
        WHERE {where}
        ORDER BY created_at DESC;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def get_random_chunk_spotlight(corpus: str | None = None) -> dict | None:
    """Return a random chunk (source_filename, section, chunk_text) for the
    weekly 'policy spotlight' DM. Returns None if the corpus is empty."""
    sql = "SELECT source_filename, section, chunk_text FROM hr_chunks"
    params: tuple = ()
    if corpus:
        sql += " WHERE corpus = %s"
        params = (corpus,)
    sql += " ORDER BY random() LIMIT 1;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def clear_chunks(corpus: str | None = None) -> None:
    """Clear chunks, optionally scoped to a single corpus."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if corpus:
                cur.execute("DELETE FROM hr_chunks WHERE corpus = %s;", (corpus,))
                print(f"hr_chunks cleared for corpus '{corpus}'.")
            else:
                cur.execute("TRUNCATE TABLE hr_chunks RESTART IDENTITY;")
                print("hr_chunks table cleared.")


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def insert_reminder(channel_id: str, channel_name: str, message: str,
                    cron_expression: str, timezone: str,
                    created_by: str | None = None) -> int:
    """Insert a new active reminder and return its id."""
    sql = """
        INSERT INTO ask_buddy_reminders
            (channel_id, channel_name, message, cron_expression, timezone, created_by)
        VALUES (%(channel_id)s, %(channel_name)s, %(message)s,
                %(cron_expression)s, %(timezone)s, %(created_by)s)
        RETURNING id;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "message": message,
                "cron_expression": cron_expression,
                "timezone": timezone,
                "created_by": created_by,
            })
            return cur.fetchone()["id"]


def get_active_reminders() -> list[dict]:
    """Return all active reminders (used to reload the scheduler on boot)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, channel_id, channel_name, message, cron_expression, "
                "timezone, created_by, created_at "
                "FROM ask_buddy_reminders WHERE active = TRUE ORDER BY id;"
            )
            return [dict(r) for r in cur.fetchall()]


def list_reminders_for_channel(channel_id: str | None = None) -> list[dict]:
    """Return active reminders, optionally scoped to one channel."""
    sql = ("SELECT id, channel_id, channel_name, message, cron_expression, "
           "timezone, created_by, created_at FROM ask_buddy_reminders "
           "WHERE active = TRUE")
    params: dict = {}
    if channel_id:
        sql += " AND channel_id = %(channel_id)s"
        params["channel_id"] = channel_id
    sql += " ORDER BY id;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def deactivate_reminder(reminder_id: int) -> bool:
    """Mark a reminder inactive. Returns True if a row was updated."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ask_buddy_reminders SET active = FALSE WHERE id = %s AND active = TRUE;",
                (reminder_id,),
            )
            return cur.rowcount > 0
