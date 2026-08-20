# Architecture decision records

Short notes on decisions that were expensive to reach and would be expensive to
re-litigate. Each records the choice, why the alternatives lost, and what would
have to change for the decision to be revisited.

`GIT_AGENT_PLAN.md` in the repo root is the ancestor of this directory — it
opens with "Decisions locked in (do not re-litigate)" and records the rejected
nested-supervisor design. It stays as a historical implementation plan; new
decisions go here.

| # | Decision |
|---|---|
| [0001](0001-two-git-agents-not-a-nested-supervisor.md) | Two flat git agents rather than a nested git supervisor |
| [0002](0002-one-table-for-all-corpora.md) | One chunk table for every corpus, discriminated by a column |
| [0003](0003-verify-citations-instead-of-trusting-prompts.md) | Verify citations after the fact instead of trusting the prompt |
| [0004](0004-retrieval-corpus-is-a-trust-boundary.md) | Treat the retrieval corpus as untrusted input |
| [0005](0005-approvals-cannot-survive-a-restart.md) | Accept that pending approvals cannot survive a restart |

## Format

Keep them short. Context, Decision, Consequences, and when to revisit. If it
runs past a page it is design documentation, not a decision record.
