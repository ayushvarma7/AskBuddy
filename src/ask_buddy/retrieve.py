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

from langchain_core.tools import tool
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pydantic import SecretStr

from .corpora import CORPORA, Corpus
from .db import get_conn, get_chunk_quality

EMBED_MODEL = "models/gemini-embedding-001"
RRF_K = 60          # standard constant in RRF: score = 1 / (K + rank)
VECTOR_POOL = 20    # candidates fetched from vector search before RRF merge
BM25_POOL = 20      # candidates fetched from keyword search before RRF merge

# Feedback-informed re-ranking: each net thumbs vote a chunk has accumulated
# nudges its merged score by QUALITY_ALPHA, clamped to ±QUALITY_MAX so a
# pile-on can't bury an otherwise-relevant chunk or float an irrelevant one.
QUALITY_ALPHA = 0.05   # ±5% per net vote
QUALITY_MAX = 0.30     # ±30% ceiling


# Module-level embedder singleton — created once on first use, reused for
# every subsequent retrieve call.  Avoids spinning up a new HTTP client per
# message.  Protected by a simple check-and-set (the GIL makes this safe for
# CPython threads without an explicit lock).
_embedder: Any | None = None


def _get_embedder() -> Any:
    global _embedder
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is not set.")
    if _embedder is None:
        _embedder = GoogleGenerativeAIEmbeddings(
            model=EMBED_MODEL,
            # 'api_key' is the field's declared alias, and it is typed
            # SecretStr — so the key never lands in a repr or traceback.
            api_key=SecretStr(key),
            output_dimensionality=768,     # match ingest dimensions
        )
    return _embedder


def _embed_query(query: str) -> list[float]:
    return _get_embedder().embed_query(query)


def _vector_search(embedding: list[float], pool: int,
                    corpus: str | None = None) -> list[dict]:
    """Return up to `pool` chunks ordered by cosine similarity."""
    where = "WHERE corpus = %s" if corpus else ""
    sql = f"""
        SELECT
            id,
            source_filename,
            section,
            chunk_text,
            effective_date,
            1 - (embedding <=> %s::vector) AS score
        FROM hr_chunks
        {where}
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
    """
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
    params: tuple = (emb_str, emb_str, pool) if not corpus else (emb_str, corpus, emb_str, pool)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _keyword_search(query: str, pool: int,
                    corpus: str | None = None) -> list[dict]:
    """
    Return up to `pool` chunks ordered by Postgres full-text rank.

    Terms are combined with OR, not AND. This matters more than it looks:
    plainto_tsquery ANDs every lexeme, so "How many days of PTO do I get after
    5 years of service?" becomes
    'mani' & 'day' & 'pto' & 'get' & '5' & 'year' & 'servic'
    and matches *nothing* — no single chunk contains all of those. Since that is
    exactly the shape of question Slack sends, the keyword half of this hybrid
    search was contributing zero results for real queries, quietly making
    retrieval vector-only.

    ORing the lexemes restores recall, and ts_rank_cd still supplies precision:
    a chunk matching six terms outranks one matching two. RRF then does its job
    with two genuinely independent signals.

    The coalesce/nullif guard covers a query made entirely of stopwords, where
    the lexeme list is empty and to_tsquery('') would raise a syntax error.
    """
    corpus_filter = "AND corpus = %s" if corpus else ""
    sql = f"""
        SELECT
            id,
            source_filename,
            section,
            chunk_text,
            effective_date,
            ts_rank_cd(bm25_tsvector, query) AS score
        FROM hr_chunks,
             to_tsquery(
                 'english',
                 coalesce(
                     nullif(
                         array_to_string(
                             tsvector_to_array(to_tsvector('english', %s)),
                             ' | '
                         ),
                         ''
                     ),
                     'zzzznomatchzzzz'
                 )
             ) query
        WHERE bm25_tsvector @@ query
        {corpus_filter}
        ORDER BY score DESC
        LIMIT %s;
    """
    params: tuple = (query, pool) if not corpus else (query, corpus, pool)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _quality_multiplier(net_votes: int) -> float:
    """Map a chunk's net feedback votes to a bounded score multiplier."""
    adjustment = max(-QUALITY_MAX, min(QUALITY_MAX, net_votes * QUALITY_ALPHA))
    return 1.0 + adjustment


