"""
Curation step for Ask Buddy's regression eval suite — the missing middle of
the feedback loop.

The loop is:

    1. Users 👎 an answer in Slack            → ask_buddy_feedback
    2. THIS TOOL: a human says what the       → tests/regression_evals.json
       answer should have cited
    3. TestFeedbackRegressionEvals asserts    → pytest
       retrieval still surfaces that source

Step 2 is what turns a complaint into a permanent guarantee. Without it,
`feedback_report --export-evals` produces candidates that nobody annotates and
the regression test class skips silently forever.

Usage:

    # Show what's waiting to be curated (pulls straight from the DB)
    uv run python -m src.ask_buddy.eval_curate --list

    # Curate interactively — offers the real corpus filenames to pick from
    uv run python -m src.ask_buddy.eval_curate

    # Annotate one case non-interactively (scriptable / CI-friendly)
    uv run python -m src.ask_buddy.eval_curate \\
        --set "how much PTO after 5 years?" --sources pto_policy.md

    # Curate from a previously exported candidates file instead of the DB
    uv run python -m src.ask_buddy.eval_curate --from-file candidates.json

    # Drop a case that isn't worth a regression test
    uv run python -m src.ask_buddy.eval_curate --skip "some vague question"

The output file is committed to the repo. Every curated case becomes a test.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from .corpora import default_corpus

load_dotenv()

# Must match tests/test_ask_buddy.py::_EVAL_FILE — that test is the consumer.
DEFAULT_EVAL_FILE = Path(__file__).resolve().parents[2] / "tests" / "regression_evals.json"


# ---------------------------------------------------------------------------
# Pure case handling — no DB, no I/O, unit-testable
# ---------------------------------------------------------------------------

def normalize_question(question: str) -> str:
    """Key used to dedupe cases across exports and DB pulls."""
    return " ".join(question.split()).strip().casefold()


def is_curated(case: dict) -> bool:
    """A case is only actionable once a human has attached expected_sources.
    Matches the filter in tests/test_ask_buddy.py::_load_curated_evals."""
    return bool(case.get("expected_sources"))


def is_skipped(case: dict) -> bool:
    """Explicitly set aside — kept in the file so it isn't re-proposed."""
    return bool(case.get("skipped"))


def new_case(question: str, was_refusal: bool = False, reason: str | None = None,
             sources_cited: str = "", corpus: str = "hr") -> dict:
    return {
        "question": question.strip(),
        "was_refusal": was_refusal,
        "reason": reason,
        "sources_cited": sources_cited,
        "expected_answer": "",
        "expected_sources": [],
        # Which retrieval tool must surface expected_sources. An IT document is
        # unreachable through hr_retrieve, so the case has to say which corpus
        # it belongs to or the regression test would always fail.
        "corpus": corpus,
    }


