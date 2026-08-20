"""
Deterministic citation validation for Ask Buddy answers.

Every agent prompt ends with a hard rule: never fabricate a filename,
section name, or date that wasn't in the retrieved chunk metadata. Nothing
verified that — until this module. Here we parse the ``Source(s):`` block
back out of the posted answer and check every citation against the source
metadata that actually exists in the corpus.

This is intentionally dependency-free (stdlib only, no DB, no LLM) so it can
be unit-tested offline and called on the hot path without cost. The caller
supplies the set of known (filename, section) pairs — see
``db.get_known_sources()``.

Verdicts, worst-first:

    missing_sources   a non-refusal answer had no Source(s): line at all
                      (the prompt calls this INVALID and it must never ship)
    unknown_file      a cited filename does not exist in the corpus
                      — i.e. the model invented a document
    unknown_section   the file exists but the cited section does not
                      — the model invented a heading
    ok                every citation resolves to real corpus metadata
    refusal           a "No results found" answer; citations not applicable
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Worst-first: the order used to collapse per-citation problems into one
# answer-level verdict, and the order the report should read in.
STATUS_REFUSAL = "refusal"
STATUS_OK = "ok"
STATUS_MISSING_SOURCES = "missing_sources"
STATUS_UNKNOWN_FILE = "unknown_file"
STATUS_UNKNOWN_SECTION = "unknown_section"

_SEVERITY = {
    STATUS_MISSING_SOURCES: 4,
    STATUS_UNKNOWN_FILE: 3,
    STATUS_UNKNOWN_SECTION: 2,
    STATUS_OK: 1,
    STATUS_REFUSAL: 0,
}

# A citation line looks like one of:
#     pto_policy.md — 2. PTO Accrual Rates (effective 2024-01-01)
#     pto_policy.md - PTO Accrual Rates
#     pto_policy.md
# The em dash is what the prompt asks for, but models drift to -/–, so accept
# any of them. The trailing "(effective ...)" is optional per the prompt.
_CITATION_RE = re.compile(
    r"""
    ^\s*
    (?P<file>\S+\.[A-Za-z0-9]+)          # a filename with an extension
    (?:\s*[—–-]+\s*(?P<section>.*?))?    # optional " — section"
    (?:\s*\(\s*effective\s+(?P<date>\d{4}-\d{2}-\d{2})\s*\)\s*)?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Matches "Source(s):", "Sources:" and "Source:" — the prompt asks for the
# first, but accept the drift rather than silently dropping a real citation.
_SOURCES_PREFIX_RE = re.compile(r"^\s*source(?:\(s\)|s)?\s*:", re.IGNORECASE)


@dataclass(frozen=True)
class Citation:
    """One parsed entry from an answer's Source(s) block."""
    filename: str
    section: str | None = None
    effective_date: str | None = None
    raw: str = ""


