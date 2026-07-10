# Ask Buddy — Installation Guide

Complete step-by-step guide to install, configure, and run Ask Buddy from
scratch on a new machine. Estimated time: **20–30 minutes**.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone and install dependencies](#2-clone-and-install-dependencies)
3. [Create the Slack app](#3-create-the-slack-app)
4. [Get a Google API key](#4-get-a-google-api-key)
5. [Configure environment variables](#5-configure-environment-variables)
6. [Start the database](#6-start-the-database)
7. [Ingest the HR documents](#7-ingest-the-hr-documents)
8. [Run the tests](#8-run-the-tests)
9. [Start the bot](#9-start-the-bot)
10. [Verify it works](#10-verify-it-works)
11. [Run the feedback report](#11-run-the-feedback-report)
12. [Stopping and restarting](#12-stopping-and-restarting)
13. [Troubleshooting](#13-troubleshooting)
14. [Adding your own HR documents](#14-adding-your-own-hr-documents)

---

## 1. Prerequisites

Install these before starting. Verify each one with the command shown.

| Requirement | Min version | Check |
|---|---|---|
| **Python** | 3.13 | `python3 --version` |
| **uv** (package manager) | any | `uv --version` |
| **Docker Desktop** | any | `docker info` |
| **Git** | any | `git --version` |

### Install uv (if not installed)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Install Docker Desktop (Mac)

Download from **https://www.docker.com/products/docker-desktop/**  
→ Choose **Apple Silicon** if you are on an M1/M2/M3 Mac  
→ Drag to Applications, open it, wait for the whale icon to stop animating  
→ Confirm: `docker info` should print a server version, no errors

---

## 2. Clone and Install Dependencies

```bash
git clone <your-repo-url> meeting-scribe
cd meeting-scribe

# Install all Python dependencies into a local virtual environment
uv sync
```

Expected output ends with:
```
Installed N packages in Xs
```

No manual `pip install` is needed — `uv sync` reads `pyproject.toml` and
installs everything including `cuga`, `langchain-google-genai`,
`slack-bolt`, `psycopg2-binary`, and `pgvector`.

---

## 3. Create the Slack App

You need **admin access** (or permission to install apps) on your Slack
workspace. This section walks through it step by step; for a condensed
reference (just the scope table, tokens, and a troubleshooting checklist),
see **[SLACK_PERMISSIONS.md](SLACK_PERMISSIONS.md)**.

### 3a — Create the app

1. Go to **https://api.slack.com/apps**
2. Click **Create New App** → **From scratch**
3. App Name: `Ask Buddy`
4. Pick your workspace → **Create App**

### 3b — Add bot token scopes

1. In the left sidebar click **OAuth & Permissions**
2. Scroll to **Bot Token Scopes** → click **Add an OAuth Scope** for each:

   | Scope | Purpose |
   |---|---|
   | `chat:write` | Post messages and update them |
   | `chat:write.public` | Post in public channels without joining |
   | `im:history` | Read DM history (receive messages) |
   | `im:read` | List DMs |
   | `im:write` | Open DM conversations |
   | `app_mentions:read` | Receive @mentions in channels |
   | `channels:history` | Read channel history for @mentions |
   | `users:read` | Look up user info |
   | `commands` | Required for the `/askbuddy` slash command |

3. Scroll up and click **Install to Workspace** → **Allow**
4. Copy the **Bot User OAuth Token** (starts with `xoxb-`) — you need this later

### 3c — Enable Socket Mode

1. Click **Socket Mode** in the left sidebar
2. Toggle **Enable Socket Mode** → **On**
3. Under **App-Level Tokens** click **Generate Token and Scopes**
   - Token name: `askbuddy-socket`
   - Add scope: `connections:write`
   - Click **Generate**
4. Copy the **App-Level Token** (starts with `xapp-`) — you need this later

### 3d — Subscribe to events

1. Click **Event Subscriptions** in the left sidebar
2. Toggle **Enable Events** → **On**
3. Under **Subscribe to bot events** → click **Add Bot User Event**:
   - Add `message.im` (receive DMs)
   - Add `app_mention` (receive @mentions in channels)
4. Click **Save Changes**

### 3e — Enable Interactivity (required for feedback buttons)

1. Click **Interactivity & Shortcuts** in the left sidebar
2. Toggle **Interactivity** → **On**
3. In the **Request URL** field enter `https://example.com` (placeholder — Socket Mode doesn't use this URL)
4. Click **Save Changes**

### 3f — Add the /askbuddy slash command

1. Click **Slash Commands** in the left sidebar → **Create New Command**
2. Command: `/askbuddy`
3. Short Description: `Ask Ask Buddy an HR question`
4. Request URL: `https://example.com` (placeholder — Socket Mode doesn't use this URL)
5. Click **Save**

### 3g — Reinstall the app

After changing scopes, Slack requires a reinstall:

1. Click **OAuth & Permissions** → **Reinstall to Workspace** → **Allow**
2. Re-copy the **Bot User OAuth Token** if it changed

---

## 4. Get a Google API Key

Ask Buddy uses **Google Gemini** for both embeddings and the LLM.

1. Go to **https://aistudio.google.com/apikey**
2. Click **Create API key** → **Create API key in new project**
3. Copy the key (starts with `AI...`)

> **Free tier limits:** The Google AI Studio free tier allows ~1,500 requests/day
> and ~15 requests/minute. For production use, enable billing in
> [Google Cloud Console](https://console.cloud.google.com) to remove these limits.

---

## 5. Configure Environment Variables

```bash
# Copy the template
cp .env.example .env

# Open in your editor
open -e .env       # macOS TextEdit
# or: code .env   # VS Code
# or: nano .env   # terminal
```

Edit `.env` and set these values (replace everything after `=`):

```dotenv
# ── Google Gemini (LLM + embeddings) ─────────────────────────────────────
GOOGLE_API_KEY=AIza...your-key-here...
AGENT_SETTING_CONFIG=settings.google.toml
MODEL_NAME=gemini-2.5-flash

# ── Slack ─────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN=xoxb-...your-bot-token...
SLACK_APP_TOKEN=xapp-1-...your-app-token...

# ── Database (matches docker-compose.askbuddy.yml exactly) ───────────────
ASK_BUDDY_DB_DSN=postgresql://askbuddy:askbuddy_secret@localhost:5432/askbuddy
```

**Token format check:**
- `SLACK_BOT_TOKEN` must start with `xoxb-` — found in *OAuth & Permissions*
- `SLACK_APP_TOKEN` must start with `xapp-1-` — found in *Basic Info → App-Level Tokens*

Save the file. **Never commit `.env` to git** — it is already in `.gitignore`.

---

## 6. Start the Database

Ask Buddy uses **PostgreSQL 16 with the pgvector extension** running in Docker.

```bash
docker compose -f docker-compose.askbuddy.yml up -d
```

Wait ~10 seconds for it to become healthy:

```bash
docker compose -f docker-compose.askbuddy.yml ps
```

Expected output:
```
NAME                STATUS
askbuddy_postgres   Up X seconds (healthy)
```

If it shows `(health: starting)` wait a few more seconds and run again.

> The database stores its data in a Docker volume (`askbuddy_pgdata`) so it
> persists across container restarts. Your ingested HR documents are not lost
> when you stop and restart the container.

---

## 7. Ingest the HR Documents

This step chunks the 8 synthetic HR policy files, generates embeddings
with Google's `gemini-embedding-001` model, and stores everything in
Postgres. It takes about 2–3 minutes and costs a few API calls.

```bash
uv run python -m src.ask_buddy.ingest --clear
```

Expected output:
```
Schema initialised.
hr_chunks table cleared.
  benefits_enrollment.md: 11 sections, effective_date=2023-11-01
  code_of_conduct.md: 8 sections, effective_date=2023-06-01
  expense_reimbursement.md: 9 sections, effective_date=2023-04-01
  it_security_policy.md: 10 sections, effective_date=2024-01-15
  parental_leave.md: 9 sections, effective_date=2024-03-01
  performance_reviews.md: 8 sections, effective_date=2023-01-01
  pto_policy.md: 7 sections, effective_date=2024-01-01
  remote_work_policy.md: 8 sections, effective_date=2023-09-01

Embedding 70 chunks in batches of 32…
  embedded 32/70
  embedded 64/70
  embedded 70/70
Inserting into database…
Done. 70 chunks stored.
```

After ingestion, delete the IT security policy chunks (they are included for
testing but should not be in the live HR corpus):

```bash
docker exec askbuddy_postgres psql -U askbuddy -d askbuddy \
  -c "DELETE FROM hr_chunks WHERE source_filename = 'it_security_policy.md';"
```

Confirm 60 chunks remain:

```bash
docker exec askbuddy_postgres psql -U askbuddy -d askbuddy \
  -c "SELECT source_filename, COUNT(*) FROM hr_chunks GROUP BY 1 ORDER BY 1;"
```

---

## 8. Run the Tests

Verify the installation before starting the bot:

```bash
uv run pytest tests/test_ask_buddy.py -v
```

Expected result: **17 passed** (or 16 passed + 1 skipped for
`TestAgentMockedSlack` if the Gemini free-tier rate limit was just hit —
wait 1 minute and retry).

If integration tests are skipping (showing as `SKIPPED` rather than running),
check that `GOOGLE_API_KEY` and `ASK_BUDDY_DB_DSN` are set in `.env` and
that Docker is running.

---

## 9. Start the Bot

```bash
uv run python -m src.ask_buddy.slack_listener
```

You should see:
```
2024-XX-XX [INFO] ask_buddy.slack: ask_buddy_feedback table ready.
2024-XX-XX [INFO] ask_buddy.slack: Starting Ask Buddy in Socket Mode…
⚡️ Bolt app is running!
```

The bot is now live. Leave this terminal running — the bot stops when you
close it. For a persistent deployment, see [Stopping and restarting](#12-stopping-and-restarting).

---

## 10. Verify It Works

Open Slack and find **Ask Buddy** in your Apps sidebar. Send it a direct message.

### Test 0 — Slash command in any channel

Type in any channel Ask Buddy has been added to:

> **`/askbuddy how many days of PTO do I get after 5 years of service?`**

Expected: an ephemeral "⏳ Looking that up…" acknowledgement, followed by the
answer posted in the channel with the usual Source(s) line and feedback buttons.

### Test 1 — HR question with citation (should answer)

> **"How many days of PTO do I get after 5 years of service?"**

Expected response:
```
Employees with 5–10 years of service receive 160 hours (20 days) of PTO
per year, accrued over 26 pay periods.

Source(s): pto_policy.md — 2. PTO Accrual Rates (effective 2024-01-01)
──────────────────────────────────────
Was this helpful?   [👍 Helpful]  [👎 Not helpful]
```

Click 👍 — the buttons should be replaced with `✅ Thanks for the feedback!`

### Test 2 — Out-of-scope question (should refuse)

> **"What is the company's password rotation policy and VPN requirements?"**

Expected response:
```
No results found in our HR documents for that question — please reach out
to HR or your manager for help.
```

No source citations. No hallucinated answer.

### Test 3 — Cross-document question

> **"How do I add my newborn to health insurance after parental leave?"**

Expected: answer drawing on both `parental_leave.md` and
`benefits_enrollment.md`, with both cited in `Source(s):`.

### Test 4 — Old vs. new PTO policy

> **"What were the PTO accrual rates before 2024?"**

Expected: answer from the archived v1.0 section with `effective 2022-06-01` cited.

---

## 11. Run the Feedback Report

After collecting some feedback from real users:

```bash
uv run python -m src.ask_buddy.feedback_report
```

Output:
```
============================================================
  Ask Buddy — Feedback Report
============================================================
  Total responses  : 12
  👍  Positive      : 9   (75.0%)
  👎  Negative      : 2   (16.7%)
  —   Unrated       : 1

  Top negatively-rated questions:
    [2x] How do I claim expenses for a home office monitor?
    [1x] What is the parental leave for secondary caregivers?
  ...
```

The report now also breaks out:
- **Refusals** — how many "No results found" answers were 👎'd (users wanted
  an answer). These are your strongest doc-gap signals.
- **👎 reasons** — the category each thumbs-down came with (from the modal:
  wrong info / no source / off-topic / unclear / other).
- **Content-review queue** — chunks that accumulated 3+ thumbs-down across the
  answers that cited them; the underlying HR text may be wrong or ambiguous.
- **Negative rate by agent config** — a built-in A/B view if you run different
  model configs (see the per-node model split in README_ASKBUDDY.md).

Optional flags:
```bash
# Cluster negatively-rated questions by meaning (uses the embedder):
uv run python -m src.ask_buddy.feedback_report --cluster

# Export negatively-rated questions as regression eval candidates:
uv run python -m src.ask_buddy.feedback_report --export-evals tests/regression_evals.json
```
After exporting, fill in `expected_sources` for questions that *should* be
answerable, commit the file, and `TestFeedbackRegressionEvals` will assert
retrieval keeps surfacing those sources so a future change can't silently regress.

### Weekly digest to Slack (optional)

Instead of running the report by hand, post a weekly negative-feedback +
doc-gap digest to a Slack channel.

**1. Get the channel ID.** Channel IDs look like `C0123ABCD` — they are
**not** the channel name. Two ways to find one:
- In Slack, open the target channel → click the channel name at the top →
  **About** tab → scroll to the bottom, or
- Right-click the channel in the sidebar → **Copy link** → the ID is the
  last segment of the URL, e.g. `https://yourteam.slack.com/archives/C0123ABCD`.

**2. Invite the bot to that channel** (required to post there):
```
/invite @Ask Buddy
```

**3. Add the ID to `.env`.** Open `.env` (not `.env.example`) and set:
```dotenv
ASK_BUDDY_DIGEST_CHANNEL=C0123ABCD
```
This line already exists as a commented-out placeholder in `.env.example`
under the *Ask Buddy — HR RAG Bot for Slack* section — uncomment it in your
own `.env` and replace the value with the real channel ID from step 1.

**4. Run it:**
```bash
# Preview without posting:
uv run python -m src.ask_buddy.feedback_digest --dry-run

# Post to the configured channel:
uv run python -m src.ask_buddy.feedback_digest
```
If `ASK_BUDDY_DIGEST_CHANNEL` is missing, the command exits with an error
telling you to set it (or pass `--dry-run` instead).

Schedule it with cron (Mondays 09:00 shown) — the bot itself does not schedule it:
```cron
0 9 * * 1  cd /path/to/AskBuddy && uv run python -m src.ask_buddy.feedback_digest
```

> **Note on the 👎 modal:** thumbs-down now opens a short "what was wrong?"
> modal before recording. This needs **Interactivity** enabled in the Slack app
> (already required for feedback buttons — no new scope). If Interactivity is
> off, the bot falls back to recording a bare thumbs-down.

> **Schema note:** the new feedback columns are added automatically on bot
> startup via an idempotent migration — no manual SQL needed when upgrading an
> existing database.

---

## 12. Stopping and Restarting

### Stop the bot
Press `Ctrl+C` in the terminal where the listener is running.

### Stop the database
```bash
docker compose -f docker-compose.askbuddy.yml down
```

Data is preserved in the Docker volume. To also delete all data:
```bash
docker compose -f docker-compose.askbuddy.yml down -v
```

### Restart everything from scratch
```bash
# 1. Start DB
docker compose -f docker-compose.askbuddy.yml up -d

# 2. Start bot (no re-ingest needed — data persists)
uv run python -m src.ask_buddy.slack_listener
```

### Run as a background process (macOS)
To keep the bot running after closing the terminal, use `nohup`:
```bash
nohup uv run python -m src.ask_buddy.slack_listener > askbuddy.log 2>&1 &
echo "Bot PID: $!"
```

To stop it:
```bash
pkill -f slack_listener
```

---

## 13. Troubleshooting

### "ERROR: Required environment variable 'X' is not set"
Open `.env` and check the named variable is present and not commented out.
Variable names are case-sensitive. No spaces around the `=` sign.

### "⚡️ Bolt app is running!" but no reply in Slack
1. Check **Event Subscriptions** at api.slack.com/apps → Ask Buddy →
   `message.im` must be listed under *Subscribe to bot events*
2. Check **Interactivity** is toggled **On**
3. Reinstall the app: *OAuth & Permissions → Reinstall to Workspace*
4. Restart the listener after reinstalling

### Bot posts "⏳ Looking…" but never replies
Multiple listener processes may be running. Kill them all and restart:
```bash
pkill -f slack_listener
sleep 1
uv run python -m src.ask_buddy.slack_listener
```

### "429 RESOURCE_EXHAUSTED" from Google API
The free-tier daily or per-minute quota is exhausted. Options:
- Wait 1 minute (per-minute limit resets)
- Wait until midnight Pacific (daily limit resets)
- Enable billing on your Google Cloud project to remove free-tier limits
- Switch to a different Gemini model: edit `MODEL_NAME` in `.env`
  (available: `gemini-2.5-flash`, `gemini-2.0-flash`, `gemini-2.0-flash-lite`)

### "connection refused" on port 5432
The Docker container isn't running. Start it:
```bash
docker compose -f docker-compose.askbuddy.yml up -d
```

### Feedback buttons do nothing
**Interactivity** is not enabled in the Slack app. Go to:
*api.slack.com/apps → Ask Buddy → Interactivity & Shortcuts → toggle On → Save*

### Re-ingest after editing documents
The ingest pipeline is incremental by default — it hashes each file's
content and skips any file that hasn't changed since the last run, so
running it plainly after editing one document only re-chunks and
re-embeds that document:
```bash
uv run python -m src.ask_buddy.ingest
```
Expected output for an unrelated unchanged file:
```
  pto_policy.md: unchanged, skipping
  remote_work_policy.md: 8 sections, effective_date=2023-09-01
...
Done. 9 chunks stored across 1 file(s) (7 unchanged file(s) skipped).
```
Deleting a `.md` file from `data/hr_docs/synthetic/` and re-running also
removes its chunks from `hr_chunks` automatically — no manual cleanup needed.

Only pass `--clear` when you want to force a full rebuild (e.g. after
changing the chunking logic itself) — it wipes `hr_chunks` and re-embeds
every file regardless of whether it changed:
```bash
uv run python -m src.ask_buddy.ingest --clear

# Then remove IT security again (only needed after --clear)
docker exec askbuddy_postgres psql -U askbuddy -d askbuddy \
  -c "DELETE FROM hr_chunks WHERE source_filename = 'it_security_policy.md';"
```

---

## 14. Adding Your Own HR Documents

1. Add your `.md` files to `data/hr_docs/synthetic/`

2. Each file should have a header like:
   ```markdown
   # Policy Title
   **Effective Date:** YYYY-MM-DD
   ```

3. Re-ingest — only new/changed files are processed, everything else is skipped:
   ```bash
   uv run python -m src.ask_buddy.ingest
   ```

4. The bot picks up the new content immediately — no restart needed.

For non-Markdown documents (PDF, DOCX), convert them to Markdown first
or extend `ingest.py` to use a parser like `pypdf` or `python-docx`.

---

## File Structure Reference

```
meeting-scribe/
├── data/hr_docs/synthetic/      8 HR policy markdown files
│   ├── pto_policy.md            (two versions — tests date-specific queries)
│   ├── parental_leave.md        (references benefits_enrollment.md)
│   ├── benefits_enrollment.md
│   ├── remote_work_policy.md
│   ├── expense_reimbursement.md
│   ├── performance_reviews.md
│   ├── code_of_conduct.md
│   └── it_security_policy.md    (out-of-scope doc — deleted from DB after ingest)
│
├── src/ask_buddy/
│   ├── __init__.py
│   ├── db.py                    Schema init, connection helper
│   ├── ingest.py                Chunk → embed → store pipeline
│   ├── retrieve.py              hybrid_retrieve tool (vector + FTS + RRF)
│   ├── agent.py                 CugaAgent + system prompt
│   ├── feedback.py              Block Kit builder, 👎 modal, DB feedback helpers
│   ├── feedback_report.py       Analytics CLI (refusals, reasons, clustering, evals)
│   ├── feedback_digest.py       Weekly digest → Slack channel
│   └── slack_listener.py        Slack Bolt Socket Mode listener (+ /askbuddy, modal)
│
├── tests/
│   ├── conftest.py              Loads .env before test collection
│   └── test_ask_buddy.py        17 tests (offline + integration)
│
├── docker-compose.askbuddy.yml  pgvector/pg16 container
├── .env.example                 Environment variable template
├── pyproject.toml               Python dependencies
├── README_ASKBUDDY.md           Architecture overview
└── INSTALLATION.md              ← this file
```
