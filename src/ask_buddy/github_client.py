"""
Thin GitHub REST client for Ask Buddy's git agents and triage watcher.

Authenticates with a fine-grained PAT (GITHUB_TOKEN).
Every function returns trimmed plain dicts/lists, or raises GitHubError.

Endpoints used:
  GET  /repos/{owner}/{repo}/issues
  GET  /repos/{owner}/{repo}/issues/{n}
  GET  /repos/{owner}/{repo}/pulls
  GET  /repos/{owner}/{repo}/pulls/{n}
  GET  /repos/{owner}/{repo}/pulls/{n}/reviews
  GET  /repos/{owner}/{repo}/commits/{ref}/check-runs
  GET  /search/issues
  GET  /rate_limit
  POST /repos/{owner}/{repo}/issues
  POST /repos/{owner}/{repo}/pulls
  POST /repos/{owner}/{repo}/issues/{n}/comments
  POST /repos/{owner}/{repo}/issues/{n}/labels
  DELETE /repos/{owner}/{repo}/issues/{n}/labels/{name}
  POST /repos/{owner}/{repo}/issues/{n}/assignees
  DELETE /repos/{owner}/{repo}/issues/{n}/assignees
  POST /repos/{owner}/{repo}/pulls/{n}/requested_reviewers
  PATCH /repos/{owner}/{repo}/issues/{n}
  PUT  /repos/{owner}/{repo}/pulls/{n}/merge
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("ask_buddy.github_client")

API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT = 15.0
MAX_LIST = 30          # cap list results so tool output stays small for the LLM

# Retry config for transient 5xx / network errors.
_RETRY_ATTEMPTS = 3
_RETRY_BASE_SLEEP = 1.0  # seconds; doubles each attempt


class GitHubError(RuntimeError):
    """Raised on non-2xx responses or missing configuration."""


def _base_url() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise GitHubError(
            "GITHUB_TOKEN is not set. Add a fine-grained PAT to .env."
        )
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }


# ---------------------------------------------------------------------------
# Rate-limit state — shared across all calls in the process
# ---------------------------------------------------------------------------

_rate_limit_reset_epoch: float = 0.0   # epoch seconds when the window resets


def _check_rate_limit_window() -> None:
    """Raise GitHubError if the process-level rate-limit window hasn't reset yet."""
    global _rate_limit_reset_epoch
    if _rate_limit_reset_epoch and time.time() < _rate_limit_reset_epoch:
        wait = int(_rate_limit_reset_epoch - time.time())
        raise GitHubError(
            f"GitHub rate limit active — resets in ~{wait}s. Try again later."
        )


def _record_rate_limit_reset(headers: dict) -> None:
    """Store the X-RateLimit-Reset epoch from a 403 rate-limit response."""
    global _rate_limit_reset_epoch
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            _rate_limit_reset_epoch = float(reset)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# HTTP helpers with retry
# ---------------------------------------------------------------------------

