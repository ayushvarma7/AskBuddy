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
