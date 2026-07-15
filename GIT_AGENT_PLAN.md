# Ask Buddy — Git Agent Implementation Plan (v1)

> **Status:** design/plan only. No code has been written yet. This document is
> written to be executed step-by-step by an implementer (human or a less-capable
> model). Follow the phases **in order**. Every file, function signature, SQL
> statement, env var, and prompt is spelled out. Do not improvise names.

---

## 0. Decisions locked in (do not re-litigate)

| Decision | Choice | Rationale |
|---|---|---|
| **Scope of v1** | **Read-only Q&A** + **Proactive triage** | No write actions (no create/close/merge). Lowest risk, minimal token scopes. |
| **Provider** | **GitHub** (`api.github.com`) | Free API, 5,000 req/hr authenticated. |
| **Auth** | **Fine-grained Personal Access Token (PAT)** | One env var, 5-min setup, per-repo read-only scoping. |
| **Triage trigger** | **Polling** (reuse APScheduler, like `scheduler.py`) | No public URL / webhook infra needed. |
| **Architecture** | Existing `CugaSupervisor` = **thinker**; add two git **slaves**: `git_issue_agent`, `git_pr_agent` | `CugaSupervisor` cannot nest another supervisor (see below). Two slaves = clean issues-vs-PRs split, matches the existing `hr_agent`/`it_agent`/`scheduler_agent` pattern. |

### Why not a nested "git sub-supervisor"?

`CugaSupervisor.__init__` in `.venv/.../cuga/sdk.py` (v0.2.20) takes
`agents: Dict[str, Union[CugaAgent, Dict[str, Any]]]`. A sub-agent is either a
`CugaAgent` **or** an external A2A dict — **never another `CugaSupervisor`**.
So we realize "thinker + slaves" as:

```
Slack (DM / @mention / /askbuddy)
      │
      ▼
CugaSupervisor  ← the THINKER (routes by topic)
      ├── hr_agent          (existing)
      ├── it_agent          (existing)
      ├── scheduler_agent   (existing)
      ├── git_issue_agent   ← NEW slave (GitHub issues, read-only)
      └── git_pr_agent      ← NEW slave (GitHub pull requests, read-only)

Background (no Slack message needed to start):
  APScheduler ──every N min──► git_watch poll ──► summarize new issues/PRs ──► Slack channel
```

> If you later decide two git slaves is too many, folding them into one
> `git_agent` with all read tools is a valid simplification — but v1 ships two.

---

## 1. GitHub setup (do this first, outside the code)

### 1.1 Create a fine-grained PAT (read-only)

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Token name:** `askbuddy-git-agent`. **Expiration:** 90 days (rotate later).
3. **Resource owner:** the org/user that owns the repos Ask Buddy will read.
4. **Repository access:** *Only select repositories* → pick the repos to expose.
5. **Permissions** (Repository permissions — set each to **Read-only**):
   - **Metadata** → Read-only *(mandatory; GitHub forces this on)*
   - **Issues** → Read-only
   - **Pull requests** → Read-only
   - **Contents** → Read-only *(optional; only if you later read file contents)*
   - **Commit statuses / Checks** → Read-only *(needed for PR check status)*
6. Generate, copy the token (starts with `github_pat_...`). You cannot see it again.

> **Rate limit:** authenticated = 5,000 requests/hour. Polling every 5 min across
> a handful of repos uses a few dozen requests/hour — nowhere near the ceiling.

### 1.2 Environment variables (add to `.env` and `.env.example`)

```dotenv
# ── Git Agent ────────────────────────────────────────────────
# Fine-grained PAT, read-only scopes (Issues/PRs/Metadata).
GITHUB_TOKEN=github_pat_...

# Base API URL. Leave as-is for github.com; change only for GitHub Enterprise.
GITHUB_API_URL=https://api.github.com

# Proactive triage config (comma-separated "owner/repo" list). Empty = triage off.
GIT_WATCH_REPOS=acme-corp/backend,acme-corp/frontend

# Slack channel (name or ID) where triage summaries are posted.
GIT_WATCH_CHANNEL=eng-triage

# Poll interval in minutes for the triage watcher.
GIT_WATCH_INTERVAL_MINUTES=5
```

> If `GITHUB_TOKEN` is unset, the git slaves must degrade gracefully (return a
> clear "GitHub is not configured" tool result) and the watcher must not start.
> If `GIT_WATCH_REPOS` is empty, the watcher must not start.

---

## 2. Dependencies

Add **one** dependency to `pyproject.toml` under `dependencies`:

```toml
    "httpx>=0.27.0",
```

Then run:

