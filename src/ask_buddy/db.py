"""Database connection and schema management for Ask Buddy."""

from __future__ import annotations

import os
import psycopg2
import psycopg2.extras
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
_GIT_WATCH_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_git_watch (
        repo               TEXT        PRIMARY KEY,   -- 'owner/name'
        last_issue_number  INTEGER     NOT NULL DEFAULT 0,
        last_pr_number     INTEGER     NOT NULL DEFAULT 0,
        last_polled_at     TIMESTAMPTZ
    );
"""


def _dsn() -> str:
    dsn = os.environ.get("ASK_BUDDY_DB_DSN")
    if not dsn:
        raise RuntimeError(
            "ASK_BUDDY_DB_DSN environment variable is not set. "
            "Example: postgresql://askbuddy:secret@localhost:5432/askbuddy"
        )
    return dsn


@contextmanager
def get_conn():
    """Yield a psycopg2 connection with autocommit disabled."""
    conn = psycopg2.connect(_dsn(), cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
    print("Schema initialised.")


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
    """Return {'last_issue_number', 'last_pr_number'} for a repo (0/0 if new)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_issue_number, last_pr_number "
                "FROM ask_buddy_git_watch WHERE repo = %s;", (repo,))
            row = cur.fetchone()
            if row is None:
                return {"last_issue_number": 0, "last_pr_number": 0}
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
