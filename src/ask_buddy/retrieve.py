"""
Hybrid retrieval tool for Ask Buddy.

Combines:
  - Vector similarity search (pgvector cosine)
  - Full-text keyword search (Postgres tsvector / GIN index)
  - Reciprocal Rank Fusion (RRF) merge

Registered as a LangChain @tool so CugaAgent can call it directly.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import psycopg2.extras
from langchain_core.tools import tool
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from .db import get_conn

EMBED_MODEL = "models/gemini-embedding-001"
RRF_K = 60          # standard constant in RRF: score = 1 / (K + rank)
VECTOR_POOL = 20    # candidates fetched from vector search before RRF merge
BM25_POOL = 20      # candidates fetched from keyword search before RRF merge


def _embed_query(query: str) -> list[float]:
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is not set.")
    embedder = GoogleGenerativeAIEmbeddings(
        model=EMBED_MODEL,
        google_api_key=key,
        output_dimensionality=768,     # match ingest dimensions
    )
    return embedder.embed_query(query)


def _vector_search(embedding: list[float], pool: int) -> list[dict]:
    """Return up to `pool` chunks ordered by cosine similarity."""
    sql = """
        SELECT
            id,
            source_filename,
            section,
            chunk_text,
            effective_date,
            1 - (embedding <=> %s::vector) AS score
        FROM hr_chunks
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
    """
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (emb_str, emb_str, pool))
            return [dict(r) for r in cur.fetchall()]


def _keyword_search(query: str, pool: int) -> list[dict]:
    """Return up to `pool` chunks ordered by Postgres full-text rank."""
    sql = """
        SELECT
            id,
            source_filename,
            section,
            chunk_text,
            effective_date,
            ts_rank_cd(bm25_tsvector, query) AS score
        FROM hr_chunks, plainto_tsquery('english', %s) query
        WHERE bm25_tsvector @@ query
        ORDER BY score DESC
        LIMIT %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (query, pool))
            return [dict(r) for r in cur.fetchall()]


def _rrf_merge(
    vector_results: list[dict],
    keyword_results: list[dict],
    top_k: int,
    prefer_latest: bool = True,
) -> list[dict]:
    """
    Merge two ranked lists via Reciprocal Rank Fusion.

    score(doc) = Σ  1 / (K + rank_i)   for each list the doc appears in
    """
    scores: dict[int, float] = {}
    by_id: dict[int, dict] = {}

    for rank, row in enumerate(vector_results, start=1):
        doc_id = row["id"]
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
        by_id[doc_id] = row

    for rank, row in enumerate(keyword_results, start=1):
        doc_id = row["id"]
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
        by_id[doc_id] = row

    merged = sorted(scores.keys(), key=lambda doc_id: scores[doc_id], reverse=True)

    results = []
    for doc_id in merged[:top_k]:
        row = dict(by_id[doc_id])
        row["rrf_score"] = round(scores[doc_id], 6)
        # Stringify effective_date for JSON serialisation
        if isinstance(row.get("effective_date"), date):
            row["effective_date"] = row["effective_date"].isoformat()
        results.append(row)

    if prefer_latest and results:
        # Boost results from the most recent effective_date for ambiguous queries
        # (no re-sorting — just a signal the agent can use via the returned dates)
        pass  # Dates are already in the returned metadata; agent reasons over them.

    return results


@tool
def hybrid_retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """
    Retrieve the most relevant HR policy chunks for `query` using hybrid
    vector + keyword search merged via Reciprocal Rank Fusion.

    Returns a list of dicts, each containing:
      - chunk_text       : the raw policy text
      - source_filename  : the document filename (e.g. 'pto_policy.md')
      - section          : the heading the chunk was extracted from
      - effective_date   : ISO date string or null
      - rrf_score        : merged relevance score (higher is better)

    Use the chunk_text to compose your answer and always cite
    source_filename, section, and effective_date in your response.

    If this returns an empty list, or none of the chunks answer the
    question, call this tool once more with a reformulated query before
    concluding no results are available.
    """
    try:
        embedding = _embed_query(query)
        vector_results = _vector_search(embedding, pool=VECTOR_POOL)
        keyword_results = _keyword_search(query, pool=BM25_POOL)
        merged = _rrf_merge(vector_results, keyword_results, top_k=top_k)
    except Exception as exc:
        # Surface errors clearly to the agent rather than silently returning []
        return [{"error": str(exc)}]

    return merged
