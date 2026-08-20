# AskBuddy

A Slack bot that routes workplace questions to the right specialist, retrieves answers from your own documents, and keeps your engineering team's GitHub activity visible — all without hallucinating or making things up.

---

## What it does

Send AskBuddy a message in Slack — as a DM, an @mention in a channel, or a `/askbuddy` slash command — and it figures out what you're asking about, finds the right answer, and replies in plain English with a source citation.

**HR questions** pull from your ingested HR policy documents. Every answer includes the exact filename, section, and effective date it was drawn from. If the documents don't contain an answer, AskBuddy says so instead of guessing.

**IT security questions** work the same way against a separate IT policy corpus. The two corpora are kept completely separate — an HR question never accidentally returns IT content and vice versa.

**Reminders** let you schedule recurring broadcast messages to Slack channels. "Remind #svl-interns-2026 to submit timecards every Friday at 9am PST" creates a persistent cron job that fires on schedule and survives bot restarts (reminders are stored in Postgres).

**GitHub questions** let you ask about your repos directly in Slack. "List open issues in acme/backend", "summarize issue #42", "is PR #17 ready to merge?", "what files does PR #12 change?" — all answered by reading from the GitHub API. No write access by default; the write tools (comment, label, assign, close, merge) require a PAT scope bump and the dangerous ones (close/merge) show a Slack confirmation prompt before doing anything.

**Triage watcher** polls your watched repos on an interval and posts a short summary of new issues and PRs to a channel of your choice. The first poll seeds watermarks silently so you don't get a dump of every existing item.

**Daily digest** posts a full state snapshot on a schedule — open/closed issue counts, top labels, draft PRs, PRs waiting for a reviewer. You can also trigger it any time with `/askbuddy git digest`.

---

## Agents

AskBuddy runs a `CugaSupervisor` that routes each incoming message to one of five specialist agents. The supervisor decides who handles what based on the topic.

| Agent | What it handles |
|---|---|
| `hr_agent` | PTO, leave, benefits, expenses, performance reviews, remote work, conduct, hiring, onboarding, compensation |
| `it_agent` | Passwords, MFA, VPN, device management, data classification, acceptable use, encryption, helpdesk |
| `scheduler_agent` | Creating, listing, and cancelling recurring Slack reminders |
| `git_issue_agent` | GitHub issue Q&A and write actions (comment, label, assign, close/reopen) |
| `git_pr_agent` | GitHub PR Q&A, file diffs, merge-readiness, and write actions (comment, label, assign, request reviewers, close/reopen, merge) |

Cross-domain questions (e.g. something touching both HR policy and IT security) get delegated to both agents and the answers are combined. Out-of-scope requests get a plain refusal — no fabricated answer, no partial guess blended in.

---

## Architecture

> A full interactive diagram — request path, background jobs, and the feedback
> loop — is in [`docs/architecture.html`](docs/architecture.html); open it in a
> browser. The text below covers the same ground.

**Request path:**
1. A message arrives via Slack Bolt (Socket Mode — no public URL needed)
2. The `CugaSupervisor` reads the message and decides which agent to call
3. The agent calls its tools (retrieval, GitHub API, or scheduler DB)
4. The agent calls `post_slack_message` to deliver the final answer
5. The answer is posted as a Block Kit message with 👍/👎 feedback buttons

**Background jobs (no Slack message needed to start them):**
- `git_watch.py` — APScheduler interval job, polls GitHub every N minutes, posts new-item summaries to the triage channel
- `git_digest.py` — APScheduler cron job, posts a full repo state snapshot on a schedule (default 9 AM daily)
- `scheduler.py` — APScheduler cron jobs, fires recurring reminder messages to Slack channels

**Storage:**
- `hr_chunks` — pgvector table, stores document chunks with embeddings, FTS tsvectors, corpus tag, and effective date
- `ask_buddy_feedback` — one row per posted answer, records the question, answer, sources, thumbs up/down, and reason
- `ask_buddy_reminders` — one row per active reminder with cron expression and channel
- `ask_buddy_git_watch` — one row per watched repo, stores the high-water mark (highest issue/PR number already reported)

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.13 | Earlier versions untested |
| uv | any | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker Desktop | any | For the pgvector Postgres container |
| Google API key | — | Gemini embeddings + LLM (`gemini-2.5-flash` default) |
| Slack workspace | — | Admin access or permission to install apps |
| GitHub PAT | — | Only needed for the git agent features; fine-grained, read-only |