```bash
uv sync
```

> We use a thin `httpx` REST client rather than `PyGithub` to (a) keep the
> dependency surface small, (b) match the repo's existing "raw helper functions +
> `@tool` wrapper" pattern (see `retrieve.py`, `db.py`), and (c) make offline
> unit testing trivial (mock one `_request` function).

---

## 3. New file: `src/ask_buddy/github_client.py`

A thin, testable GitHub REST v3 wrapper. **All functions are read-only.**
Every public function returns plain Python dicts/lists (already trimmed to the
fields we care about) or raises `GitHubError`.

### 3.1 Module skeleton (implement exactly these signatures)

```python
"""
Thin read-only GitHub REST client for Ask Buddy's git slaves and triage watcher.

Only GET endpoints. Authenticates with a fine-grained PAT (GITHUB_TOKEN).
Every function returns trimmed plain dicts/lists, or raises GitHubError.

Endpoints used (all documented, all free within the 5,000 req/hr limit):
  GET /repos/{owner}/{repo}/issues       (list; note: includes PRs — we filter)
  GET /repos/{owner}/{repo}/issues/{n}   (single issue)
  GET /repos/{owner}/{repo}/pulls        (list PRs)
  GET /repos/{owner}/{repo}/pulls/{n}    (single PR)
  GET /repos/{owner}/{repo}/pulls/{n}/reviews
  GET /repos/{owner}/{repo}/commits/{ref}/check-runs
  GET /search/issues                     (cross-repo search)
  GET /rate_limit                        (diagnostics)
"""

from __future__ import annotations
import os
from typing import Any
import httpx

API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT = 15.0
MAX_LIST = 30          # cap list results so tool output stays small for the LLM


class GitHubError(RuntimeError):
    """Raised on non-2xx responses or missing configuration."""


def _base_url() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise GitHubError(
            "GITHUB_TOKEN is not set. Add a fine-grained read-only PAT to .env."
        )
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }


def _request(path: str, params: dict | None = None) -> Any:
    """GET {base}{path}. Returns parsed JSON. Raises GitHubError on failure."""
    url = f"{_base_url()}{path}"
    try:
        resp = httpx.get(url, headers=_headers(), params=params or {},
                         timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        raise GitHubError(f"Network error calling GitHub: {e}") from e
    if resp.status_code == 404:
        raise GitHubError(f"Not found: {path} (check owner/repo/number and token access).")
    if resp.status_code in (401, 403):
        # 403 with rate-limit headers means throttled; otherwise auth/permission.
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            raise GitHubError("GitHub rate limit hit — try again shortly.")
        raise GitHubError("GitHub auth/permission error — check GITHUB_TOKEN scopes.")
    if resp.status_code >= 400:
        raise GitHubError(f"GitHub error {resp.status_code}: {resp.text[:200]}")
    return resp.json()
```

### 3.2 Trimming helpers (keep tool output small — the LLM reads it)

```python
def _trim_user(u: dict | None) -> str:
    return (u or {}).get("login", "") if u else ""


def _trim_issue(it: dict) -> dict:
    return {
        "number": it["number"],
        "title": it["title"],
        "state": it["state"],
        "author": _trim_user(it.get("user")),
        "labels": [l["name"] for l in it.get("labels", [])],
        "assignees": [_trim_user(a) for a in it.get("assignees", [])],
        "comments": it.get("comments", 0),
        "created_at": it.get("created_at"),
        "updated_at": it.get("updated_at"),
        "url": it.get("html_url"),
        # body only included by get_issue, not the list view (keep lists short)
    }


def _trim_pr(pr: dict) -> dict:
    return {
        "number": pr["number"],
        "title": pr["title"],
        "state": pr["state"],
        "draft": pr.get("draft", False),
        "author": _trim_user(pr.get("user")),
        "labels": [l["name"] for l in pr.get("labels", [])],
        "requested_reviewers": [_trim_user(r) for r in pr.get("requested_reviewers", [])],
        "base": (pr.get("base") or {}).get("ref"),
        "head": (pr.get("head") or {}).get("ref"),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "mergeable_state": pr.get("mergeable_state"),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "url": pr.get("html_url"),
    }
```

### 3.3 Public read functions (implement all)

