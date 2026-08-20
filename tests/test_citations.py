"""
Offline tests for the deterministic citation validator.

No DB, no network, no API key — citations.py is stdlib-only by design so
this file runs everywhere, including in CI.
"""

from __future__ import annotations

from src.ask_buddy.citations import (
    STATUS_MISSING_SOURCES,
    STATUS_OK,
    STATUS_REFUSAL,
    STATUS_UNKNOWN_FILE,
    STATUS_UNKNOWN_SECTION,
    cites_documents,
    has_sources_line,
    parse_source_lines,
    validate_citations,
)


KNOWN = {
    ("pto_policy.md", "2. PTO Accrual Rates"),
    ("pto_policy.md", "3. Requesting PTO"),
    ("parental_leave.md", "Leave Duration"),
    ("it_security_policy.md", "2. Password and Authentication Requirements"),
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParseSourceLines:
    def test_single_citation_with_section_and_date(self):
        answer = (
            "Employees with 5-10 years get 160 hours of PTO.\n"
            "\n"
            "Source(s): pto_policy.md — 2. PTO Accrual Rates (effective 2024-01-01)"
        )
        cites = parse_source_lines(answer)
        assert len(cites) == 1
        assert cites[0].filename == "pto_policy.md"
        assert cites[0].section == "2. PTO Accrual Rates"
        assert cites[0].effective_date == "2024-01-01"

    def test_multiple_citations_one_per_line(self):
        answer = (
            "You have 30 days to add a dependent.\n"
            "\n"
            "Source(s): parental_leave.md — Leave Duration (effective 2024-03-01)\n"
            "           pto_policy.md — 3. Requesting PTO\n"
        )
        cites = parse_source_lines(answer)
        assert [c.filename for c in cites] == ["parental_leave.md", "pto_policy.md"]
        assert cites[1].effective_date is None

    def test_citation_without_section(self):
        cites = parse_source_lines("Answer.\n\nSource(s): pto_policy.md")
        assert len(cites) == 1
        assert cites[0].section is None

    def test_accepts_hyphen_and_en_dash_separators(self):
        for sep in ("—", "–", "-", "--"):
            answer = f"Answer.\n\nSource(s): pto_policy.md {sep} 2. PTO Accrual Rates"
            cites = parse_source_lines(answer)
            assert len(cites) == 1, f"separator {sep!r} failed to parse"
            assert cites[0].section == "2. PTO Accrual Rates"

    def test_accepts_sources_and_source_headers(self):
        for header in ("Source(s):", "Sources:", "Source:"):
            cites = parse_source_lines(f"Answer.\n\n{header} pto_policy.md")
            assert len(cites) == 1, f"header {header!r} failed to parse"

    def test_blank_line_terminates_block(self):
        answer = (
            "Answer.\n\nSource(s): pto_policy.md — 3. Requesting PTO\n"
            "\n"
            "Some unrelated trailing prose with a file.md in it\n"
        )
        cites = parse_source_lines(answer)
        assert [c.filename for c in cites] == ["pto_policy.md"]

    def test_no_sources_line_returns_empty(self):
        assert parse_source_lines("Just an answer, no citations.") == []

    def test_prose_inside_block_is_skipped_not_guessed(self):
        answer = (
            "Answer.\n\nSource(s): pto_policy.md — 3. Requesting PTO\n"
            "           see your manager for details\n"
        )
        cites = parse_source_lines(answer)
        assert [c.filename for c in cites] == ["pto_policy.md"]

    def test_has_sources_line(self):
        assert has_sources_line("Answer.\n\nSource(s): x.md")
        assert not has_sources_line("Answer with no citation block.")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateCitations:
    def test_valid_citation_is_ok(self):
        answer = "Answer.\n\nSource(s): pto_policy.md — 2. PTO Accrual Rates (effective 2024-01-01)"
        verdict = validate_citations(answer, KNOWN)
        assert verdict.status == STATUS_OK
        assert verdict.ok
        assert not verdict.problems

    def test_fabricated_filename_is_flagged(self):
        answer = "Answer.\n\nSource(s): vacation_handbook.md — Section 4"
        verdict = validate_citations(answer, KNOWN)
        assert verdict.status == STATUS_UNKNOWN_FILE
        assert not verdict.ok
        assert verdict.problems[0][0].filename == "vacation_handbook.md"

    def test_fabricated_section_is_flagged(self):
        answer = "Answer.\n\nSource(s): pto_policy.md — 9. Unlimited Vacation"
        verdict = validate_citations(answer, KNOWN)
        assert verdict.status == STATUS_UNKNOWN_SECTION
        assert not verdict.ok

    def test_missing_sources_line_is_flagged(self):
        verdict = validate_citations("You get 20 days of PTO.", KNOWN)
        assert verdict.status == STATUS_MISSING_SOURCES
        assert not verdict.ok

    def test_refusal_short_circuits(self):
        refusal = ("No results found in our documents for that question — "
                   "please reach out to the appropriate team for help.")
        verdict = validate_citations(refusal, KNOWN, is_refusal=True)
        assert verdict.status == STATUS_REFUSAL
        assert verdict.ok

    def test_filename_only_citation_cannot_fabricate_a_section(self):
        verdict = validate_citations("Answer.\n\nSource(s): pto_policy.md", KNOWN)
        assert verdict.status == STATUS_OK

    def test_comparison_tolerates_whitespace_and_case_drift(self):
        answer = "Answer.\n\nSource(s): PTO_Policy.md — 2.  pto   accrual rates"
        verdict = validate_citations(answer, KNOWN)
        assert verdict.status == STATUS_OK

    def test_dropped_leading_number_is_not_a_fabrication(self):
        """Models routinely cite "PTO Accrual Rates" for the heading
        "2. PTO Accrual Rates" — that must not read as invented."""
        answer = "Answer.\n\nSource(s): pto_policy.md — PTO Accrual Rates"
        assert validate_citations(answer, KNOWN).status == STATUS_OK

    def test_added_prefix_is_not_a_fabrication(self):
        answer = ("Answer.\n\nSource(s): pto_policy.md — "
                  "Section 2. PTO Accrual Rates (effective 2024-01-01)")
        assert validate_citations(answer, KNOWN).status == STATUS_OK

    def test_invented_heading_is_still_caught(self):
        answer = "Answer.\n\nSource(s): pto_policy.md — Unlimited Sabbatical Program"
        assert validate_citations(answer, KNOWN).status == STATUS_UNKNOWN_SECTION

    def test_section_from_the_wrong_file_is_caught(self):
        """A real heading, but not one that belongs to the cited document."""
        answer = "Answer.\n\nSource(s): pto_policy.md — Leave Duration"
        assert validate_citations(answer, KNOWN).status == STATUS_UNKNOWN_SECTION

    def test_empty_known_section_does_not_match_everything(self):
        """A chunk with no heading stores section='' — that must not make
        every cited section look valid."""
        known = {("notes.md", "")}
        answer = "Answer.\n\nSource(s): notes.md — Some Invented Heading"
        assert validate_citations(answer, known).status == STATUS_UNKNOWN_SECTION

    def test_worst_problem_wins_across_multiple_citations(self):
        answer = (
            "Answer.\n\n"
            "Source(s): pto_policy.md — 9. Invented Heading\n"
            "           totally_made_up.md — Whatever\n"
        )
        verdict = validate_citations(answer, KNOWN)
        # unknown_file outranks unknown_section
        assert verdict.status == STATUS_UNKNOWN_FILE
        assert len(verdict.problems) == 2

    def test_empty_corpus_flags_every_citation(self):
        answer = "Answer.\n\nSource(s): pto_policy.md — 2. PTO Accrual Rates"
        verdict = validate_citations(answer, set())
        assert verdict.status == STATUS_UNKNOWN_FILE

    def test_summary_is_human_readable(self):
        answer = "Answer.\n\nSource(s): made_up.md — Nope"
        summary = validate_citations(answer, KNOWN).summary()
        assert "made_up.md" in summary
        assert STATUS_UNKNOWN_FILE in summary


# ---------------------------------------------------------------------------
# Chunk attribution
# ---------------------------------------------------------------------------

class TestCitesDocuments:
    """Decides whether retrieved chunk IDs get attributed to an answer."""

    def test_answer_with_sources_cites_documents(self):
        answer = "You get 20 days.\n\nSource(s): pto_policy.md — 2. PTO Accrual Rates"
        assert cites_documents(answer)

    def test_github_answer_does_not(self):
        answer = ("Here are the open issues in acme/backend:\n"
                  "• #5 Benchmark run hangs — open\n"
                  "• #4 Add latency percentiles — open")
        assert not cites_documents(answer)

    def test_reminder_confirmation_does_not(self):
        answer = ("I've scheduled this to repeat every Friday at 9:00 AM Pacific "
                  "in #eng-team. Reminder id 42.")
        assert not cites_documents(answer)

    def test_clarifying_question_does_not(self):
        assert not cites_documents("Which repository do you mean?")

    def test_refusal_does_not_even_with_a_sources_line(self):
        answer = ("No results found in our documents for that question — please "
                  "reach out to the appropriate team for help.\n\n"
                  "Source(s): pto_policy.md")
        assert not cites_documents(answer, is_refusal=True)

    def test_unparseable_sources_block_does_not_count(self):
        """A "Source(s):" header with nothing citation-shaped under it must not
        pull chunk attribution along with it."""
        assert not cites_documents("Answer.\n\nSource(s): ask your manager")
