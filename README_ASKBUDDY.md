# Ask Buddy — Multi-Domain Agentic RAG Bot for Slack

Ask Buddy is a Slack bot that answers HR and IT policy questions from
your ingested document corpora using hybrid search (vector + keyword) and
a CugaSupervisor that routes queries to domain-specific sub-agents. It
always cites the source document and section, and says *"No results
found"* rather than hallucinating.

```
Slack DM / @mention / /askbuddy
      │
      ▼
Slack Bolt (Socket Mode)
      │
      ▼
CugaSupervisor ──── routes by topic
      │
      ├── hr_agent  ──── hr_retrieve  (corpus='hr')
      │                       │
      └── it_agent  ──── it_retrieve  (corpus='it')
                              │
                       hr_chunks table (pgvector/pg16)
                       (corpus column scopes retrieval)
                              │
post_slack_message  ──▶  Slack
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | `uv` strongly recommended |
| Docker | for the pgvector Postgres container |
| Google API key | Gemini embeddings + LLM reasoning |
| Slack workspace (admin) | to create the bot app |

---

## 1. Setup Order

Follow these steps **in order**.

### Step 1 — Clone and install dependencies

```bash
# From the repo root
uv sync
```

All new Ask Buddy dependencies (`openai`, `psycopg2-binary`, `pgvector`,
`slack-bolt`, `python-dotenv`, `rank-bm25`) are already listed in
`pyproject.toml`.

### Step 2 — Start Postgres + pgvector

```bash
docker compose -f docker-compose.askbuddy.yml up -d
```

Wait for the healthcheck to pass (about 5–10 seconds):

```bash
docker compose -f docker-compose.askbuddy.yml ps
# STATUS should show "healthy"
```

### Step 3 — Create your Slack App

See **[SLACK_PERMISSIONS.md](SLACK_PERMISSIONS.md)** for the complete,
up-to-date walkthrough: which Bot Token Scopes to add, Socket Mode + the
App-Level Token, Event Subscriptions, Interactivity (needed for the feedback
buttons and the 👎 reason modal), and registering the `/askbuddy` slash
command — plus a troubleshooting table for the most common permission errors.

### Step 4 — Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` — the Ask Buddy section:

```dotenv
OPENAI_API_KEY=sk-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
ASK_BUDDY_DB_DSN=postgresql://askbuddy:askbuddy_secret@localhost:5432/askbuddy
```

The `AGENT_SETTING_CONFIG` and `MODEL_NAME` values for the CUGA LLM are also
read from `.env` (defaults to `openai` / `gpt-4o`).

### Step 5 — Ingest the document corpora

Ingest each corpus separately. The `--corpus` flag tags every chunk so
retrieval stays scoped to the right domain.

```bash
# HR documents (default)
uv run python -m src.ask_buddy.ingest --clear

# IT security documents
uv run python -m src.ask_buddy.ingest --corpus it --clear
```

This will:
- Create the `hr_chunks` table and pgvector + GIN indexes (first run only).
- Chunk all `*.md` files in the corpus directory.
- Embed each chunk using `gemini-embedding-001`.
- Store chunks + embeddings + full-text tsvector + corpus tag in Postgres.

Default directories: `data/hr_docs/synthetic/` for HR, `data/it_docs/` for IT.
Override with `--docs-dir PATH`.

To re-ingest after editing a document, pass `--clear` to replace all chunks
for that corpus (other corpora are untouched).

### Step 6 — Start the bot

```bash
uv run python -m src.ask_buddy.slack_listener
```

You should see:

```
Starting Ask Buddy in Socket Mode…
⚡️ Bolt app is running!
```

The bot is now live. Send it a DM or @mention it in a channel.

---

## 2. Running the Tests

```bash
# Offline unit tests only (no DB, no OpenAI key needed)
uv run pytest tests/test_ask_buddy.py -v -m "not integration"

# Full suite including integration tests (requires DB + OPENAI_API_KEY)
uv run pytest tests/test_ask_buddy.py -v
```

---

## 3. Test Case Verification

### TC-1 — Clear single-doc question

**Send in Slack DM:**
> How many PTO days do I get after 5 years of service?

**Expected response:**
```
Employees with 5–10 years of service receive 160 hours (20 days) of PTO per year,
accrued over 26 pay periods.

Source(s): pto_policy.md — PTO Accrual Rates (effective 2024-01-01)
```

✅ Pass criteria: correct accrual figure, `pto_policy.md` cited, date present.

---

### TC-2 — Cross-document question (parental leave → benefits enrollment)

**Send in Slack DM:**
> How do I add my newborn to my health insurance after parental leave?