```python
def _split_repo(repo: str) -> tuple[str, str]:
    """'owner/name' -> ('owner','name'). Raises GitHubError on bad format."""
    parts = repo.strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubError(f"Repo must be 'owner/name', got {repo!r}.")
    return parts[0], parts[1]


def list_issues(repo: str, state: str = "open", limit: int = MAX_LIST) -> list[dict]:
    """Open (or 'closed'/'all') issues. Excludes PRs (GitHub returns PRs in the
    issues endpoint — filter out any item that has a 'pull_request' key)."""
    owner, name = _split_repo(repo)
    data = _request(f"/repos/{owner}/{name}/issues",
                    {"state": state, "per_page": min(limit, 100)})
    issues = [_trim_issue(i) for i in data if "pull_request" not in i]
    return issues[:limit]


def get_issue(repo: str, number: int) -> dict:
    owner, name = _split_repo(repo)
    it = _request(f"/repos/{owner}/{name}/issues/{number}")
    trimmed = _trim_issue(it)
    trimmed["body"] = (it.get("body") or "")[:2000]   # cap body length
    return trimmed


def list_pull_requests(repo: str, state: str = "open", limit: int = MAX_LIST) -> list[dict]:
    owner, name = _split_repo(repo)
    data = _request(f"/repos/{owner}/{name}/pulls",
                    {"state": state, "per_page": min(limit, 100)})
    return [_trim_pr(p) for p in data][:limit]


def get_pull_request(repo: str, number: int) -> dict:
    owner, name = _split_repo(repo)
    pr = _request(f"/repos/{owner}/{name}/pulls/{number}")
    trimmed = _trim_pr(pr)
    trimmed["body"] = (pr.get("body") or "")[:2000]
    return trimmed


def get_pr_reviews(repo: str, number: int) -> list[dict]:
    """Who has reviewed and their verdict (APPROVED / CHANGES_REQUESTED / COMMENTED)."""
    owner, name = _split_repo(repo)
    data = _request(f"/repos/{owner}/{name}/pulls/{number}/reviews")
    return [
        {"reviewer": _trim_user(r.get("user")),
         "state": r.get("state"),
         "submitted_at": r.get("submitted_at")}
        for r in data
    ]


def get_pr_checks(repo: str, number: int) -> dict:
    """CI check-run summary for a PR's head commit.
    Returns {'total': int, 'success': int, 'failure': int, 'pending': int,
             'runs': [{'name','status','conclusion'}...]}."""
    owner, name = _split_repo(repo)
    pr = _request(f"/repos/{owner}/{name}/pulls/{number}")
    sha = (pr.get("head") or {}).get("sha")
    if not sha:
        return {"total": 0, "success": 0, "failure": 0, "pending": 0, "runs": []}
    data = _request(f"/repos/{owner}/{name}/commits/{sha}/check-runs")
    runs = data.get("check_runs", [])
    out = {"total": len(runs), "success": 0, "failure": 0, "pending": 0, "runs": []}
    for r in runs:
        concl = r.get("conclusion")
        status = r.get("status")
        if status != "completed":
            out["pending"] += 1
        elif concl == "success":
            out["success"] += 1
        elif concl in ("failure", "timed_out", "cancelled", "action_required"):
            out["failure"] += 1
        out["runs"].append({"name": r.get("name"), "status": status, "conclusion": concl})
    return out


def search_issues(query: str, limit: int = MAX_LIST) -> list[dict]:
    """Cross-repo search using GitHub's search syntax, e.g.
    'repo:acme/backend is:open label:bug'. Returns trimmed issue/PR dicts."""
    data = _request("/search/issues", {"q": query, "per_page": min(limit, 100)})
    items = data.get("items", [])
    out = []
    for it in items[:limit]:
        is_pr = "pull_request" in it
        out.append({**_trim_issue(it), "type": "pr" if is_pr else "issue"})
    return out


def rate_limit() -> dict:
    """Diagnostics: {'remaining': int, 'limit': int, 'reset_epoch': int}."""
    data = _request("/rate_limit")
    core = data.get("resources", {}).get("core", {})
    return {"remaining": core.get("remaining"), "limit": core.get("limit"),
            "reset_epoch": core.get("reset")}
```

---

## 4. DB changes: `src/ask_buddy/db.py`

The triage watcher must remember what it has already reported so it doesn't
re-post the same issue/PR every poll. Add a small state table.

### 4.1 Add DDL constant (place next to `_REMINDERS_TABLE_DDL`)

```python
# ---------------------------------------------------------------------------
# Git watch state (proactive triage dedup — one row per watched repo)
# ---------------------------------------------------------------------------
# last_issue_number / last_pr_number : highest number already reported to Slack.
#   On each poll we only report items with number > the stored high-water mark.
_GIT_WATCH_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS ask_buddy_git_watch (
        repo               TEXT        PRIMARY KEY,   -- 'owner/name'
        last_issue_number  INTEGER     NOT NULL DEFAULT 0,
        last_pr_number     INTEGER     NOT NULL DEFAULT 0,
        last_polled_at     TIMESTAMPTZ
    );
"""
```

