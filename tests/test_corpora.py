"""
Offline tests for the corpus registry.

corpora.py is stdlib-only. The consumers (retrieve.py, agent.py, ingest.py)
need the project's dependencies, so their integration with the registry is
covered in test_ask_buddy.py.
"""

from __future__ import annotations

import pytest

from src.ask_buddy.corpora import (
    CORPORA,
    Corpus,
    by_name,
    default_corpus,
    docs_dir_for,
    names,
)


class TestRegistryContents:
    def test_hr_and_it_are_registered(self):
        assert names() == ["hr", "it"]

    def test_default_is_hr(self):
        """The ingest CLI's historical default; changing it would silently
        re-tag documents."""
        assert default_corpus().name == "hr"

    def test_lookup_by_name(self):
        assert by_name("it").agent_name == "it_agent"

    def test_unknown_name_lists_the_valid_options(self):
        with pytest.raises(KeyError) as excinfo:
            by_name("payroll")
        message = str(excinfo.value)
        assert "payroll" in message
        assert "hr" in message and "it" in message

    def test_docs_dir_points_into_data(self):
        assert docs_dir_for("hr").parts[-3:] == ("data", "hr_docs", "synthetic")
        assert docs_dir_for("it").parts[-2:] == ("data", "it_docs")

    def test_entries_are_immutable(self):
        """Frozen so nothing can retag a corpus at runtime."""
        with pytest.raises(Exception):
            by_name("hr").name = "something-else"      # type: ignore[misc]


class TestRegistryInvariants:
    """Properties every corpus must satisfy — these are what keep a new entry
    from being half-wired."""

    @pytest.mark.parametrize("corpus", CORPORA, ids=lambda c: c.name)
    def test_all_fields_are_populated(self, corpus: Corpus):
        for field_name in ("name", "agent_name", "tool_name", "label",
                           "role", "topics", "refusal_domain"):
            value = getattr(corpus, field_name)
            assert isinstance(value, str) and value.strip(), \
                f"{corpus.name}.{field_name} is empty"

    @pytest.mark.parametrize("corpus", CORPORA, ids=lambda c: c.name)
    def test_naming_conventions_hold(self, corpus: Corpus):
        assert corpus.agent_name == f"{corpus.name}_agent"
        assert corpus.tool_name == f"{corpus.name}_retrieve"

    @pytest.mark.parametrize("corpus", CORPORA, ids=lambda c: c.name)
    def test_docs_dir_is_absolute(self, corpus: Corpus):
        assert corpus.docs_dir.is_absolute()

    def test_names_are_unique(self):
        assert len(names()) == len(set(names()))

    def test_agent_names_are_unique(self):
        agent_names = [c.agent_name for c in CORPORA]
        assert len(agent_names) == len(set(agent_names))

    def test_tool_names_are_unique(self):
        tool_names = [c.tool_name for c in CORPORA]
        assert len(tool_names) == len(set(tool_names))

    def test_docs_dirs_are_distinct(self):
        """Two corpora sharing a directory would ingest the same documents
        under two tags."""
        dirs = [c.docs_dir for c in CORPORA]
        assert len(dirs) == len(set(dirs))


class TestExtensibility:
    """A new corpus should need nothing but a registry entry."""

    def test_a_new_entry_satisfies_every_invariant(self):
        added = Corpus(
            name="legal",
            agent_name="legal_agent",
            tool_name="legal_retrieve",
            docs_dir=docs_dir_for("hr").parent.parent / "legal_docs",
            label="Legal",
            role="contract and compliance topics ONLY",
            topics="contracts, NDAs, compliance, data retention",
            refusal_domain="legal documents",
        )
        assert added.agent_name == f"{added.name}_agent"
        assert added.tool_name == f"{added.name}_retrieve"
        assert added.docs_dir.is_absolute()
        assert added not in CORPORA          # registry itself is untouched