---

## Setup

### 1. Install dependencies

```bash
uv sync
```

This reads `pyproject.toml` and installs everything into `.venv`. No separate `pip install` needed.

### 2. Start the database

```bash
docker compose -f docker-compose.askbuddy.yml up -d
```

Wait ~10 seconds for the health check to pass:

```bash
docker compose -f docker-compose.askbuddy.yml ps
# STATUS column should say "healthy"
```

The container runs pgvector/pg16 on port 5432 with credentials `askbuddy:askbuddy_secret` and database `askbuddy`. Those values are already in `.env.example`.

### 3. Configure the Slack app

Full walkthrough is in [SLACK_PERMISSIONS.md](SLACK_PERMISSIONS.md). The short version:

1. Create a new app at **api.slack.com/apps**
2. Add these Bot Token Scopes: `chat:write`, `chat:write.public`, `im:history`, `im:read`, `im:write`, `users:read`, `channels:history`, `channels:read`, `groups:read`, `app_mentions:read`, `commands`
3. Enable Socket Mode, generate an App-Level Token with `connections:write`
4. Subscribe to bot events: `message.im`, `app_mention`
5. Enable Interactivity (required for feedback buttons)
6. Create the `/askbuddy` slash command
7. Install the app to your workspace and copy the bot token (`xoxb-...`) and app token (`xapp-1-...`)

### 4. Set environment variables

```bash
cp .env.example .env
```

Minimum required to start the bot:

```dotenv
# LLM — pick one provider and uncomment its block
AGENT_SETTING_CONFIG=settings.google.toml
MODEL_NAME=gemini-2.5-flash
GOOGLE_API_KEY=your-key-here

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-1-...

# Database
ASK_BUDDY_DB_DSN=postgresql://askbuddy:askbuddy_secret@localhost:5432/askbuddy
```

Full list of variables is in [`.env.example`](.env.example), including the optional GitHub and digest scheduler variables.

### 5. Ingest documents

```bash
# HR corpus (default directory: data/hr_docs/synthetic/)
uv run python -m src.ask_buddy.ingest --clear

# IT corpus (default directory: data/it_docs/)
uv run python -m src.ask_buddy.ingest --corpus it --clear
```

`--clear` wipes existing chunks for that corpus before re-ingesting. Corpora are independent — clearing HR doesn't touch IT. To point at a different directory: `--docs-dir /path/to/your/docs`.

The ingestor:
- Parses the `Effective Date:` header from each document and stores it per-chunk
- Chunks text at ~500 tokens with overlap so context isn't split across boundaries
- Embeds each chunk with `gemini-embedding-001` (768 dimensions)
- Builds both a pgvector index (for cosine similarity) and a GIN FTS index (for keyword matching)

### 6. Run the tests

```bash
# Offline only — no DB or API key required
uv run pytest tests/test_ask_buddy.py -v -m "not integration"

# Full suite (requires running DB + GOOGLE_API_KEY)
uv run pytest tests/test_ask_buddy.py -v
```

Expect 54 offline tests to pass, 1 skipped.

### 7. Start the bot

```bash
uv run python -m src.ask_buddy.slack_listener
```

Startup log should show:

```
ask_buddy_feedback table ready.
ask_buddy_reminders table ready.
ask_buddy_git_watch table ready.
ask_buddy_pending_approvals table ready.
[lifecycle] reminder_scheduler running
[lifecycle] git_triage_watcher disabled by configuration   ← unless GIT_WATCH_REPOS is set
[lifecycle] git_daily_digest disabled by configuration     ← unless GIT_WATCH_REPOS is set
[lifecycle] approval_sweep running
Starting Ask Buddy in Socket Mode…
⚡️ Bolt app is running!
```

