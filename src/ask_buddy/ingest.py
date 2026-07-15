"""
Ingestion pipeline for Ask Buddy.

Usage:
    python -m src.ask_buddy.ingest [--docs-dir PATH] [--corpus NAME] [--clear]

Reads all *.md files under docs-dir, chunks them, embeds them with
Google gemini-embedding-001, and upserts into Postgres/pgvector.

Each chunk is tagged with a corpus name (default "hr") so multiple
document domains (HR, IT, …) share one table with scoped retrieval.

Incremental by default: each file's content hash is tracked in
hr_ingested_files, so a plain re-run skips files that haven't changed,
only re-embeds new/modified files, and removes chunks for files that
were deleted from docs-dir. Pass --clear to force a full rebuild.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterator

import psycopg2.extras
from dotenv import load_dotenv
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from .db import (
    get_conn,
    init_schema,
    clear_chunks,
    get_ingested_hashes,
    upsert_ingested_file,
    delete_chunks_for_file,
    delete_ingested_file_record,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HR_DOCS_DIR = Path(__file__).resolve().parents[2] / "data" / "hr_docs" / "synthetic"
IT_DOCS_DIR = Path(__file__).resolve().parents[2] / "data" / "it_docs"
DOCS_DIR = HR_DOCS_DIR
CHUNK_TOKENS_TARGET = 500          # rough token target per chunk
CHUNK_OVERLAP_CHARS = 200          # character overlap between adjacent chunks
EMBED_MODEL = "models/gemini-embedding-001"
EMBED_BATCH = 32                   # chunks per API call
WORDS_PER_TOKEN = 0.75             # conservative estimate for splitting


def _embedder() -> GoogleGenerativeAIEmbeddings:
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is not set.")
    return GoogleGenerativeAIEmbeddings(
        model=EMBED_MODEL,
        google_api_key=key,
        output_dimensionality=768,     # truncate 3072→768; stays within pgvector index limit
    )


# ---------------------------------------------------------------------------
# Markdown parsing helpers
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_DATE_RE = re.compile(
    r"\*\*Effective Date:\*\*\s*(\d{4}-\d{2}-\d{2})"
)


def _parse_effective_date(text: str) -> date | None:
    """Extract the first effective date found in the document header area."""
    m = _DATE_RE.search(text[:500])
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """
    Split markdown text into (section_heading, section_body) pairs.
    The document preamble (before the first heading) uses the document
    title (first # heading) as its section label.
    """
    headings = list(_HEADING_RE.finditer(text))
    if not headings:
        return [("", text)]

    sections: list[tuple[str, str]] = []

    # Preamble before first heading
    preamble = text[: headings[0].start()].strip()
    if preamble:
        sections.append((headings[0].group(2), preamble))

    for i, match in enumerate(headings):
        heading_text = match.group(2)
        body_start = match.end()
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            sections.append((heading_text, body))

    return sections


def _chunk_text(text: str, target_tokens: int = CHUNK_TOKENS_TARGET,
                overlap_chars: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """
    Split text into overlapping chunks by approximate token count.
    Uses character-based splitting (WORDS_PER_TOKEN heuristic).
    """
    target_chars = int(target_tokens / WORDS_PER_TOKEN * 5)  # ~5 chars/word
    if len(text) <= target_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + target_chars
        if end < len(text):
            # Try to break on a paragraph or sentence boundary
            for sep in ("\n\n", ". ", " "):
                pos = text.rfind(sep, start + target_chars // 2, end)
                if pos != -1:
                    end = pos + len(sep)
                    break
        chunks.append(text[start:end].strip())
        start = end - overlap_chars
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed_batch(embedder: GoogleGenerativeAIEmbeddings, texts: list[str]) -> list[list[float]]:
    return embedder.embed_documents(texts)


# ---------------------------------------------------------------------------
# Database write
# ---------------------------------------------------------------------------

def _insert_chunks(rows: list[dict]) -> None:
    sql = """
        INSERT INTO hr_chunks
            (source_filename, section, chunk_text, embedding, effective_date, corpus)
        VALUES
            (%(source_filename)s, %(section)s, %(chunk_text)s,
             %(embedding)s::vector, %(effective_date)s, %(corpus)s)
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=50)


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------