### 4.2 Wire DDL into `init_schema()`

In `init_schema()`, append `_GIT_WATCH_TABLE_DDL` to the big `ddl` string
(right after `_REMINDERS_TABLE_DDL`, same style).

### 4.3 Add idempotent init + helpers (place near the reminders helpers)

```python
def init_git_watch_schema() -> None:
    """Idempotent: create just the git-watch table. Safe to call at bot startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_GIT_WATCH_TABLE_DDL)


def get_git_watermark(repo: str) -> dict:
    """Return {'last_issue_number', 'last_pr_number'} for a repo (0/0 if new)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_issue_number, last_pr_number "
                "FROM ask_buddy_git_watch WHERE repo = %s;", (repo,))
            row = cur.fetchone()
            if row is None:
                return {"last_issue_number": 0, "last_pr_number": 0}
            return {"last_issue_number": row["last_issue_number"],
                    "last_pr_number": row["last_pr_number"]}


def set_git_watermark(repo: str, last_issue_number: int, last_pr_number: int) -> None:
    """Upsert the high-water marks + last_polled_at=now() for a repo."""
    sql = """
        INSERT INTO ask_buddy_git_watch
            (repo, last_issue_number, last_pr_number, last_polled_at)
        VALUES (%(repo)s, %(iss)s, %(pr)s, now())
        ON CONFLICT (repo) DO UPDATE
            SET last_issue_number = EXCLUDED.last_issue_number,
                last_pr_number    = EXCLUDED.last_pr_number,
                last_polled_at    = now();
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"repo": repo, "iss": last_issue_number, "pr": last_pr_number})
```

> **First-poll behavior:** on the very first poll of a repo the watermark is 0,
> which would report *every* open issue/PR at once (spammy). Mitigation in the
> watcher (Section 6): on first sight of a repo (no DB row), **seed** the
> watermarks to the current maxima **without posting** anything, then report
> only newer items on subsequent polls. See `poll_once()`.

---

## 5. Agent changes: `src/ask_buddy/agent.py`

### 5.1 Add two system prompts (place after `SCHEDULER_SYSTEM_PROMPT`)

```python
GIT_ISSUE_SYSTEM_PROMPT = """\
You are the Git Issue Agent within Ask Buddy. You answer READ-ONLY questions \
about GitHub issues: listing open/closed issues, summarizing a specific issue, \
who is assigned, what labels it has, and searching issues.

You have these tools: list_issues, get_issue, search_issues. You CANNOT create, \
edit, comment on, or close issues — you are read-only. If a user asks you to \
change anything, tell them plainly that Ask Buddy's git access is read-only.

== PROCESS ==
1. Identify the repository as 'owner/name'. If the user did not give a repo and \
one cannot be inferred, ask them which repo (via post_slack_message).
2. Call the right tool. Read the result.
3. Reply in plain English. For lists, show issue number, title, and state, one \
per line, most recent first. For a single issue, summarize title, state, author, \
labels, assignees, and a 1-2 sentence gist of the body. Always include the \
issue URL(s).

== HARD RULES ==
- Read-only. Never claim to have changed anything.
- Never fabricate issue numbers, titles, authors, or URLs — only report what the \
tool returned.
- If a tool returns an error (e.g. GitHub not configured, repo not found), relay \
a short, clear message; do not guess.
- Deliver the final answer via post_slack_message.
"""

GIT_PR_SYSTEM_PROMPT = """\
You are the Git PR Agent within Ask Buddy. You answer READ-ONLY questions about \
GitHub pull requests: listing open/closed PRs, summarizing a specific PR, its \
review status, requested reviewers, and CI check results.

You have these tools: list_pull_requests, get_pull_request, get_pr_reviews, \
get_pr_checks. You CANNOT open, approve, comment on, or merge PRs — read-only.

== PROCESS ==
1. Identify the repository as 'owner/name'; ask if missing.
2. Call the right tool(s). To answer "is PR #N ready to merge?" combine \
get_pull_request (draft/mergeable_state), get_pr_reviews (approvals), and \
get_pr_checks (CI pass/fail).
3. Reply in plain English. For lists: PR number, title, author, draft flag. For \
a single PR: title, author, base<-head, reviewers + their verdicts, CI summary \
(X passed / Y failed / Z pending), and the PR URL.

== HARD RULES ==
- Read-only. Never claim to have merged/approved/commented.
- Never fabricate PR numbers, reviewers, verdicts, or check results.
- On tool error, relay a short clear message; do not guess.
- Deliver the final answer via post_slack_message.
"""
```