def merge_cases(existing: list[dict], candidates: list[dict]) -> list[dict]:
    """
    Fold new candidates into the curated file without losing human work.

    Existing curation always wins — a case that already has expected_sources
    keeps them, and its ordering is preserved so the committed file produces a
    stable diff. Genuinely new questions are appended uncurated.
    """
    merged = list(existing)
    seen = {normalize_question(c["question"]) for c in merged}
    for candidate in candidates:
        key = normalize_question(candidate.get("question", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(candidate)
    return merged


def pending_cases(cases: list[dict]) -> list[dict]:
    """Cases still awaiting a human decision."""
    return [c for c in cases if not is_curated(c) and not is_skipped(c)]


def set_expected_sources(cases: list[dict], question: str, sources: list[str],
                         corpus: str | None = None) -> bool:
    """Attach expected_sources to a case by question. True if a case matched."""
    key = normalize_question(question)
    for case in cases:
        if normalize_question(case.get("question", "")) == key:
            case["expected_sources"] = sources
            if corpus:
                case["corpus"] = corpus
            case.setdefault("corpus", "hr")
            case.pop("skipped", None)
            return True
    return False


def skip_case(cases: list[dict], question: str) -> bool:
    """Mark a case as deliberately not worth a regression test."""
    key = normalize_question(question)
    for case in cases:
        if normalize_question(case.get("question", "")) == key:
            case["skipped"] = True
            case["expected_sources"] = []
            return True
    return False


def parse_sources_arg(raw: str) -> list[str]:
    """'a.md, b.md' or 'a.md b.md' -> ['a.md', 'b.md']."""
    return [part.strip() for part in raw.replace(",", " ").split() if part.strip()]


def load_cases(path: Path) -> list[dict]:
    """Read the curated file, tolerating absence and corruption."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"WARNING: {path} is not valid JSON — starting from an empty set.",
              file=sys.stderr)
        return []
    return data if isinstance(data, list) else []


def write_cases(path: Path, cases: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Candidate sources (DB or exported file)
# ---------------------------------------------------------------------------

def candidates_from_db() -> list[dict]:
    """Pull negatively-rated questions straight from ask_buddy_feedback,
    so the export step is optional."""
    from .db import get_negative_feedback_rows

    rows = get_negative_feedback_rows()
    seen: set[str] = set()
    candidates: list[dict] = []
    for row in rows:
        question = (row["question"] or "").strip()
        key = normalize_question(question)
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(new_case(
            question,
            was_refusal=bool(row["is_refusal"]),
            reason=row["feedback_reason"],
            sources_cited=row["sources_cited"] or "",
        ))
    return candidates


def candidates_from_file(path: Path) -> list[dict]:
    """Read a candidates file produced by `feedback_report --export-evals`."""
    raw = load_cases(path)
    return [
        new_case(
            c.get("question", ""),
            was_refusal=bool(c.get("was_refusal")),
            reason=c.get("reason"),
            sources_cited=c.get("sources_cited") or "",
        ) | {"expected_sources": c.get("expected_sources") or []}
        for c in raw
        if (c.get("question") or "").strip()
    ]


def known_filenames() -> list[str]:
    """Sorted corpus filenames, offered as the menu during curation so the
    curator picks a real document instead of typing one from memory."""
    from .db import get_known_sources

    return sorted({filename for filename, _ in get_known_sources()})


def file_corpora() -> dict[str, str]:
    """filename -> corpus, so a curated case records the right retrieval tool.
    Falls back to an empty map when the DB is unavailable."""
    try:
        from .db import get_source_corpora
        return get_source_corpora()
    except Exception:
        return {}


def corpus_for_sources(sources: list[str], mapping: dict[str, str]) -> str:
    """
    Corpus a case should be tested against, given its expected sources.

    Uses the first source with a known corpus. Mixed-corpus expectations fall
    back to that first match rather than failing: the regression assertion only
    needs *one* expected source to be surfaced.
    """
    for source in sources:
        if source in mapping:
            return mapping[source]
    return default_corpus().name


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_status(cases: list[dict]) -> None:
    curated = [c for c in cases if is_curated(c)]
    skipped = [c for c in cases if is_skipped(c)]
    pending = pending_cases(cases)

    print("=" * 60)
    print("  Ask Buddy — regression eval curation")
    print("=" * 60)
    print(f"  Curated (active tests) : {len(curated)}")
    print(f"  Pending curation       : {len(pending)}")
    print(f"  Skipped                : {len(skipped)}")
    print()

    if pending:
        print("  Pending:")
        for case in pending:
            flag = " [was refusal]" if case.get("was_refusal") else ""
            reason = f" ({case['reason']})" if case.get("reason") else ""
            print(f"    • {case['question'][:70]!r}{flag}{reason}")
        print()

    if curated:
        print("  Curated:")
        for case in curated:
            print(f"    ✅ {case['question'][:60]!r} -> {', '.join(case['expected_sources'])}")
        print()


# ---------------------------------------------------------------------------
# Interactive curation
# ---------------------------------------------------------------------------

def _prompt_for_sources(case: dict, filenames: list[str]) -> list[str] | None:
    """
    Ask which document(s) should answer this question.

    Returns the chosen filenames, [] to skip the case, or None to stop
    curating and save what's been done so far.
    """
    print("-" * 60)
    print(f"Question : {case['question']}")
    if case.get("was_refusal"):
        print("Note     : Ask Buddy refused this — a 👎 means it should have answered.")
    if case.get("reason"):
        print(f"👎 reason : {case['reason']}")
    if case.get("sources_cited"):
        cited = " ".join(case["sources_cited"].split())
        print(f"Cited    : {cited[:100]}")
    print()

    if filenames:
        print("Corpus documents:")
        for index, filename in enumerate(filenames, start=1):
            print(f"  {index:>2}. {filename}")
        print()

    raw = input("Expected source(s) [numbers or filenames, 's'=skip, 'q'=quit]: ").strip()

    if raw.lower() in ("q", "quit"):
        return None
    if raw.lower() in ("s", "skip", ""):
        return []

    chosen: list[str] = []
    for token in parse_sources_arg(raw):
        if token.isdigit() and filenames:
            index = int(token)
            if 1 <= index <= len(filenames):
                chosen.append(filenames[index - 1])
            else:
                print(f"  ignoring out-of-range choice {index}")
        elif filenames and token not in filenames:
            print(f"  WARNING: {token!r} is not in the corpus — keeping it anyway")
            chosen.append(token)
        else:
            chosen.append(token)
    return chosen


def curate_interactively(cases: list[dict], filenames: list[str],
                         mapping: dict[str, str] | None = None) -> int:
    """Walk pending cases, prompting for each. Returns the number curated."""
    mapping = mapping or {}
    pending = pending_cases(cases)
    if not pending:
        print("Nothing pending — every case is curated or skipped. ✅")
        return 0

    print(f"{len(pending)} case(s) to curate. Ctrl-C or 'q' saves and exits.\n")
    curated_count = 0

    for case in pending:
        try:
            chosen = _prompt_for_sources(case, filenames)
        except (KeyboardInterrupt, EOFError):
            print("\nStopping — progress so far will be saved.")
            break

        if chosen is None:
            print("Stopping — progress so far will be saved.")
            break
        if not chosen:
            case["skipped"] = True
            print("  skipped.\n")
            continue

        case["expected_sources"] = chosen
        case["corpus"] = corpus_for_sources(chosen, mapping)
        case.pop("skipped", None)
        curated_count += 1
        print(f"  ✅ expects: {', '.join(chosen)}  [corpus: {case['corpus']}]\n")

    return curated_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Curate regression eval cases from Ask Buddy feedback.",
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_EVAL_FILE,
                        help=f"Curated eval file (default: {DEFAULT_EVAL_FILE})")
    parser.add_argument("--from-file", type=Path, metavar="PATH",
                        help="Read candidates from a --export-evals file instead of the DB")
    parser.add_argument("--no-pull", action="store_true",
                        help="Don't look for new candidates; work with the existing file only")
    parser.add_argument("--list", action="store_true",
                        help="Print curation status and exit")
    parser.add_argument("--set", metavar="QUESTION",
                        help="Non-interactively curate one case (needs --sources)")
    parser.add_argument("--sources", metavar="LIST",
                        help="Comma/space-separated expected filenames for --set")
    parser.add_argument("--corpus", metavar="NAME",
                        help="Corpus for --set (default: inferred from the sources)")
    parser.add_argument("--skip", metavar="QUESTION",
                        help="Mark a case as not worth a regression test")
    args = parser.parse_args()

    cases = load_cases(args.file)

    # Fold in new candidates unless the caller asked us not to. The result is
    # persisted straight away — otherwise `--list` would show pending cases
    # that aren't in the file yet, and a follow-up `--set`/`--skip` naming one
    # of them would find nothing to update.
    if not args.no_pull:
        try:
            candidates = (candidates_from_file(args.from_file) if args.from_file
                          else candidates_from_db())
            before = len(cases)
            cases = merge_cases(cases, candidates)
            if len(cases) > before:
                print(f"Found {len(cases) - before} new candidate(s).")
                write_cases(args.file, cases)
        except Exception as exc:
            # No DB / no export file is not fatal — curating what's already in
            # the file is still useful.
            print(f"WARNING: could not load new candidates ({exc}).", file=sys.stderr)

    if args.set:
        if not args.sources:
            parser.error("--set requires --sources")
        sources = parse_sources_arg(args.sources)
        corpus = args.corpus or corpus_for_sources(sources, file_corpora())
        if not set_expected_sources(cases, args.set, sources, corpus=corpus):
            cases.append(new_case(args.set, corpus=corpus)
                         | {"expected_sources": sources, "corpus": corpus})
            print(f"Added new curated case: {args.set!r} -> "
                  f"{', '.join(sources)} [corpus: {corpus}]")
        else:
            print(f"Curated: {args.set!r} -> {', '.join(sources)} [corpus: {corpus}]")
        write_cases(args.file, cases)
        return

    if args.skip:
        if not skip_case(cases, args.skip):
            print(f"No case matching {args.skip!r}.", file=sys.stderr)
            sys.exit(1)
        write_cases(args.file, cases)
        print(f"Skipped: {args.skip!r}")
        return

    if args.list:
        print_status(cases)
        return

    try:
        filenames = known_filenames()
    except Exception as exc:
        print(f"WARNING: could not read corpus filenames ({exc}) — "
              "you'll need to type them by hand.", file=sys.stderr)
        filenames = []

    curated_count = curate_interactively(cases, filenames, file_corpora())
    write_cases(args.file, cases)

    print("=" * 60)
    print(f"Saved {args.file}")
    print(f"{curated_count} newly curated | "
          f"{len([c for c in cases if is_curated(c)])} active regression test(s)")
    print()
    print("Run them with:")
    print("  uv run pytest tests/test_ask_buddy.py -k FeedbackRegressionEvals -v")


if __name__ == "__main__":
    main()