Each background service reports `running`, `disabled by configuration`, or
`failed` — and `/askbuddy status` shows the same thing at any time, so a service
that died isn't invisible.

**Or run it in Docker** — the compose file includes the bot alongside Postgres:

```bash
docker compose -f docker-compose.askbuddy.yml up -d --build
```

That reads your `.env`, points the bot at the `postgres` service, switches
logging to JSON, and allows 20s on stop for in-flight answers to finish. You
still need to run the ingest step above once against the database.

### Stopping cleanly

SIGTERM (or Ctrl-C) triggers a graceful shutdown: schedulers stop, in-flight
answers get up to `ASK_BUDDY_SHUTDOWN_WAIT` seconds to finish delivering, and
the Postgres pool is closed. Any GitHub confirmation still awaiting a click is
marked orphaned, and the user is told it was cancelled on next startup —
nothing runs on GitHub without a live click.

---

## Using the bot

### Ask an HR or IT question

Send a DM to AskBuddy, @mention it in a channel, or use the slash command:

```
/askbuddy how many days of PTO do I get after 3 years?
```

```
@AskBuddy what is the password rotation policy?
```

The answer always includes a `Source(s):` line with the exact document, section, and effective date. If nothing in the corpus answers the question, you get:

```
No results found in our documents for that question — please reach out to the appropriate team for help.
```

Never a guess. Never a made-up citation.

### Schedule a reminder

```
/askbuddy remind #svl-interns-2026 to submit timecards every Friday at 9am PST
```

AskBuddy confirms with the reminder ID and the interpreted schedule ("every Friday at 9:00 AM Pacific"). The reminder persists across bot restarts.

```
/askbuddy list reminders for #svl-interns-2026
/askbuddy cancel reminder 3
```

### Ask about GitHub

These go to `git_issue_agent` or `git_pr_agent` depending on the topic:

```
list open issues in ayushvarma7/GitHub-Sample-Repo
summarize issue #5 in ayushvarma7/GitHub-Sample-Repo
what files does PR #2 change in ayushvarma7/GitHub-Sample-Repo?
is PR #2 ready to merge in ayushvarma7/GitHub-Sample-Repo?
```

For the merge-readiness question, AskBuddy checks three things in one call: draft status, review verdicts (latest per reviewer, so a re-review flips the verdict), and CI check results.

### Get a repo digest on demand

```
/askbuddy git digest
/askbuddy git digest ayushvarma7/GitHub-Sample-Repo
```

Posts immediately to the current channel:

```
📊 Daily repo digest — `ayushvarma7/GitHub-Sample-Repo`

Issues
  • Open: 8   |   Closed: 2
  • Top labels: `bug` ×3   `enhancement` ×1

Pull Requests
  • Open: 1   |   Closed/Merged: 0
  • Waiting for reviewer (1): #2 _Add configurable TTL support_
```

---

## GitHub agent setup

### Read-only setup (Q&A + triage)

Add to `.env`:

```dotenv
GITHUB_TOKEN=github_pat_...
GIT_WATCH_REPOS=owner/repo,owner/repo2
GIT_WATCH_CHANNEL=github-alerts        # channel name or ID (use ID for private channels)
GIT_WATCH_INTERVAL_MINUTES=5
```

Create a fine-grained PAT at **GitHub → Settings → Developer settings → Fine-grained tokens**. Set these repository permissions to **Read-only**: Metadata, Issues, Pull requests, Commit statuses.

In Slack, create the triage channel and run `/invite @AskBuddy` inside it. Then restart the bot.

On first startup, the watcher seeds watermarks silently (no dump of existing items). New issues and PRs created after that point will be posted automatically.

To check the watermarks:

```bash
docker exec -it askbuddy_postgres psql -U askbuddy -d askbuddy \
  -c "SELECT repo, last_issue_number, last_pr_number, last_polled_at FROM ask_buddy_git_watch;"
```

To reset and reseed (e.g. after a PAT change):

```bash
docker exec -it askbuddy_postgres psql -U askbuddy -d askbuddy \
  -c "DELETE FROM ask_buddy_git_watch;"
```