def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ingest_docs(docs_dir: Path, clear: bool = False, corpus: str = "hr") -> None:
    init_schema()
    if clear:
        clear_chunks(corpus=corpus)

    md_files = sorted(docs_dir.glob("*.md"))
    if not md_files:
        print(f"No markdown files found in {docs_dir}", file=sys.stderr)
        sys.exit(1)

    previous_hashes = {} if clear else get_ingested_hashes(corpus=corpus)

    on_disk_filenames = {md_path.name for md_path in md_files}

    for stale_filename in set(previous_hashes) - on_disk_filenames:
        print(f"  {stale_filename}: removed from {docs_dir}, deleting its chunks")
        delete_chunks_for_file(stale_filename, corpus=corpus)
        delete_ingested_file_record(stale_filename, corpus=corpus)

    files_to_process: list[tuple[Path, str]] = []
    skipped = 0
    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        content_hash = _file_hash(text)
        if not clear and previous_hashes.get(md_path.name) == content_hash:
            print(f"  {md_path.name}: unchanged, skipping")
            skipped += 1
            continue
        files_to_process.append((md_path, content_hash))

    if not files_to_process:
        print(f"\nNothing to do — all {len(md_files)} file(s) unchanged since last ingest.")
        return

    embedder = _embedder()
    all_rows: list[dict] = []
    chunk_counts: dict[str, int] = {}
    file_hashes: dict[str, str] = {}

    for md_path, content_hash in files_to_process:
        text = md_path.read_text(encoding="utf-8")
        effective_date = _parse_effective_date(text)
        sections = _split_into_sections(text)

        print(f"  {md_path.name}: {len(sections)} sections, "
              f"effective_date={effective_date}")

        file_rows = []
        for section_heading, section_body in sections:
            for chunk in _chunk_text(section_body):
                file_rows.append({
                    "source_filename": md_path.name,
                    "section": section_heading,
                    "chunk_text": chunk,
                    "embedding": None,
                    "effective_date": effective_date,
                    "corpus": corpus,
                })

        # A previously-ingested file that changed must have its old chunks
        # removed before the new ones are inserted, or stale + fresh chunks
        # would both be retrievable at once.
        if md_path.name in previous_hashes:
            delete_chunks_for_file(md_path.name, corpus=corpus)

        chunk_counts[md_path.name] = len(file_rows)
        file_hashes[md_path.name] = content_hash
        all_rows.extend(file_rows)

    print(f"\nEmbedding {len(all_rows)} chunks in batches of {EMBED_BATCH}…")

    # Embed in batches
    texts = [r["chunk_text"] for r in all_rows]
    for i in range(0, len(texts), EMBED_BATCH):
        batch_texts = texts[i : i + EMBED_BATCH]
        embeddings = _embed_batch(embedder, batch_texts)
        for j, emb in enumerate(embeddings):
            all_rows[i + j]["embedding"] = emb
        print(f"  embedded {min(i + EMBED_BATCH, len(texts))}/{len(texts)}")

    print("Inserting into database…")
    _insert_chunks(all_rows)

    for filename, content_hash in file_hashes.items():
        upsert_ingested_file(filename, content_hash, chunk_counts[filename],
                             corpus=corpus)

    print(
        f"Done. {len(all_rows)} chunks stored across {len(files_to_process)} file(s) "
        f"({skipped} unchanged file(s) skipped)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest docs into pgvector")
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=None,
        help="Directory containing *.md files (default: auto from --corpus)",
    )
    parser.add_argument(
        "--corpus",
        type=str,
        default="hr",
        help="Corpus tag stored on each chunk (hr, it, …). Default: hr",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help=(
            "Delete all chunks for this corpus and force a full re-embed, "
            "ignoring content hashes. Without this flag, unchanged files "
            "are skipped."
        ),
    )
    args = parser.parse_args()
    if args.docs_dir is None:
        defaults = {"hr": HR_DOCS_DIR, "it": IT_DOCS_DIR}
        args.docs_dir = defaults.get(args.corpus, HR_DOCS_DIR)
    ingest_docs(args.docs_dir, clear=args.clear, corpus=args.corpus)


if __name__ == "__main__":
    main()