def _rrf_merge(
    vector_results: list[dict],
    keyword_results: list[dict],
    top_k: int,
    quality: dict[int, dict[str, int]] | None = None,
) -> list[dict]:
    """
    Merge two ranked lists via Reciprocal Rank Fusion, then optionally
    re-weight by accumulated user feedback.

    score(doc)   = Σ  1 / (K + rank_i)   for each list the doc appears in
    adjusted(doc) = score(doc) * (1 + clamp(net_votes * ALPHA, ±MAX))

    `quality` maps chunk_id -> {"net": int, ...} (see db.get_chunk_quality).
    When omitted, this is plain RRF — keeping the function pure and testable.
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

    # Apply feedback-informed re-weighting to the merged scores.
    adjusted: dict[int, float] = {}
    for doc_id, base in scores.items():
        net = (quality or {}).get(doc_id, {}).get("net", 0)
        adjusted[doc_id] = base * _quality_multiplier(net)

    merged = sorted(adjusted.keys(), key=lambda doc_id: adjusted[doc_id], reverse=True)

    results = []
    for doc_id in merged[:top_k]:
        row = dict(by_id[doc_id])
        row["rrf_score"] = round(scores[doc_id], 6)
        net = (quality or {}).get(doc_id, {}).get("net", 0)
        if net:
            # Surface the feedback adjustment for transparency/debugging.
            row["quality_net"] = net
            row["adjusted_score"] = round(adjusted[doc_id], 6)
        # Stringify effective_date for JSON serialisation
        if isinstance(row.get("effective_date"), date):
            row["effective_date"] = row["effective_date"].isoformat()
        results.append(row)

    return results


def _hybrid_retrieve_core(query: str, top_k: int = 5,
                          corpus: str | None = None) -> list[dict[str, Any]]:
    """Shared retrieval logic, optionally scoped to a corpus."""
    try:
        embedding = _embed_query(query)
        vector_results = _vector_search(embedding, pool=VECTOR_POOL, corpus=corpus)
        keyword_results = _keyword_search(query, pool=BM25_POOL, corpus=corpus)
        try:
            quality = get_chunk_quality()
        except Exception:
            quality = None
        merged = _rrf_merge(vector_results, keyword_results, top_k=top_k,
                            quality=quality)
    except Exception as exc:
        return [{"error": str(exc)}]
    return merged


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
    return _hybrid_retrieve_core(query, top_k, corpus="hr")


# ---------------------------------------------------------------------------
# Per-corpus retrieval tools, built from the registry
# ---------------------------------------------------------------------------
#
# One @tool per entry in corpora.CORPORA, so adding a domain needs no edit
# here. The docstring matters: it is what the LLM reads to decide whether the
# tool is the right one to call, so the corpus's topic list goes into it.


def _build_retrieve_tool(corpus: Corpus) -> Any:
    """Create the corpus-scoped retrieval @tool for one domain."""

    def _retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
        return _hybrid_retrieve_core(query, top_k, corpus=corpus.name)

    _retrieve.__name__ = corpus.tool_name
    _retrieve.__doc__ = (
        f"Retrieve {corpus.label} policy chunks ({corpus.topics}) for `query`.\n\n"
        "Returns a list of dicts with chunk_text, source_filename, section, "
        "effective_date, and rrf_score. Always cite source_filename, section, "
        "and effective_date in your answer."
    )
    return tool(_retrieve)


#: corpus name -> its retrieval @tool
RETRIEVE_TOOLS: dict[str, Any] = {
    corpus.name: _build_retrieve_tool(corpus) for corpus in CORPORA
}


def retrieve_tool_for(corpus_name: str) -> Any:
    """The retrieval tool for one corpus."""
    return RETRIEVE_TOOLS[corpus_name]


# Named bindings for the two corpora that shipped first. Tests and any external
# callers import these directly, and the supervisor prompt names them.
hr_retrieve = RETRIEVE_TOOLS["hr"]
it_retrieve = RETRIEVE_TOOLS["it"]
