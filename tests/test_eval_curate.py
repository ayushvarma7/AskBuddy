"""
Offline tests for the regression-eval curation step.

The DB-touching functions (candidates_from_db, known_filenames) are thin
wrappers and are deliberately not covered here — everything that decides what
lands in the committed eval file is pure and tested.
"""

from __future__ import annotations

import json

from src.ask_buddy.eval_curate import (
    curate_interactively,
    is_curated,
    is_skipped,
    load_cases,
    merge_cases,
    new_case,
    normalize_question,
    parse_sources_arg,
    pending_cases,
    set_expected_sources,
    skip_case,
    write_cases,
)


class TestNormalizeQuestion:
    def test_collapses_whitespace_and_case(self):
        assert normalize_question("  How   Much PTO?  ") == "how much pto?"

    def test_differing_only_in_spacing_are_the_same_key(self):
        assert normalize_question("a  b") == normalize_question("a b")


class TestCuratedAndSkipped:
    def test_case_without_sources_is_not_curated(self):
        assert not is_curated(new_case("q"))

    def test_case_with_sources_is_curated(self):
        case = new_case("q") | {"expected_sources": ["pto_policy.md"]}
        assert is_curated(case)

    def test_skipped_flag(self):
        assert is_skipped(new_case("q") | {"skipped": True})
        assert not is_skipped(new_case("q"))

    def test_pending_excludes_curated_and_skipped(self):
        cases = [
            new_case("uncurated"),
            new_case("curated") | {"expected_sources": ["a.md"]},
            new_case("skipped") | {"skipped": True},
        ]
        assert [c["question"] for c in pending_cases(cases)] == ["uncurated"]


class TestMergeCases:
    def test_new_candidate_is_appended(self):
        merged = merge_cases([], [new_case("brand new")])
        assert [c["question"] for c in merged] == ["brand new"]

    def test_existing_curation_is_never_overwritten(self):
        existing = [new_case("how much PTO?") | {"expected_sources": ["pto_policy.md"]}]
        # Same question resurfacing from a later export, uncurated.
        merged = merge_cases(existing, [new_case("How Much PTO?")])
        assert len(merged) == 1
        assert merged[0]["expected_sources"] == ["pto_policy.md"]

    def test_skipped_case_is_not_reproposed(self):
        existing = [new_case("vague thing") | {"skipped": True}]
        merged = merge_cases(existing, [new_case("vague thing")])
        assert len(merged) == 1
        assert is_skipped(merged[0])
        assert pending_cases(merged) == []

    def test_ordering_is_stable_for_clean_diffs(self):
        existing = [new_case("first"), new_case("second")]
        merged = merge_cases(existing, [new_case("third"), new_case("first")])
        assert [c["question"] for c in merged] == ["first", "second", "third"]

    def test_blank_questions_are_dropped(self):
        merged = merge_cases([], [new_case("   "), new_case("real")])
        assert [c["question"] for c in merged] == ["real"]

    def test_duplicate_candidates_collapse(self):
        merged = merge_cases([], [new_case("dupe"), new_case("DUPE")])
        assert len(merged) == 1


class TestSetAndSkip:
    def test_set_expected_sources_matches_case_insensitively(self):
        cases = [new_case("How much PTO?")]
        assert set_expected_sources(cases, "how much pto?", ["pto_policy.md"])
        assert cases[0]["expected_sources"] == ["pto_policy.md"]

    def test_set_returns_false_when_no_match(self):
        assert not set_expected_sources([new_case("a")], "b", ["x.md"])

    def test_curating_clears_a_previous_skip(self):
        cases = [new_case("q") | {"skipped": True}]
        set_expected_sources(cases, "q", ["a.md"])
        assert not is_skipped(cases[0])
        assert is_curated(cases[0])

    def test_skip_clears_sources_so_the_test_stops_running(self):
        cases = [new_case("q") | {"expected_sources": ["a.md"]}]
        assert skip_case(cases, "q")
        assert not is_curated(cases[0])
        assert is_skipped(cases[0])


class TestParseSourcesArg:
    def test_comma_separated(self):
        assert parse_sources_arg("a.md, b.md") == ["a.md", "b.md"]

    def test_space_separated(self):
        assert parse_sources_arg("a.md b.md") == ["a.md", "b.md"]

    def test_extra_separators_ignored(self):
        assert parse_sources_arg(" a.md ,, b.md ") == ["a.md", "b.md"]

    def test_empty_string(self):
        assert parse_sources_arg("") == []