### 5.2 Extend `SUPERVISOR_PROMPT`

Add two bullet points to the agent list and a routing rule. Insert after the
`scheduler_agent` bullet:

```
  • git_issue_agent — READ-ONLY questions about GitHub issues (list/summarize \
issues, labels, assignees, search issues) for a given 'owner/repo'.

  • git_pr_agent — READ-ONLY questions about GitHub pull requests (list/summarize \
PRs, review status, requested reviewers, CI check results) for a given 'owner/repo'.
```

And add to the numbered routing rules:

```
5. GitHub *issue* questions go to git_issue_agent; GitHub *pull request* / PR / \
review / CI-check questions go to git_pr_agent. If a git question needs both \
(e.g. "summarize all activity on repo X"), delegate to both and combine. \
Ask Buddy's git access is READ-ONLY — if a user asks to create/close/merge \
anything, explain that plainly instead of delegating.
```

### 5.3 Add the git tool factory + agent builders

Follow the exact closure pattern used by `_build_reminder_tools` /
`build_scheduler_agent`. Add near those functions:

```python
def _build_git_issue_tools():
    from langchain_core.tools import tool
    from . import github_client as gh

    @tool
    def list_issues(repo: str, state: str = "open") -> list[dict]:
        """List GitHub issues for 'owner/repo'. state: 'open'|'closed'|'all'.
        Returns number, title, state, author, labels, assignees, url. Read-only."""
        try:
            return gh.list_issues(repo, state=state)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    @tool
    def get_issue(repo: str, number: int) -> dict:
        """Get one GitHub issue by number from 'owner/repo', including a truncated
        body. Read-only."""
        try:
            return gh.get_issue(repo, number)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def search_issues(query: str) -> list[dict]:
        """Search issues/PRs with GitHub search syntax, e.g.
        'repo:acme/backend is:open label:bug'. Read-only."""
        try:
            return gh.search_issues(query)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    return list_issues, get_issue, search_issues


def _build_git_pr_tools():
    from langchain_core.tools import tool
    from . import github_client as gh

    @tool
    def list_pull_requests(repo: str, state: str = "open") -> list[dict]:
        """List GitHub pull requests for 'owner/repo'. Read-only."""
        try:
            return gh.list_pull_requests(repo, state=state)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    @tool
    def get_pull_request(repo: str, number: int) -> dict:
        """Get one PR by number from 'owner/repo'. Read-only."""
        try:
            return gh.get_pull_request(repo, number)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def get_pr_reviews(repo: str, number: int) -> list[dict]:
        """List reviews (reviewer + APPROVED/CHANGES_REQUESTED/COMMENTED) for a PR."""
        try:
            return gh.get_pr_reviews(repo, number)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    @tool
    def get_pr_checks(repo: str, number: int) -> dict:
        """Summarize CI check-runs (passed/failed/pending) for a PR's head commit."""
        try:
            return gh.get_pr_checks(repo, number)
        except gh.GitHubError as e:
            return {"error": str(e)}

    return list_pull_requests, get_pull_request, get_pr_reviews, get_pr_checks


def build_git_issue_agent(slack_post_fn: Callable[[str, str], None]) -> CugaAgent:
    post_tool = _build_post_tool(slack_post_fn)
    list_issues, get_issue, search_issues = _build_git_issue_tools()
    return CugaAgent(
        tools=[list_issues, get_issue, search_issues, post_tool],
        enable_knowledge=False,
        special_instructions=GIT_ISSUE_SYSTEM_PROMPT,
    )


def build_git_pr_agent(slack_post_fn: Callable[[str, str], None]) -> CugaAgent:
    post_tool = _build_post_tool(slack_post_fn)
    list_prs, get_pr, get_reviews, get_checks = _build_git_pr_tools()
    return CugaAgent(
        tools=[list_prs, get_pr, get_reviews, get_checks, post_tool],
        enable_knowledge=False,
        special_instructions=GIT_PR_SYSTEM_PROMPT,
    )
```

### 5.4 Register the slaves in `build_supervisor`

In `build_supervisor(...)`, after building `scheduler`, add:

```python
    git_issue = build_git_issue_agent(slack_post_fn)
    git_pr = build_git_pr_agent(slack_post_fn)
```

and extend the `agents=` dict:

```python
    supervisor = CugaSupervisor(
        agents={
            "hr_agent": hr,
            "it_agent": it,
            "scheduler_agent": scheduler,
            "git_issue_agent": git_issue,
            "git_pr_agent": git_pr,
        },
        special_instructions=SUPERVISOR_PROMPT,
    )
```