### Write actions setup

The write tools (comment, label, assign, request reviewer, close/reopen, merge) are already in the agents but the read-only PAT will reject them with a clear error. To enable them:

1. Go to GitHub and edit the PAT
2. Change Issues and Pull requests permissions from **Read-only** to **Read and write**
3. Save — no bot restart needed, the token is read from the environment at call time

Close/reopen and merge always show a Slack confirmation prompt before executing. Comment, label, assign, and request-reviewer do not prompt — they're considered low enough risk to run directly.

### Digest schedule

```dotenv
# Once a day at 9 AM Pacific (default if GIT_DIGEST_TIMES is not set)
GIT_DIGEST_TIMES=9:00

# Twice a day
GIT_DIGEST_TIMES=9:00,17:00

# Weekdays only via cron
GIT_DIGEST_CRON=0 9 * * 1-5
GIT_DIGEST_TIMEZONE=America/New_York
```

---

## Feedback and analytics

Every answer AskBuddy posts has 👍/👎 buttons. Clicking 👎 opens a modal asking why (wrong info, no source, incomplete, other). Ratings are stored in `ask_buddy_feedback` with the question, answer text, sources cited, and which document chunks were retrieved.

Run the feedback report:

```bash
uv run python -m src.ask_buddy.feedback_report
```