def _request(path: str, params: dict | None = None) -> Any:
    """GET {base}{path}. Retries up to _RETRY_ATTEMPTS times on 5xx/network.
    Raises GitHubError on 4xx or exhausted retries."""
    _check_rate_limit_window()
    url = f"{_base_url()}{path}"
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = httpx.get(url, headers=_headers(), params=params or {},
                             timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BASE_SLEEP * (2 ** attempt))
                continue
            raise GitHubError(f"Network error calling GitHub: {e}") from e

        if resp.status_code == 404:
            raise GitHubError(
                f"Not found: {path} (check owner/repo/number and token access)."
            )
        if resp.status_code in (401, 403):
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                _record_rate_limit_reset(dict(resp.headers))
                raise GitHubError("GitHub rate limit hit — try again shortly.")
            raise GitHubError(
                "GitHub auth/permission error — check GITHUB_TOKEN scopes."
            )
        if resp.status_code >= 500:
            last_exc = GitHubError(
                f"GitHub server error {resp.status_code}: {resp.text[:200]}"
            )
            if attempt < _RETRY_ATTEMPTS - 1:
                log.warning("[github] %s %d — retrying (attempt %d/%d)",
                            path, resp.status_code, attempt + 1, _RETRY_ATTEMPTS)
                time.sleep(_RETRY_BASE_SLEEP * (2 ** attempt))
                continue
            raise last_exc
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub error {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    raise GitHubError("GitHub request failed after retries.")  # unreachable


def _request_write(method: str, path: str, json_body: dict | None = None) -> Any:
    """POST/PATCH/PUT/DELETE variant of _request — same retry and error handling.
    Logs every write call for audit purposes."""
    _check_rate_limit_window()
    url = f"{_base_url()}{path}"
    log.info("[github] %s %s", method, path)
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = httpx.request(method, url, headers=_headers(), json=json_body,
                                 timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BASE_SLEEP * (2 ** attempt))
                continue
            raise GitHubError(f"Network error calling GitHub: {e}") from e

        if resp.status_code in (401, 403):
            raise GitHubError(
                "GitHub write permission error — the PAT needs Read-and-write "
                "on Issues/Pull requests for this action."
            )
        if resp.status_code == 404:
            raise GitHubError(f"Not found: {path}")
        if resp.status_code == 422:
            # Unprocessable entity — not retryable (bad input)
            raise GitHubError(
                f"GitHub validation error: {resp.text[:300]}"
            )
        if resp.status_code >= 500:
            last_exc = GitHubError(
                f"GitHub server error {resp.status_code}: {resp.text[:200]}"
            )
            if attempt < _RETRY_ATTEMPTS - 1:
                log.warning("[github] %s %s %d — retrying (attempt %d/%d)",
                            method, path, resp.status_code, attempt + 1, _RETRY_ATTEMPTS)
                time.sleep(_RETRY_BASE_SLEEP * (2 ** attempt))
                continue
            raise last_exc
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub error {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.text else {}
    raise GitHubError("GitHub write request failed after retries.")


# ---------------------------------------------------------------------------
# Trimming helpers
# ---------------------------------------------------------------------------

def _trim_user(u: dict | None) -> str:
    return (u or {}).get("login", "") if u else ""


def _trim_issue(it: dict) -> dict:
    return {
        "number": it["number"],
        "title": it["title"],
        "state": it["state"],
        "author": _trim_user(it.get("user")),
        "labels": [lbl["name"] for lbl in it.get("labels", [])],
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
        "labels": [lbl["name"] for lbl in pr.get("labels", [])],
        "requested_reviewers": [_trim_user(r) for r in pr.get("requested_reviewers", [])],
        "base": (pr.get("base") or {}).get("ref"),
        "head": (pr.get("head") or {}).get("ref"),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "mergeable_state": pr.get("mergeable_state"),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "url": pr.get("html_url"),
    }


def _split_repo(repo: str) -> tuple[str, str]:
    """'owner/name' -> ('owner','name'). Raises GitHubError on bad format."""
    parts = repo.strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubError(f"Repo must be 'owner/name', got {repo!r}.")
    return parts[0], parts[1]


# ---------------------------------------------------------------------------
# Read functions
# ---------------------------------------------------------------------------

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


def list_pull_requests(repo: str, state: str = "open",
                       limit: int = MAX_LIST) -> list[dict]:
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
    # Heterogeneous by design — three counters plus the run list — so the value
    # type has to be Any for the counter arithmetic below to type-check.
    out: dict[str, Any] = {
        "total": len(runs), "success": 0, "failure": 0, "pending": 0, "runs": [],
    }
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


def get_pr_files(repo: str, number: int, limit: int = MAX_LIST) -> list[dict]:
    """List files changed by a PR: filename, status, additions, deletions, changes.
    Raw patch text is excluded to keep tool output small. Read-only."""
    owner, name = _split_repo(repo)
    data = _request(f"/repos/{owner}/{name}/pulls/{number}/files",
                    {"per_page": min(limit, 100)})
    return [
        {
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "changes": f.get("changes", 0),
        }
        for f in data[:limit]
    ]


def get_pr_merge_status(repo: str, number: int) -> dict:
    """
    Composite merge-readiness verdict combining PR state, reviews, and CI
    checks into one answer. Read-only — makes NO merge call.

    Returns:
        {
          "mergeable": bool | None,
          "draft": bool,
          "approved_count": int,
          "changes_requested_count": int,
          "checks_passed": bool,
          "checks_summary": {"total", "success", "failure", "pending"},
          "blocking_reasons": list[str],   # empty = nothing blocking
        }
    """
    pr = get_pull_request(repo, number)
    reviews = get_pr_reviews(repo, number)
    checks = get_pr_checks(repo, number)

    # Latest review per reviewer wins.
    latest_by_reviewer: dict[str, str] = {}
    for r in reviews:
        latest_by_reviewer[r["reviewer"]] = r["state"]
    approved_count = sum(1 for s in latest_by_reviewer.values() if s == "APPROVED")
    changes_requested_count = sum(
        1 for s in latest_by_reviewer.values() if s == "CHANGES_REQUESTED"
    )

    checks_passed = checks["total"] == 0 or (
        checks["failure"] == 0 and checks["pending"] == 0
    )

    blocking: list[str] = []
    if pr.get("draft"):
        blocking.append("PR is a draft")
    if changes_requested_count > 0:
        blocking.append(f"{changes_requested_count} reviewer(s) requested changes")
    if checks["failure"] > 0:
        blocking.append(f"{checks['failure']} CI check(s) failing")
    if checks["pending"] > 0:
        blocking.append(f"{checks['pending']} CI check(s) still pending")
    if pr.get("mergeable_state") in ("dirty", "blocked"):
        blocking.append(f"GitHub reports mergeable_state='{pr['mergeable_state']}'")

    return {
        "mergeable": pr.get("mergeable_state") == "clean" if pr.get("mergeable_state") else None,
        "draft": pr.get("draft", False),
        "approved_count": approved_count,
        "changes_requested_count": changes_requested_count,
        "checks_passed": checks_passed,
        "checks_summary": {k: checks[k] for k in ("total", "success", "failure", "pending")},
        "blocking_reasons": blocking,
    }


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


# ---------------------------------------------------------------------------
# Write functions — ungated (low blast-radius)
# ---------------------------------------------------------------------------

def add_issue_comment(repo: str, number: int, body: str) -> dict:
    """Post a comment on an issue or PR. Returns {'url': ..., 'created_at': ...}."""
    owner, name = _split_repo(repo)
    data = _request_write("POST", f"/repos/{owner}/{name}/issues/{number}/comments",
                          {"body": body})
    return {"url": data.get("html_url"), "created_at": data.get("created_at")}


def add_labels(repo: str, number: int, labels: list[str]) -> list[str]:
    """Add one or more labels to an issue or PR. Returns the full label set after the call."""
    owner, name = _split_repo(repo)
    data = _request_write("POST", f"/repos/{owner}/{name}/issues/{number}/labels",
                          {"labels": labels})
    return [lbl["name"] for lbl in data]


def remove_label(repo: str, number: int, label: str) -> dict:
    """Remove a single label from an issue or PR.
    Returns {'number': ..., 'labels_remaining': [...]}."""
    owner, name = _split_repo(repo)
    # GitHub returns the remaining labels after removal.
    data = _request_write("DELETE",
                          f"/repos/{owner}/{name}/issues/{number}/labels/{label}")
    remaining = [lbl["name"] for lbl in data] if isinstance(data, list) else []
    return {"number": number, "labels_remaining": remaining}


def assign_users(repo: str, number: int, assignees: list[str]) -> list[str]:
    """Add assignees to an issue or PR. Returns the full assignee list after the call."""
    owner, name = _split_repo(repo)
    data = _request_write("POST", f"/repos/{owner}/{name}/issues/{number}/assignees",
                          {"assignees": assignees})
    return [_trim_user(a) for a in data.get("assignees", [])]


def unassign_users(repo: str, number: int, assignees: list[str]) -> list[str]:
    """Remove assignees from an issue or PR. Returns the remaining assignee list."""
    owner, name = _split_repo(repo)
    data = _request_write("DELETE", f"/repos/{owner}/{name}/issues/{number}/assignees",
                          {"assignees": assignees})
    return [_trim_user(a) for a in data.get("assignees", [])]


def request_pr_reviewers(repo: str, number: int, reviewers: list[str]) -> list[str]:
    """Request one or more reviewers on a PR. Returns the requested-reviewer list."""
    owner, name = _split_repo(repo)
    data = _request_write(
        "POST", f"/repos/{owner}/{name}/pulls/{number}/requested_reviewers",
        {"reviewers": reviewers},
    )
    return [_trim_user(r) for r in data.get("requested_reviewers", [])]


def create_issue(repo: str, title: str, body: str = "",
                 labels: list[str] | None = None,
                 assignees: list[str] | None = None) -> dict:
    """Create a new issue. Returns {'number': ..., 'url': ..., 'state': 'open'}."""
    owner, name = _split_repo(repo)
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    data = _request_write("POST", f"/repos/{owner}/{name}/issues", payload)
    return {
        "number": data.get("number"),
        "url": data.get("html_url"),
        "state": data.get("state", "open"),
    }


def create_pull_request(repo: str, title: str, head: str, base: str,
                        body: str = "", draft: bool = False) -> dict:
    """Open a new pull request from head -> base.
    head: branch name (or 'fork:branch' for cross-fork). Returns trimmed PR dict."""
    owner, name = _split_repo(repo)
    data = _request_write("POST", f"/repos/{owner}/{name}/pulls", {
        "title": title,
        "head": head,
        "base": base,
        "body": body,
        "draft": draft,
    })
    trimmed = _trim_pr(data)
    trimmed["body"] = (data.get("body") or "")[:2000]
    return trimmed


# ---------------------------------------------------------------------------
# Write functions — gated (high blast-radius, require ToolApproval policy)
# ---------------------------------------------------------------------------

def set_issue_state(repo: str, number: int, state: str) -> dict:
    """state: 'open' or 'closed'. Works for issues and PRs (does NOT merge a PR)."""
    if state not in ("open", "closed"):
        raise GitHubError("state must be 'open' or 'closed'.")
    owner, name = _split_repo(repo)
    data = _request_write("PATCH", f"/repos/{owner}/{name}/issues/{number}",
                          {"state": state})
    return {"number": data.get("number"), "state": data.get("state")}


def merge_pull_request(repo: str, number: int,
                       merge_method: str = "merge") -> dict:
    """merge_method: 'merge' | 'squash' | 'rebase'. HIGH BLAST RADIUS — gated
    behind a ToolApproval policy at the agent layer.
    Returns {'merged': bool, 'message': str}."""
    if merge_method not in ("merge", "squash", "rebase"):
        raise GitHubError("merge_method must be 'merge', 'squash', or 'rebase'.")
    owner, name = _split_repo(repo)
    data = _request_write("PUT", f"/repos/{owner}/{name}/pulls/{number}/merge",
                          {"merge_method": merge_method})
    return {"merged": data.get("merged", False), "message": data.get("message", "")}