> Do **not** touch `build_agent` (the backward-compatible single-agent builder).

---

## 6. New file: `src/ask_buddy/git_watch.py` (proactive triage)

Mirrors `scheduler.py`'s structure: an APScheduler `IntervalTrigger` job that
polls each watched repo, finds items newer than the stored watermark, formats a
short summary, and posts to the configured Slack channel via a plain-post fn.

### 6.1 Full module (implement as specified)

```python
"""
Proactive GitHub triage watcher for Ask Buddy.

Polls the repos in GIT_WATCH_REPOS every GIT_WATCH_INTERVAL_MINUTES using
APScheduler, and posts a short summary of NEW issues / PRs to GIT_WATCH_CHANNEL.

Dedup: ask_buddy_git_watch stores a per-repo high-water mark (highest issue /
PR number already reported). We only report items with a higher number. On the
first sight of a repo (no DB row) we SEED the watermark to the current maxima
without posting, so the bot doesn't dump the entire backlog on startup.

Caveat (same as scheduler.py): only runs while the bot process is up.
"""

from __future__ import annotations
import logging
import os
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import github_client as gh
from .db import get_git_watermark, set_git_watermark

log = logging.getLogger("ask_buddy.git_watch")

_scheduler: BackgroundScheduler | None = None


def _watched_repos() -> list[str]:
    raw = os.environ.get("GIT_WATCH_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _channel() -> str:
    return os.environ.get("GIT_WATCH_CHANNEL", "").strip()


def _interval_minutes() -> int:
    try:
        return max(1, int(os.environ.get("GIT_WATCH_INTERVAL_MINUTES", "5")))
    except ValueError:
        return 5


def _format_summary(repo: str, new_issues: list[dict], new_prs: list[dict]) -> str:
    lines = [f":mag: *New activity in `{repo}`*"]
    if new_issues:
        lines.append(f"\n*Issues ({len(new_issues)}):*")
        for i in new_issues:
            lines.append(f"• #{i['number']} {i['title']} — by {i['author']} <{i['url']}>")
    if new_prs:
        lines.append(f"\n*Pull requests ({len(new_prs)}):*")
        for p in new_prs:
            draft = " _(draft)_" if p.get("draft") else ""
            lines.append(f"• #{p['number']} {p['title']}{draft} — by {p['author']} <{p['url']}>")
    return "\n".join(lines)


def poll_once(repo: str, post_fn: Callable[[str, str], None]) -> None:
    """Poll one repo, post a summary of items newer than the watermark, then
    advance the watermark. First sight of a repo seeds silently (no post)."""
    channel = _channel()
    if not channel:
        return
    try:
        issues = gh.list_issues(repo, state="open")
        prs = gh.list_pull_requests(repo, state="open")
    except gh.GitHubError as e:
        log.warning("[git_watch] poll failed for %s: %s", repo, e)
        return

    max_issue = max([i["number"] for i in issues], default=0)
    max_pr = max([p["number"] for p in prs], default=0)

    mark = get_git_watermark(repo)
    first_sight = (mark["last_issue_number"] == 0 and mark["last_pr_number"] == 0)

    if first_sight:
        # Seed silently so we don't dump the whole backlog.
        set_git_watermark(repo, max_issue, max_pr)
        log.info("[git_watch] seeded %s at issue<=%s pr<=%s (no post)",
                 repo, max_issue, max_pr)
        return

    new_issues = [i for i in issues if i["number"] > mark["last_issue_number"]]
    new_prs = [p for p in prs if p["number"] > mark["last_pr_number"]]

    if new_issues or new_prs:
        try:
            post_fn(channel, _format_summary(repo, new_issues, new_prs))
        except Exception:
            log.exception("[git_watch] failed to post summary for %s", repo)
            return  # do NOT advance watermark if the post failed

    set_git_watermark(repo, max(max_issue, mark["last_issue_number"]),
                      max(max_pr, mark["last_pr_number"]))


def _poll_all(post_fn: Callable[[str, str], None]) -> None:
    for repo in _watched_repos():
        poll_once(repo, post_fn)


def start_git_watch(slack_post_fn: Callable[[str, str], None]) -> BackgroundScheduler | None:
    """Start the polling scheduler. No-op (returns None) if GITHUB_TOKEN,
    GIT_WATCH_REPOS, or GIT_WATCH_CHANNEL are unset."""
    global _scheduler
    if not os.environ.get("GITHUB_TOKEN"):
        log.info("[git_watch] GITHUB_TOKEN unset — triage watcher disabled.")
        return None
    if not _watched_repos():
        log.info("[git_watch] GIT_WATCH_REPOS empty — triage watcher disabled.")
        return None
    if not _channel():
        log.info("[git_watch] GIT_WATCH_CHANNEL unset — triage watcher disabled.")
        return None

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _poll_all,
        trigger=IntervalTrigger(minutes=_interval_minutes()),
        args=[slack_post_fn],
        id="git-watch-poll",
        replace_existing=True,
        next_run_time=None,   # first run happens after one interval; see note
    )
    _scheduler.start()
    log.info("[git_watch] started: repos=%s channel=%s every %d min",
             _watched_repos(), _channel(), _interval_minutes())
    return _scheduler
```