Output includes total/positive/negative/unrated counts, top negatively-rated questions, low-quality chunk detection (chunks that have been 👎'd across multiple answers), and citation integrity (see below).

Run the weekly digest to a Slack channel:

```bash
uv run python -m src.ask_buddy.feedback_digest
```

Set `ASK_BUDDY_DIGEST_CHANNEL` in `.env` to a channel ID (not name) to make this work.

---

## Citation integrity

Every agent prompt forbids fabricating a filename, section, or date. That rule
is now **verified rather than trusted**: before each answer is posted, its
`Source(s):` block is parsed and every citation is checked against the source
metadata that actually exists in the corpus. The verdict is stored on the
answer's row in `ask_buddy_feedback.citation_status`:

| Verdict | Meaning |
|---|---|
| `ok` | Every citation resolves to real corpus metadata |
| `refusal` | A "No results found" answer — cites nothing by design |
| `missing_sources` | A non-refusal answer shipped with no `Source(s):` line |
| `unknown_file` | A cited document does not exist — the model invented it |
| `unknown_section` | The document exists but the cited heading does not |

The check never blocks a message — an answer the user is waiting on still gets
delivered — but a failure is logged at WARNING level and counted in the report:

```
  Citation integrity (verified at post time):
    ✅ ok                : 128
    ✅ refusal           : 14
    ❌ unknown_section   : 2
    → 142/144 checked answer(s) fully verified (98.6%)
```

Because the verdict is recorded at post time, it covers **every** answer —
including the ones nobody ever rates. Section comparison tolerates a
dropped or added heading number (`2. PTO Accrual Rates` vs `PTO Accrual
Rates`); an invented heading shares no text with a real one and is still
caught.

---

## Regression evals from real feedback

A 👎 in Slack can become a permanent test. The loop:

```
user 👎s an answer   →   ask_buddy_feedback
        ↓
eval_curate           →   a human says which document should have answered
        ↓
tests/regression_evals.json  (committed)
        ↓
pytest                →   retrieval must keep surfacing that source
```

Curate interactively — it pulls pending cases straight from the database and
offers the real corpus filenames to choose from, so you pick a document that
exists instead of typing one from memory:

```bash
uv run python -m src.ask_buddy.eval_curate
```

See what's waiting, without changing anything:

```bash
uv run python -m src.ask_buddy.eval_curate --list
```

Annotate or dismiss one case non-interactively:

```bash
uv run python -m src.ask_buddy.eval_curate --set "how much PTO after 5 years?" --sources pto_policy.md
uv run python -m src.ask_buddy.eval_curate --skip "some question not worth a test"
```

Then run the suite the curated file drives:

```bash
uv run pytest tests/test_ask_buddy.py -k FeedbackRegressionEvals -v
```

`tests/regression_evals.json` is committed, so each curated case is enforced
for everyone from then on. Curation is never lost: re-running the pull merges
new candidates without touching cases you've already annotated or skipped.

---

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | What it does | Needs |
|---|---|---|
| `lint` | `ruff check .` | nothing |
| `typecheck` | `mypy` over `src/ask_buddy` | `uv sync` |
| `fast-tests` | The stdlib-only suites — citations, eval curation, logging, lifecycle, rate limits, corpora (seconds) | nothing |
| `offline-tests` | `pytest -m "not integration"` — 247 tests | `uv sync` |
| `integration-tests` | Ingests both corpora into a pgvector service container, then `pytest -m integration`, including the curated regression evals | `GOOGLE_API_KEY` secret |

The integration job self-skips when no `GOOGLE_API_KEY` secret is set, so forks
and fresh clones stay green. Run the same checks locally:

```bash
uv run ruff check .
uv run pytest -m "not integration" -v
```

---

## Retrieval

AskBuddy uses Reciprocal Rank Fusion (RRF) to combine two independent search signals:

1. **Vector search** — embeds the query with `gemini-embedding-001`, does cosine similarity against `hr_chunks.embedding` using pgvector's HNSW index (top 20 results)
2. **Full-text search** — converts the query with PostgreSQL's `plainto_tsquery`, searches the GIN-indexed `bm25_tsvector` column (top 20 results)

The two lists are merged with RRF scoring (`1 / (60 + rank)`). Items appearing in both lists get a boost. Final ranking also applies a quality multiplier from feedback: chunks that have been 👍'd in past answers get ranked higher; chunks that have been 👎'd across many answers get ranked lower.

All retrieval is scoped by the `corpus` column so HR and IT results never mix, even though they share the same table.

---

## Environment variables reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GOOGLE_API_KEY` | Yes | — | Gemini embeddings + LLM |
| `SLACK_BOT_TOKEN` | Yes | — | Bot User OAuth Token (`xoxb-`) |
| `SLACK_APP_TOKEN` | Yes | — | App-Level Token for Socket Mode (`xapp-1-`) |
| `ASK_BUDDY_DB_DSN` | Yes | — | Postgres connection string |
| `AGENT_SETTING_CONFIG` | No | `openai` | LLM provider config (use `settings.google.toml` for Gemini) |
| `MODEL_NAME` | No | `gpt-4o` | Model name for the LLM |
| `ASK_BUDDY_FEWSHOT` | No | `0` | Number of top-rated past answers to inject as few-shot examples |
| `ASK_BUDDY_DIGEST_CHANNEL` | No | — | Channel ID for the weekly feedback digest |
| `ASK_BUDDY_LOG_FORMAT` | No | `text` | `json` for one object per line |
| `ASK_BUDDY_LOG_LEVEL` | No | `INFO` | Root log level |
| `ASK_BUDDY_SHUTDOWN_WAIT` | No | `10` | Seconds to drain in-flight answers on SIGTERM |
| `ASK_BUDDY_DB_POOL_MAX` | No | `10` | Max pooled Postgres connections |
| `ASK_BUDDY_MISFIRE_GRACE_SECONDS` | No | `3600` | How late a missed reminder may still fire |
| `ASK_BUDDY_RATE_LIMIT_PER_USER` | No | `0` | Requests per user per window (0 = off) |
| `ASK_BUDDY_RATE_LIMIT_WINDOW_SEC` | No | `60` | Rate-limit window length |
| `ASK_BUDDY_DAILY_REQUEST_CAP` | No | `0` | Total requests per UTC day (0 = off) |
| `ASK_BUDDY_SKIP_SLACK_VERIFY` | No | — | Skip `auth.test` at startup (tests/CI only) |
| `GITHUB_TOKEN` | No | — | Fine-grained PAT for GitHub access |
| `GITHUB_API_URL` | No | `https://api.github.com` | Override for GitHub Enterprise |
| `GIT_WATCH_REPOS` | No | — | Comma-separated `owner/repo` list to watch |
| `GIT_WATCH_CHANNEL` | No | — | Channel name or ID for triage posts and digests |
| `GIT_WATCH_INTERVAL_MINUTES` | No | `5` | How often to poll for new issues/PRs |
| `GIT_DIGEST_TIMES` | No | — | Daily digest times, e.g. `9:00,17:00` |
| `GIT_DIGEST_CRON` | No | `0 9 * * *` | Cron expression for digest (used if `GIT_DIGEST_TIMES` unset) |
| `GIT_DIGEST_TIMEZONE` | No | `America/Los_Angeles` | IANA timezone for digest schedule |

---

## File structure

```
.
├── data/
│   ├── hr_docs/synthetic/          HR policy markdown files
│   └── it_docs/                    IT security policy docs
│
├── src/ask_buddy/
│   ├── agent.py                    CugaSupervisor + all five sub-agents + tool factories
│   ├── db.py                       Schema init, connection helper, all DB helpers
│   ├── ingest.py                   Chunking, embedding, and corpus-aware ingestion
│   ├── retrieve.py                 hr_retrieve, it_retrieve @tools (vector + FTS + RRF)
│   ├── citations.py                Deterministic Source(s) validation (stdlib only)
│   ├── eval_curate.py              Turn 👎 feedback into committed regression tests
│   ├── slack_listener.py           Slack Bolt Socket Mode entry point
│   ├── scheduler.py                APScheduler-based reminder system
│   ├── feedback.py                 Block Kit feedback buttons and modals
│   ├── feedback_report.py          Analytics CLI (clustering, evals export)
│   ├── feedback_digest.py          Weekly Slack feedback digest
│   ├── github_client.py            GitHub REST v3 client (read + write)
│   ├── git_watch.py                Proactive triage watcher
│   └── git_digest.py               Scheduled repo state digest
│
├── tests/
│   ├── test_ask_buddy.py           Offline unit tests + integration test suite
│   ├── test_citations.py           Citation validator (no DB, no network)
│   ├── test_eval_curate.py         Eval curation logic (no DB, no network)
│   └── regression_evals.json       Curated regression cases (created by eval_curate)
│
├── .github/workflows/ci.yml        Lint + test pipeline
├── docker-compose.askbuddy.yml     pgvector/pg16 container definition
├── INSTALLATION.md                 Step-by-step setup guide
├── SLACK_PERMISSIONS.md            Slack app scopes and troubleshooting reference
├── .env.example                    All environment variables with comments
└── pyproject.toml                  Python dependencies
```

---

## Troubleshooting

**Bot starts but never replies to DMs**
Check `message.im` is subscribed under Event Subscriptions in the Slack app settings. Also check `im:history` and `im:read` scopes are granted and the app has been reinstalled after adding them.

**`/askbuddy` returns "app did not respond"**
The `commands` scope is missing or the app wasn't reinstalled after it was added. Also make sure the slash command is registered in the Slack app settings with *something* in the Request URL field (Socket Mode ignores it but Slack won't save without it).

**Feedback buttons do nothing**
Interactivity is not enabled. Go to Settings → Interactivity & Shortcuts → toggle On.

**Triage watcher logs "disabled" on startup**
One of `GITHUB_TOKEN`, `GIT_WATCH_REPOS`, or `GIT_WATCH_CHANNEL` is missing or blank. Check for inline comments in `.env` — `GIT_WATCH_CHANNEL=github-alerts  # my channel` will include the comment text as part of the value.

**Triage posts never arrive in channel**
The bot needs to be a member of the channel. Run `/invite @AskBuddy` in that channel. For private channels, use the channel ID in `GIT_WATCH_CHANNEL` instead of the name.

**"GitHub auth/permission error" in logs**
PAT scopes are wrong, or the PAT has expired. Regenerate at GitHub → Settings → Developer settings → Fine-grained tokens.

**"No results found" for questions that should have answers**
The corpus for that domain hasn't been ingested, or was ingested with a different `--corpus` tag than the agent expects. Re-run the ingest command for the relevant corpus.

**`connection refused` on port 5432**
The Docker container isn't running. Run `docker compose -f docker-compose.askbuddy.yml up -d` and wait for the health check to pass.