class TestFileRoundTrip:
    def test_write_then_load(self, tmp_path):
        path = tmp_path / "regression_evals.json"
        cases = [new_case("q") | {"expected_sources": ["pto_policy.md"]}]
        write_cases(path, cases)
        assert load_cases(path) == cases

    def test_missing_file_is_empty(self, tmp_path):
        assert load_cases(tmp_path / "nope.json") == []

    def test_corrupt_file_is_empty_not_a_crash(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert load_cases(path) == []

    def test_output_matches_the_shape_the_test_suite_consumes(self, tmp_path):
        """tests/test_ask_buddy.py::_load_curated_evals filters on
        expected_sources and reads `question` — guard that contract."""
        path = tmp_path / "regression_evals.json"
        write_cases(path, [
            new_case("curated q") | {"expected_sources": ["pto_policy.md"]},
            new_case("pending q"),
        ])
        data = json.loads(path.read_text())
        actionable = [c for c in data if c.get("expected_sources")]
        assert len(actionable) == 1
        assert actionable[0]["question"] == "curated q"
        assert set(actionable[0]["expected_sources"]) == {"pto_policy.md"}

    def test_written_file_ends_with_newline(self, tmp_path):
        path = tmp_path / "regression_evals.json"
        write_cases(path, [new_case("q")])
        assert path.read_text().endswith("\n")


class TestInteractiveCuration:
    """The prompt loop, driven by a scripted stdin."""

    FILENAMES = ["benefits_enrollment.md", "parental_leave.md", "pto_policy.md"]

    def _answers(self, monkeypatch, *responses):
        queued = iter(responses)
        monkeypatch.setattr("builtins.input", lambda *_: next(queued))

    def test_numeric_choice_maps_to_filename(self, monkeypatch):
        self._answers(monkeypatch, "3")
        cases = [new_case("how much PTO?")]
        assert curate_interactively(cases, self.FILENAMES) == 1
        assert cases[0]["expected_sources"] == ["pto_policy.md"]

    def test_multiple_numeric_choices(self, monkeypatch):
        self._answers(monkeypatch, "1 2")
        cases = [new_case("newborn insurance?")]
        curate_interactively(cases, self.FILENAMES)
        assert cases[0]["expected_sources"] == [
            "benefits_enrollment.md", "parental_leave.md",
        ]

    def test_typed_filename_is_accepted(self, monkeypatch):
        self._answers(monkeypatch, "pto_policy.md")
        cases = [new_case("q")]
        curate_interactively(cases, self.FILENAMES)
        assert cases[0]["expected_sources"] == ["pto_policy.md"]

    def test_out_of_range_number_is_ignored(self, monkeypatch):
        self._answers(monkeypatch, "99 1")
        cases = [new_case("q")]
        curate_interactively(cases, self.FILENAMES)
        assert cases[0]["expected_sources"] == ["benefits_enrollment.md"]

    def test_s_skips_the_case(self, monkeypatch):
        self._answers(monkeypatch, "s")
        cases = [new_case("q")]
        assert curate_interactively(cases, self.FILENAMES) == 0
        assert is_skipped(cases[0])

    def test_empty_input_skips(self, monkeypatch):
        self._answers(monkeypatch, "")
        cases = [new_case("q")]
        curate_interactively(cases, self.FILENAMES)
        assert is_skipped(cases[0])

    def test_q_stops_and_leaves_the_rest_pending(self, monkeypatch):
        self._answers(monkeypatch, "1", "q")
        cases = [new_case("first"), new_case("second"), new_case("third")]
        assert curate_interactively(cases, self.FILENAMES) == 1
        assert is_curated(cases[0])
        assert pending_cases(cases) == [cases[1], cases[2]]

    def test_ctrl_c_preserves_work_done_so_far(self, monkeypatch):
        def _raise(*_):
            raise KeyboardInterrupt

        calls = {"n": 0}

        def _input(*_):
            calls["n"] += 1
            if calls["n"] == 1:
                return "3"
            _raise()

        monkeypatch.setattr("builtins.input", _input)
        cases = [new_case("first"), new_case("second")]
        assert curate_interactively(cases, self.FILENAMES) == 1
        assert cases[0]["expected_sources"] == ["pto_policy.md"]
        assert not is_curated(cases[1])

    def test_nothing_pending_is_a_clean_noop(self, monkeypatch):
        self._answers(monkeypatch)   # input must never be called
        cases = [new_case("done") | {"expected_sources": ["a.md"]}]
        assert curate_interactively(cases, self.FILENAMES) == 0

    def test_unknown_filename_warns_but_is_kept(self, monkeypatch, capsys):
        self._answers(monkeypatch, "not_in_corpus.md")
        cases = [new_case("q")]
        curate_interactively(cases, self.FILENAMES)
        assert cases[0]["expected_sources"] == ["not_in_corpus.md"]
        assert "not in the corpus" in capsys.readouterr().out