> **Note on `next_run_time`:** APScheduler's `IntervalTrigger` first fires after
> one interval. That is fine — the first fire seeds watermarks silently anyway.
> If you want an immediate seed at boot, call `_poll_all(slack_post_fn)` once
> right after `start()` (wrapped in try/except); it will only seed, not post.

---

## 7. Wiring: `src/ask_buddy/slack_listener.py`

Two small additions, both mirroring the existing reminders wiring.

### 7.1 Init the git-watch schema at startup

After the reminders-schema init block (~line 79), add:

```python
# Ensure the git-watch table exists at startup (idempotent)
try:
    from .db import init_git_watch_schema
    init_git_watch_schema()
    log.info("ask_buddy_git_watch table ready.")
except Exception as _e:
    log.warning("Could not initialise git-watch schema: %s", _e)
```

### 7.2 Start the git watcher at startup

After the `start_scheduler(_plain_post)` block (~line 139), add:

```python
# Proactive GitHub triage watcher (polls GitHub, posts to GIT_WATCH_CHANNEL)
try:
    from .git_watch import start_git_watch
    start_git_watch(_plain_post)   # reuse the no-feedback-buttons poster
except Exception as _e:
    log.warning("Could not start git watch: %s", _e)
```

> Reuse `_plain_post` (defined at ~line 129) — triage summaries are broadcasts,
> not rated Q&A answers, so they must **not** get 👍/👎 feedback buttons.
> No changes are needed to `_run_agent_for_message`: the supervisor already
> routes to whichever slave matches, so git questions Just Work once the slaves
> are registered in `build_supervisor`.

---

## 8. Tests: `tests/test_ask_buddy.py`

Add offline unit tests (marked so they run without a DB or network). Mock
`github_client._request` so no real HTTP happens.

### 8.1 Tests to add

1. **`test_github_client_trims_issue`** — feed a fake raw issue dict through
   `_trim_issue`, assert only expected keys and that a `pull_request`-bearing
   item is excluded by `list_issues` (monkeypatch `_request` to return a list
   mixing issues and PRs; assert PRs filtered out).
2. **`test_github_client_split_repo`** — `_split_repo("a/b") == ("a","b")`;
   `_split_repo("bad")` raises `GitHubError`.
3. **`test_github_client_missing_token`** — unset `GITHUB_TOKEN`, assert
   `list_issues` surfaces a `GitHubError` mentioning the token.
4. **`test_git_pr_checks_counts`** — monkeypatch `_request` to return two calls
   (PR object with head sha, then check-runs with mixed conclusions); assert
   `get_pr_checks` counts success/failure/pending correctly.
5. **`test_git_tools_return_error_dict`** — build tools via
   `_build_git_issue_tools`, monkeypatch `gh.list_issues` to raise
   `GitHubError`, assert the tool returns `[{"error": ...}]` (not a raise).
6. **`test_git_watch_first_sight_seeds_no_post`** — monkeypatch `gh.list_issues`
   / `gh.list_pull_requests` to return items, and `get_git_watermark` to return
   0/0; assert `poll_once` calls `set_git_watermark` but the `post_fn` mock is
   **not** called.
7. **`test_git_watch_reports_only_new`** — watermark = issue 5 / pr 3; issues
   include #4,#6,#7 and PRs include #3,#4; assert `post_fn` called once and the
   summary text contains #6, #7, PR #4 but not #4-issue/#3/#5.
8. **`test_supervisor_registers_git_slaves`** — call `build_supervisor` with a
   dummy `slack_post_fn`; assert the resulting supervisor's agents dict contains
   `git_issue_agent` and `git_pr_agent` (inspect whatever attribute stores them;
   if not easily introspectable, assert `build_git_issue_agent` /
   `build_git_pr_agent` return `CugaAgent` instances).

