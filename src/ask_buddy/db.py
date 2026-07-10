"""Database connection and schema management for Ask Buddy."""

from __future__ import annotations

import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager


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
        embedding        vector(768),            -- Google gemini-embedding-001 truncated to 768
        effective_date   DATE,
        bm25_tsvector    tsvector
            GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED
    );

    CREATE INDEX IF NOT EXISTS hr_chunks_embedding_idx
        ON hr_chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);

    CREATE INDEX IF NOT EXISTS hr_chunks_fts_idx
        ON hr_chunks USING GIN (bm25_tsvector);

    -- Feedback table: one row per thumbs-up / thumbs-down interaction
    CREATE TABLE IF NOT EXISTS ask_buddy_feedback (
        id              SERIAL PRIMARY KEY,
        response_id     TEXT        NOT NULL UNIQUE,   -- uuid, ties buttons to a response
        question        TEXT        NOT NULL,
        answer_text     TEXT        NOT NULL,
        sources_cited   TEXT        NOT NULL DEFAULT '',
        feedback        TEXT        CHECK (feedback IN ('positive','negative')),
        user_id         TEXT,                          -- Slack user ID who clicked
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS ask_buddy_feedback_response_idx
        ON ask_buddy_feedback (response_id);

    CREATE INDEX IF NOT EXISTS ask_buddy_feedback_feedback_idx
        ON ask_buddy_feedback (feedback);

    -- Tracks the content hash of each ingested source file so re-running
    -- the ingest pipeline can skip files that haven't changed.
    CREATE TABLE IF NOT EXISTS hr_ingested_files (
        source_filename  TEXT        PRIMARY KEY,
        content_hash     TEXT        NOT NULL,
        chunk_count      INTEGER     NOT NULL,
        ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
    print("Schema initialised.")


def get_ingested_hashes() -> dict[str, str]:
    """Return {source_filename: content_hash} for all previously ingested files."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT source_filename, content_hash FROM hr_ingested_files;")
            return {r["source_filename"]: r["content_hash"] for r in cur.fetchall()}


def upsert_ingested_file(source_filename: str, content_hash: str, chunk_count: int) -> None:
    """Record (or update) the content hash + chunk count for a source file."""
    sql = """
        INSERT INTO hr_ingested_files (source_filename, content_hash, chunk_count, ingested_at)
        VALUES (%(source_filename)s, %(content_hash)s, %(chunk_count)s, now())
        ON CONFLICT (source_filename) DO UPDATE
            SET content_hash = EXCLUDED.content_hash,
                chunk_count  = EXCLUDED.chunk_count,
                ingested_at  = now();
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "source_filename": source_filename,
                "content_hash": content_hash,
                "chunk_count": chunk_count,
            })


def delete_chunks_for_file(source_filename: str) -> None:
    """Remove all hr_chunks rows for a given source file (used before re-ingesting it)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM hr_chunks WHERE source_filename = %s;",
                (source_filename,),
            )


def delete_ingested_file_record(source_filename: str) -> None:
    """Remove the tracking row for a source file (used when the file was deleted)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM hr_ingested_files WHERE source_filename = %s;",
                (source_filename,),
            )


def init_feedback_schema() -> None:
    """Idempotent: create only the feedback table (safe to call at bot startup)."""
    ddl = """
    CREATE TABLE IF NOT EXISTS ask_buddy_feedback (
        id              SERIAL PRIMARY KEY,
        response_id     TEXT        NOT NULL UNIQUE,
        question        TEXT        NOT NULL,
        answer_text     TEXT        NOT NULL,
        sources_cited   TEXT        NOT NULL DEFAULT '',
        feedback        TEXT        CHECK (feedback IN ('positive','negative')),
        user_id         TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS ask_buddy_feedback_response_idx
        ON ask_buddy_feedback (response_id);
    CREATE INDEX IF NOT EXISTS ask_buddy_feedback_feedback_idx
        ON ask_buddy_feedback (feedback);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)


def clear_chunks() -> None:
    """Truncate the hr_chunks table (used before a full re-ingest)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE hr_chunks RESTART IDENTITY;")
    print("hr_chunks table cleared.")
