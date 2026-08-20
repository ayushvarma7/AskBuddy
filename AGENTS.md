# Working on Ask Buddy

Conventions this codebase actually follows. They are load-bearing: most exist
because the alternative broke something. If you are changing code here, read the
section that covers what you are touching.

For CUGA framework documentation (policies, the SDK, its own skill system), see
the skills under `.claude/skills/` — that material is not repeated here.

---

## The one invariant

**Ask Buddy never answers from general knowledge.** Every layer enforces this
independently, and none of them is redundant:

1. `enable_knowledge=False` on every `CugaAgent` — disables CUGA's built-in RAG.
2. Tool lists are minimal. A domain agent gets exactly
   `[<corpus>_retrieve, post_slack_message]`. No web search, no cross-corpus tool.
3. Prompts forbid it in a shared block (`_SHARED_RULES` in `agent.py`).
4. `citations.py` **verifies** it after the fact, checking every cited filename
   and section against real corpus metadata.

If you add an agent, it inherits 1–3 automatically only if you build it through
the corpus registry. Do that.

---

## Adding a document domain

One entry in `src/ask_buddy/corpora.py`. That is the whole change.

Everything derives from it: the retrieval `@tool`, its docstring (which is what
the LLM reads to pick a tool, so make `topics` generous), the sub-agent, its
system prompt including the shared guardrails, the supervisor's routing bullet,
and the ingest default directory.

Then `uv run python -m src.ask_buddy.ingest --corpus <name>`.

Do **not** hand-write a new `@tool` in `retrieve.py` or a new prompt in
`agent.py`. The registry exists because the old four-file version had a silent
failure mode: forget the supervisor bullet and the agent is built, never routed
to, and never mentioned in a log.

`tests/test_corpora.py` asserts the invariants a new entry has to satisfy.

---

## GitHub write actions

Three tiers, and the tier decides where the tool goes:

| Tier | Home | Examples |
|---|---|---|
| Read | `_build_git_issue_tools` / `_build_git_pr_tools` | list, get, search, checks, merge status |
| Low-risk write | `_build_git_write_tools` | comment, label, assign, request review, create |
| High-risk write | `_build_git_dangerous_tools` | `set_issue_state`, `merge_pull_request` |

Anything in the third tier **must** be registered with
`agent.policies.add_tool_approval` using a stable `policy_id`, so the
CugaSupervisor pauses and Slack shows Confirm/Cancel before it runs. A
dangerous tool that isn't approval-gated is a bug, not a shortcut.

The approval flow has one subtlety worth understanding before touching it: the
paused graph uses an **in-memory** checkpointer, so resuming requires the exact
same supervisor object. That is why `_pending_approvals` holds the live object
and why `_stash_pending_approval` refuses to overwrite an existing entry rather
than clobbering it. It also means a restart genuinely cannot resume a pending
approval — `ask_buddy_pending_approvals` exists so the user is *told* that,
and so there is an audit trail of every high-risk action ever proposed.

---

## Database

- **DDL is append-only and idempotent.** Every table's DDL is a module-level
  constant re-run at startup. New columns go in both the `CREATE TABLE` and the
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` list, so an existing database
  upgrades itself. Never rewrite a column in place.
- **Never rename a column to fix a name.** `hr_chunks.bm25_tsvector` ranks with
  `ts_rank_cd`, not BM25. The name is wrong and it stays, with a comment,
  because renaming needs a data migration.
- **One table, many corpora.** `hr_chunks` holds every domain, discriminated by
  `corpus`. Retrieval always filters on it; that filter is the isolation
  guarantee, so don't add a query path that skips it.
- **Concurrency guards belong in SQL.** `record_feedback` uses
  `UPDATE ... AND feedback IS NULL RETURNING` so a double-clicked button can't
  overwrite a rating. Prefer that shape over application-level locking.
- Aggregates read on the hot path are cached with a short TTL
  (`get_chunk_quality`, `get_known_sources`). Follow the existing pattern.

---

## Slack handling

- Handlers must `ack()` fast and hand real work to a thread. Slack times out
  at 3 seconds.
- Track every worker thread with `service_registry.track_worker(t)` so shutdown
  can drain it.
- `_run_agent_for_message` re-binds the request id with `set_request_id`,
  because `threading.Thread` starts with an **empty** contextvar context. A new
  background thread that logs needs the same treatment.
- Pass `thread_ts` through and post with `**_thread_kwargs(thread_ts)`. A reply
  to a threaded mention belongs in the thread.
- `_is_direct_message` gates the `message` event. Do not rely on the Slack app's
  event subscription to keep it DM-only.

---

## Background services

Start them through `lifecycle.registry.start_service(name, starter)`, never with
a bare `try/except`. The convention a starter follows:

- returns a scheduler → running
- returns `None` → disabled by configuration (a deployment choice, not a fault)
- raises → recorded as failed, never propagated

That contract is what makes `/askbuddy status` able to tell "off on purpose"
from "broken", and what gives shutdown a handle to stop.

Only the reminder scheduler uses a persistent job store. The triage watcher and
digest take a Slack post function as a job argument, which cannot be pickled —
and they need no catch-up, since they poll current state.

---

## Single sources of truth

Don't duplicate these. Import them.

| Thing | Home |
|---|---|
| Refusal string | `feedback.REFUSAL_TEXT` (+ `is_refusal_text`) |
| What counts as citing a document | `citations.cites_documents` |
| Corpus definitions | `corpora.CORPORA` |
| Negative feedback reasons | `feedback.NEGATIVE_REASONS` |
| Which agent config produced an answer | `agent.current_agent_config` |

`current_agent_config` includes a hash of every system prompt, so the feedback
report can tell a prompt edit from a model swap. If you add a prompt constant
that ships with the bot, add it to `prompt_fingerprint`.

---

## Tests

Two speeds, and it matters which one you add to:

- **Stdlib-only suites** — `test_citations.py`, `test_eval_curate.py`,
  `test_logging_setup.py`, `test_lifecycle.py`, `test_ratelimit.py`,
  `test_corpora.py`. No DB, no network, no API key. They run in seconds and in
  CI's `fast-tests` job. Keep the modules they cover dependency-free so this
  stays true.
- **`test_ask_buddy.py`** — needs the project's dependencies; the
  `@integration` subset needs live Postgres and `GOOGLE_API_KEY`.

When you can express logic as a pure function, do — that is why
`cites_documents`, `_rrf_merge`, and the `Registry` are shaped the way they are.

Don't assert on CUGA internals (`agent._tools` and friends). Capture what gets
passed to `CugaAgent` by monkeypatching it instead.

Before pushing:

```bash
uv run ruff check . && uv run mypy && uv run pytest -m "not integration"
```

---

## The feedback loop

A 👎 in Slack is meant to end up as a test. The path:

`ask_buddy_feedback` → `eval_curate` (a human names the expected source) →
`tests/regression_evals.json` (committed) → `TestFeedbackRegressionEvals`.

If you change retrieval, that suite is what tells you whether you broke a
question a real user already complained about.