**Expected response:** The answer should reference:
- The 30-day qualifying life event window (from `parental_leave.md` and/or `benefits_enrollment.md`)
- Both source documents cited.

✅ Pass criteria: at least one of `parental_leave.md`, `benefits_enrollment.md`
   appears in Sources; ideally both if the agent drew on both.

---

### TC-3 — Vague question triggering reformulation-retry

**Send in Slack DM:**
> What do I get when I have a baby?

The first query may return mixed results. The agent should reformulate (e.g.
"parental leave benefits newborn") before answering.

✅ Pass criteria: final answer cites `parental_leave.md` with correct
   leave duration (16 weeks paid for primary caregiver).

---

### TC-4 — Date-specific PTO query

**TC-4a — Current version:**
> What is the current PTO accrual table?

Expected: v2.0 table (80/120/160/200 hours), effective 2024-01-01 cited.

**TC-4b — Old version:**
> What were the PTO accrual rates before 2024, under the old policy?

Expected: v1.0 table (64/96/136 hours), mentions "superseded", effective 2022-06-01.

✅ Pass criteria: correct table for each query; effective date in Sources.

---

### TC-5 — Out-of-scope IT security query (must refuse)

**Send in Slack DM:**
> What is the company's password rotation policy and VPN requirements?

**Expected response:**
```
No results found in our HR documents for that question — please reach out to HR or your manager for help.
```

✅ Pass criteria: **exact** refusal string. No PTO/parental leave blended in.
   No fabricated HR sources. No partial answer.

---

### TC-6 — Live Slack round-trips

For at least TC-1 and TC-5:

1. Open a DM with **Ask Buddy** in your Slack workspace.
2. Type the message from the test case.
3. Verify the bot posts the expected response (with or without the "thinking" message first).
4. Check `logs` output to confirm `hybrid_retrieve` was called and returned results.

---

## 4. Architecture Notes

### CugaSupervisor Routing

```
user query
     │
CugaSupervisor ── "HR or IT?"
     │
     ├── hr_agent  → hr_retrieve (corpus='hr')
     └── it_agent  → it_retrieve (corpus='it')
```

The supervisor inspects the query topic and delegates to the right sub-agent.
Cross-domain questions are routed to both. Out-of-scope queries are refused
directly by the supervisor without calling either agent.

### Hybrid Retrieval (RRF)

```
query
  ├─► gemini-embedding-001     ──► pgvector cosine search (top 20)  ──► list A
  └─► plainto_tsquery          ──► GIN full-text search   (top 20)  ──► list B
                                                                          │
                              RRF merge: score = Σ 1/(60 + rank_i)  ◄────┘
                                                                          │
                              corpus filter on hr_chunks.corpus      ◄────┘
                                                                          │
                              Top-k chunks returned to agent         ◄────┘
```

Each retrieval tool (`hr_retrieve`, `it_retrieve`) passes its corpus tag to
`_vector_search` and `_keyword_search`, so results never leak across domains.

### Agent Guardrails

- `enable_knowledge=False` — disables CUGA's built-in RAG so agents cannot
  answer from any source other than their retrieval tool.
- Each sub-agent's system prompt constrains it to its domain and forbids
  general-knowledge answers, fabricated sources, and blending partial guesses
  with the "No results found" message.
- Tool lists are strictly `[<domain>_retrieve, post_slack_message]` — no web search.

### Effective Date Handling

Each chunk is tagged with the `effective_date` parsed from the document header.
Both PTO policy versions (2022-06-01 and 2024-01-01) are stored. Agents are
instructed to prefer the latest effective_date unless the query explicitly asks
about a past version.

---

## 5. File Structure

```
.
├── data/
│   ├── hr_docs/synthetic/        7 HR policy markdown files
│   └── it_docs/                  IT security policy docs
├── docker-compose.askbuddy.yml   pgvector/pg16 container
├── src/ask_buddy/
│   ├── __init__.py
│   ├── db.py                     schema init, connection helper
│   ├── ingest.py                 corpus-aware chunking + embedding
│   ├── retrieve.py               hr_retrieve, it_retrieve @tools (vector+FTS+RRF)
│   ├── agent.py                  CugaSupervisor + domain sub-agents
│   ├── feedback.py               Block Kit feedback buttons + modals
│   ├── feedback_report.py        analytics CLI (clustering, evals)
│   ├── feedback_digest.py        weekly Slack digest
│   └── slack_listener.py         Slack Bolt Socket Mode listener
├── tests/
│   └── test_ask_buddy.py         offline + integration test cases
├── SLACK_PERMISSIONS.md          Slack app setup reference
├── INSTALLATION.md               detailed setup guide
├── .env.example                  environment variable template
└── pyproject.toml                project dependencies
```
