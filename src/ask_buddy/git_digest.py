"""
Daily GitHub repo digest for Ask Buddy.

Posts a structured summary of each watched repo's current state to
GIT_WATCH_CHANNEL on a configurable cron schedule (default: 9 AM every day).
Unlike the triage watcher (git_watch.py) which only reports NEW items,
the digest always reflects the full current state:

  - Total open issues / closed issues
  - Open issues breakdown by label (top 5)
  - Total open PRs / merged PRs (last 30 days)
  - PRs waiting for review (no approvals yet)
  - Draft PRs

Can also be triggered on-demand via the /askbuddy slash command:
  /askbuddy git digest
  /askbuddy git digest owner/repo

Controlled by env vars:
  GIT_DIGEST_CRON        — 5-field cron, default "0 9 * * *" (9 AM every day)
  GIT_DIGEST_TIMEZONE    — IANA timezone, default "America/Los_Angeles"
  GIT_DIGEST_TIMES       — convenience alternative: "9:00,17:00" posts twice a day
                           (overrides GIT_DIGEST_CRON if set)
  GIT_WATCH_REPOS        — shared with triage watcher (same repos)
  GIT_WATCH_CHANNEL      — shared with triage watcher (same channel)
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import github_client as gh

log = logging.getLogger("ask_buddy.git_digest")

_scheduler: BackgroundScheduler | None = None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _watched_repos() -> list[str]:
    raw = os.environ.get("GIT_WATCH_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _channel() -> str:
    return os.environ.get("GIT_WATCH_CHANNEL", "").strip()


def _cron() -> str:
    return os.environ.get("GIT_DIGEST_CRON", "0 9 * * *").strip()


def _timezone() -> str:
    return os.environ.get("GIT_DIGEST_TIMEZONE", "America/Los_Angeles").strip()


def _digest_times() -> list[str]:
    """Parse GIT_DIGEST_TIMES='9:00,17:00' into ['9:00', '17:00']."""
    raw = os.environ.get("GIT_DIGEST_TIMES", "").strip()
    return [t.strip() for t in raw.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Digest formatter
# ---------------------------------------------------------------------------

def build_digest(repo: str) -> str:
    """
    Fetch current repo state from GitHub and return a formatted Slack message.
    Raises GitHubError on any API failure.
    """
    open_issues   = gh.list_issues(repo, state="open",   limit=100)
    closed_issues = gh.list_issues(repo, state="closed", limit=100)
    open_prs      = gh.list_pull_requests(repo, state="open",   limit=100)
    closed_prs    = gh.list_pull_requests(repo, state="closed", limit=100)

    # --- issue stats ---
    total_open_issues   = len(open_issues)
    total_closed_issues = len(closed_issues)

    # label breakdown (top 5 by frequency)
    label_counts: dict[str, int] = {}
    for issue in open_issues:
        for label in issue.get("labels", []):
            label_counts[label] = label_counts.get(label, 0) + 1
    top_labels = sorted(label_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # --- PR stats ---
    total_open_prs   = len(open_prs)
    total_closed_prs = len(closed_prs)   # closed includes merged on this endpoint
    draft_prs        = [p for p in open_prs if p.get("draft")]
    review_needed    = [p for p in open_prs
                        if not p.get("draft") and not p.get("requested_reviewers")]

    # --- format ---
    lines = [
        f":bar_chart: *Daily repo digest — `{repo}`*",
        "",
        "*Issues*",
        f"  • Open: *{total_open_issues}*   |   Closed: *{total_closed_issues}*",
    ]

    if top_labels:
        label_str = "   ".join(f"`{lbl}` ×{cnt}" for lbl, cnt in top_labels)
        lines.append(f"  • Top labels: {label_str}")
    else:
        lines.append("  • No labels on open issues")

    lines += [
        "",
        "*Pull Requests*",
        f"  • Open: *{total_open_prs}*   |   Closed/Merged: *{total_closed_prs}*",
    ]

    if draft_prs:
        lines.append(f"  • Drafts ({len(draft_prs)}): "
                     + ", ".join(f"#{p['number']}" for p in draft_prs))

    if review_needed:
        lines.append(f"  • Waiting for reviewer ({len(review_needed)}): "
                     + ", ".join(
                         f"#{p['number']} _{p['title'][:40]}_"
                         for p in review_needed
                     ))

    if not open_prs:
        lines.append("  • No open PRs :white_check_mark:")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scheduled + on-demand posting
# ---------------------------------------------------------------------------

def post_digest(repo: str, post_fn: Callable[[str, str], None]) -> None:
    """Fetch and post the digest for one repo. Logs on error, never raises."""
    channel = _channel()
    if not channel:
        log.warning("[git_digest] GIT_WATCH_CHANNEL not set, skipping digest for %s", repo)
        return
    try:
        message = build_digest(repo)
        post_fn(channel, message)
        log.info("[git_digest] posted digest for %s", repo)
    except gh.GitHubError as e:
        log.warning("[git_digest] GitHub error for %s: %s", repo, e)
    except Exception:
        log.exception("[git_digest] unexpected error posting digest for %s", repo)


def post_all_digests(post_fn: Callable[[str, str], None]) -> None:
    """Post digests for all repos in GIT_WATCH_REPOS."""
    for repo in _watched_repos():
        post_digest(repo, post_fn)


def start_git_digest(slack_post_fn: Callable[[str, str], None]) -> BackgroundScheduler | None:
    """
    Start the digest scheduler. No-op (returns None) if GITHUB_TOKEN,
    GIT_WATCH_REPOS, or GIT_WATCH_CHANNEL are unset.

    Schedule is controlled by (in priority order):
      1. GIT_DIGEST_TIMES=9:00,17:00  — post at these times every day
      2. GIT_DIGEST_CRON=0 9 * * *    — any valid 5-field cron expression
    Default: 9:00 AM every day in America/Los_Angeles.
    """
    global _scheduler

    if not os.environ.get("GITHUB_TOKEN"):
        log.info("[git_digest] GITHUB_TOKEN unset — digest disabled.")
        return None
    if not _watched_repos():
        log.info("[git_digest] GIT_WATCH_REPOS empty — digest disabled.")
        return None
    if not _channel():
        log.info("[git_digest] GIT_WATCH_CHANNEL unset — digest disabled.")
        return None

    tz = _timezone()
    _scheduler = BackgroundScheduler()

    times = _digest_times()
    if times:
        # e.g. GIT_DIGEST_TIMES=9:00,17:00 → two jobs per day
        for idx, t in enumerate(times):
            try:
                hour, minute = t.split(":")
                _scheduler.add_job(
                    post_all_digests,
                    trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
                    args=[slack_post_fn],
                    id=f"git-digest-{idx}",
                    replace_existing=True,
                )
                log.info("[git_digest] scheduled at %s %s", t, tz)
            except ValueError:
                log.warning("[git_digest] could not parse GIT_DIGEST_TIMES entry %r, skipping", t)
    else:
        cron = _cron()
        _scheduler.add_job(
            post_all_digests,
            trigger=CronTrigger.from_crontab(cron, timezone=tz),
            args=[slack_post_fn],
            id="git-digest",
            replace_existing=True,
        )
        log.info("[git_digest] scheduled via cron '%s' %s", cron, tz)

    _scheduler.start()
    log.info("[git_digest] started for repos=%s channel=%s",
             _watched_repos(), _channel())
    return _scheduler
