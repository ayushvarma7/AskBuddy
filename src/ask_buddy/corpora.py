"""
Corpus registry — one place that defines each document domain.

Adding a third corpus used to mean four coordinated edits: a new ``@tool`` in
retrieve.py, a system prompt and a builder in agent.py, a bullet in the
supervisor prompt, and a directory in ingest.py's defaults dict. Miss the
supervisor bullet and the agent exists but is never routed to — a silent
failure.

Now a corpus is one entry below. Everything else derives from it: the retrieval
tool, the sub-agent, its prompt, the supervisor's routing bullet, and the
ingest default directory.

To add one, append a ``Corpus`` here and re-run ingest with
``--corpus <name>``. No other file needs touching.

Stdlib-only, so the registry can be imported and tested without the project's
dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_DATA_ROOT = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class Corpus:
    """One document domain and everything derived from it."""

    name: str
    """Value stored in hr_chunks.corpus and passed to --corpus."""

    agent_name: str
    """Supervisor key, e.g. 'hr_agent'."""

    tool_name: str
    """Retrieval tool name the agent's prompt tells it to call."""

    docs_dir: Path
    """Default source directory for ingest."""

    label: str
    """Short human name used in prose, e.g. 'HR'."""

    role: str
    """Sentence completing "You answer questions about …" in the agent prompt."""

    topics: str
    """Comma-separated topic list, used for supervisor routing and the tool
    docstring. This is what makes routing work, so it should be generous."""

    refusal_domain: str
    """How the domain is named in a refusal, e.g. 'HR documents'."""


CORPORA: tuple[Corpus, ...] = (
    Corpus(
        name="hr",
        agent_name="hr_agent",
        tool_name="hr_retrieve",
        docs_dir=_DATA_ROOT / "hr_docs" / "synthetic",
        label="HR",
        role=(
            "HR topics ONLY: time off, leave, benefits, expenses, performance "
            "reviews, remote work, and workplace conduct"
        ),
        topics=(
            "PTO, vacation, sick leave, parental leave, benefits, health "
            "insurance, 401k, expense reimbursement, performance reviews, "
            "remote work, code of conduct, workplace behaviour, hiring, "
            "onboarding, offboarding, compensation"
        ),
        refusal_domain="HR documents",
    ),
    Corpus(
        name="it",
        agent_name="it_agent",
        tool_name="it_retrieve",
        docs_dir=_DATA_ROOT / "it_docs",
        label="IT Security",
        role=(
            "IT and information security topics ONLY: passwords, "
            "authentication, MFA, VPN, remote access, device management, "
            "endpoint security, data classification, data handling, and "
            "acceptable use of company IT resources"
        ),
        topics=(
            "passwords, MFA, VPN, firewalls, network access, device "
            "management, endpoint security, data classification, data "
            "handling, acceptable use, software licensing, encryption, "
            "helpdesk"
        ),
        refusal_domain="IT security documents",
    ),
)


def by_name(name: str) -> Corpus:
    """Look up a corpus by its `name`. Raises KeyError with the valid options."""
    for corpus in CORPORA:
        if corpus.name == name:
            return corpus
    raise KeyError(f"Unknown corpus {name!r}. Known: {', '.join(names())}")


def names() -> list[str]:
    return [c.name for c in CORPORA]


def docs_dir_for(name: str) -> Path:
    return by_name(name).docs_dir


def default_corpus() -> Corpus:
    """The corpus used when none is named — preserves the historical 'hr'
    default of the ingest CLI."""
    return CORPORA[0]