### 8.2 Mocking pattern (use in tests 1,3,4)

```python
import src.ask_buddy.github_client as gh

def test_git_pr_checks_counts(monkeypatch):
    calls = iter([
        {"head": {"sha": "abc"}},                       # get_pull_request
        {"check_runs": [                                # check-runs
            {"name": "unit", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "failure"},
            {"name": "e2e",  "status": "in_progress", "conclusion": None},
        ]},
    ])
    monkeypatch.setattr(gh, "_request", lambda path, params=None: next(calls))
    out = gh.get_pr_checks("a/b", 1)
    assert out == {"total": 3, "success": 1, "failure": 1, "pending": 1,
                   "runs": out["runs"]}
```

Run:

```bash
uv run pytest tests/test_ask_buddy.py -v -m "not integration"
```

---

## 9. Docs to update

- **`README_ASKBUDDY.md`** — add a "Git Agent" section: what it answers
  (read-only issues/PRs), the two new slaves in the architecture diagram, the
  new env vars, and the triage behavior. Update the File Structure list to add
  `github_client.py` and `git_watch.py`.
- **`.env.example`** — add the Git Agent block from Section 1.2.
- **`INSTALLATION.md`** — add "Step 3.5 — (Optional) Configure the Git Agent"
  covering PAT creation (Section 1.1) and env vars.
- **`SLACK_PERMISSIONS.md`** — no new Slack scopes needed *if* `GIT_WATCH_CHANNEL`
  is a channel the bot is already in. Note: the bot must be a member of the
  triage channel (`chat:write` already required) — add a one-line reminder.

---

## 10. Manual verification (after implementing)

1. `uv sync` — confirm `httpx` installed.
2. Set `GITHUB_TOKEN` + one repo you can read. Leave `GIT_WATCH_*` unset first.
3. Start the bot: `uv run python -m src.ask_buddy.slack_listener`. Confirm log
   line `ask_buddy_git_watch table ready.` and that the git watcher logs
   "disabled" (since `GIT_WATCH_REPOS` is empty).
4. In Slack DM: **"list open issues in owner/repo"** → expect a plain-English
   list with numbers, titles, URLs (routed to `git_issue_agent`).
5. **"is PR #N ready to merge in owner/repo?"** → expect review + CI summary
   (routed to `git_pr_agent`).
6. **"close issue #1 in owner/repo"** → expect a read-only refusal, no action.
7. Set `GIT_WATCH_REPOS` + `GIT_WATCH_CHANNEL`, restart. Confirm log
   "git_watch started". First poll seeds silently. Open a new issue on the repo;
   within one interval, confirm a summary posts to the triage channel exactly
   once, and does not repeat on the next poll.
8. `uv run pytest tests/test_ask_buddy.py -v -m "not integration"` — all green.

---

## 11. Guardrails & gotchas (read before coding)

- **Read-only, enforced two ways:** (a) the PAT has only read scopes, so even a
  prompt-injection can't write; (b) the tool lists contain zero write tools.
  Keep both.
- **Keep tool output small.** The LLM reads raw tool JSON. `_trim_*` helpers and
  `MAX_LIST=30` exist to prevent 100-issue dumps blowing the context. Do not
  return raw GitHub payloads.
- **The issues endpoint returns PRs too.** `list_issues` MUST filter items with a
  `pull_request` key, or issue lists will be polluted with PRs.
- **Watermark advance only after a successful post.** In `poll_once`, if
  `post_fn` raises, return early WITHOUT advancing — otherwise the missed items
  are lost forever.
- **First-sight seeding** prevents backlog spam. Do not skip it.
- **`httpx.get` is synchronous** — fine here: git tools run inside the agent's
  own thread, and the watcher runs in APScheduler's thread pool. Do not switch
  to async without reworking both call sites.
- **Rate limits** are a non-issue at this scale, but `github_client.rate_limit()`
  is provided for debugging a `403`.

---

## 12. v2 growth path (NOT in v1 — for context only)

- **Write actions** (comment, label, close/reopen issues; request reviewers,
  approve, merge PRs): add write tools + a `ToolApproval` policy
  (`agent.policies.add_tool_approval`, see `sdk.py` docstring) so risky writes
  (merge/close) require Slack confirmation. Bump PAT scopes to Read-and-write.
- **Webhooks** instead of polling: add a tiny FastAPI endpoint (or Bolt HTTP
  mode) + `smee.io` for local dev; replace the interval job with event handling.
  DB dedup table can then be dropped.
- **A third `git_repo_agent`** for repo-level Q&A (releases, contributors,
  file contents) if usage warrants it.
```
