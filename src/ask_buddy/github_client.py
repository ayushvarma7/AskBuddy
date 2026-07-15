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