@dataclass
class CitationVerdict:
    """Answer-level result of validating every citation in one answer."""
    status: str
    citations: list[Citation] = field(default_factory=list)
    problems: list[tuple[Citation, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_OK, STATUS_REFUSAL)

    def summary(self) -> str:
        """One-line human-readable description, for logs."""
        if self.status == STATUS_REFUSAL:
            return "refusal — no citations expected"
        if self.status == STATUS_OK:
            return f"ok — {len(self.citations)} citation(s) verified"
        if self.status == STATUS_MISSING_SOURCES:
            return "no Source(s) line on a non-refusal answer"
        details = "; ".join(
            f"{c.filename}"
            + (f" — {c.section}" if c.section else "")
            + f" ({problem})"
            for c, problem in self.problems
        )
        return f"{self.status} — {details}"


def _normalize(value: str) -> str:
    """Collapse whitespace and casefold, so trivial formatting drift in a
    section heading isn't reported as a fabrication."""
    return re.sub(r"\s+", " ", value).strip().casefold()


def _section_matches(cited: str, known_sections: set[str]) -> bool:
    """
    Is `cited` a plausible reference to one of `known_sections`?

    Exact match after normalisation, or either string containing the other.
    Containment is deliberate: models routinely drop or add a heading's
    leading number ("2. PTO Accrual Rates" vs "PTO Accrual Rates"), and
    flagging that as a fabricated section would bury the real signal in
    false positives. An invented heading still shares no text with any real
    one, so it is still caught.
    """
    if cited in known_sections:
        return True
    return any(cited in known or known in cited
               for known in known_sections if known)


def parse_source_lines(answer_text: str) -> list[Citation]:
    """
    Extract citations from an answer's ``Source(s):`` block.

    Collection starts at the first line beginning with ``Source(s):`` and
    continues through following non-blank lines, matching how the prompt
    asks for multiple sources ("one per line"). Lines that don't look like
    a citation are skipped rather than guessed at.
    """
    citations: list[Citation] = []
    collecting = False

    for line in answer_text.splitlines():
        stripped = line.strip()
        is_header = bool(_SOURCES_PREFIX_RE.match(stripped))

        if not collecting and not is_header:
            continue
        if collecting and not stripped:
            break            # blank line ends the block

        collecting = True
        entry = _SOURCES_PREFIX_RE.sub("", stripped, count=1).strip() if is_header else stripped
        if not entry:
            continue

        match = _CITATION_RE.match(entry)
        if not match:
            continue
        section = (match.group("section") or "").strip() or None
        citations.append(Citation(
            filename=match.group("file"),
            section=section,
            effective_date=match.group("date"),
            raw=entry,
        ))

    return citations


def has_sources_line(answer_text: str) -> bool:
    """True when the answer contains a ``Source(s):`` header at all."""
    return any(_SOURCES_PREFIX_RE.match(line.strip())
               for line in answer_text.splitlines())


def cites_documents(answer_text: str, is_refusal: bool = False) -> bool:
    """
    Did this answer draw on the document corpus at all?

    Used to decide whether retrieved chunk IDs should be attributed to an
    answer. Only HR/IT answers carry a ``Source(s):`` line — a GitHub reply, a
    reminder confirmation, or a clarifying question does not. Attributing
    chunks to those would let a 👎 on "merge PR #17" penalise unrelated policy
    chunks in the retrieval re-ranker.

    Refusals cite nothing by design and are excluded.
    """
    if is_refusal:
        return False
    return bool(parse_source_lines(answer_text))


def validate_citations(
    answer_text: str,
    known_sources: set[tuple[str, str]],
    is_refusal: bool = False,
) -> CitationVerdict:
    """
    Check every citation in `answer_text` against `known_sources`.

    `known_sources` is a set of (source_filename, section) pairs drawn from
    the corpus — see ``db.get_known_sources()``. Comparison is
    whitespace- and case-insensitive so cosmetic drift isn't flagged as a
    fabricated citation.

    Refusals short-circuit: they cite nothing by design.
    """
    if is_refusal:
        return CitationVerdict(status=STATUS_REFUSAL)

    citations = parse_source_lines(answer_text)
    if not citations:
        return CitationVerdict(status=STATUS_MISSING_SOURCES)

    sections_by_file: dict[str, set[str]] = {}
    for filename, section in known_sources:
        sections_by_file.setdefault(_normalize(filename), set()).add(_normalize(section))

    problems: list[tuple[Citation, str]] = []
    for citation in citations:
        file_key = _normalize(citation.filename)
        known_sections = sections_by_file.get(file_key)
        if known_sections is None:
            problems.append((citation, STATUS_UNKNOWN_FILE))
            continue
        # A citation with no section can't name a section that doesn't exist.
        if citation.section is None:
            continue
        if not _section_matches(_normalize(citation.section), known_sections):
            problems.append((citation, STATUS_UNKNOWN_SECTION))

    if not problems:
        return CitationVerdict(status=STATUS_OK, citations=citations)

    worst = max((p for _, p in problems), key=lambda s: _SEVERITY[s])
    return CitationVerdict(status=worst, citations=citations, problems=problems)
