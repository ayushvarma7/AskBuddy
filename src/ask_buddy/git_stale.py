"""
Stale-PR nudger for Ask Buddy (proactive GitHub intelligence).

Where git_watch.py reports NEW items and git_digest.py reports STATE, this
module reports NEGLECT: open, non-draft pull requests that have been waiting
for review longer than GIT_STALE_PR_DAYS with no approval yet.

The novel bit: instead of shouting into a channel, it DMs the *human* behind
each requested reviewer by resolving their GitHub login back to a Slack user
via ask_buddy_git_identities (the mapping populated by `/askbuddy link
github`). PRs whose reviewers aren't linked (or that have no requested
reviewer) fall back to a single channel post.

Dedup: ask_buddy_pr_nudges stores the last nudge time per PR so a reviewer
isn't pinged on every interval — only once per GIT_STALE_COOLDOWN_HOURS.

Caveat (same as scheduler.py / git_watch.py): only runs while the bot is up.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable

import psycopg2

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import github_client as gh

log = logging.getLogger("ask_buddy.git_stale")

_scheduler: BackgroundScheduler | None = None


def _watched_repos() -> list[str]:
    raw = os.environ.get("GIT_WATCH_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _channel() -> str:
    return os.environ.get("GIT_WATCH_CHANNEL", "").strip()


def _stale_days() -> float:
    try:
        return max(0.0, float(os.environ.get("GIT_STALE_PR_DAYS", "3")))
    except ValueError:
        return 3.0


def _cooldown_hours() -> float:
    try:
        return max(1.0, float(os.environ.get("GIT_STALE_COOLDOWN_HOURS", "48")))
    except ValueError:
        return 48.0


def _interval_hours() -> float:
    try:
        return max(1.0, float(os.environ.get("GIT_STALE_INTERVAL_HOURS", "12")))
    except ValueError:
        return 12.0


def _parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_days(pr: dict) -> float | None:
    opened = _parse_iso(pr.get("created_at"))
    if opened is None:
        return None
    return (datetime.now(timezone.utc) - opened).total_seconds() / 86400.0


def _has_approval(repo: str, number: int) -> bool:
    """True if the latest review from any reviewer is an approval."""
    try:
        reviews = gh.get_pr_reviews(repo, number)
    except gh.GitHubError:
        return False
    latest_by_reviewer: dict[str, str] = {}
    for r in reviews:
        latest_by_reviewer[r.get("reviewer", "")] = r.get("state", "")
    return any(state == "APPROVED" for state in latest_by_reviewer.values())


def _nudge_targets(pr: dict) -> tuple[list[str], list[str]]:
    """
    Split a PR's requested reviewers into (slack_dm_channels, unlinked_logins).
    Slack DM channels are just the reviewer's Slack user id (chat_postMessage
    treats a user id as a DM channel).
    """
    from .db import get_slack_user_for_github_login

    dm_targets: list[str] = []
    unlinked: list[str] = []
    for login in pr.get("requested_reviewers", []):
        slack_id = None
        try:
            slack_id = get_slack_user_for_github_login(login)
        except (psycopg2.OperationalError, RuntimeError):
            slack_id = None
        if slack_id:
            dm_targets.append(slack_id)
        else:
            unlinked.append(login)
    return dm_targets, unlinked


def _dm_text(repo: str, pr: dict, age_days: float) -> str:
    return (
        f":hourglass_flowing_sand: *A review is waiting on you.*\n"
        f"PR #{pr['number']} in `{repo}` has been open *{age_days:.0f} days* "
        f"with no approval yet.\n"
        f"• *{pr['title']}*\n"
        f"• {pr.get('url', '')}\n"
        f"_Reply in the PR or approve/request changes when you get a moment._"
    )


def _channel_text(repo: str, pr: dict, age_days: float, unlinked: list[str]) -> str:
    who = ""
    if unlinked:
        who = " (reviewers: " + ", ".join(f"`{u}`" for u in unlinked) + ")"
    return (
        f":hourglass_flowing_sand: *Stale PR — review needed{who}*\n"
        f"#{pr['number']} *{pr['title']}* in `{repo}` — open {age_days:.0f} days, "
        f"no approval yet. {pr.get('url', '')}"
    )


def _recently_nudged(repo: str, number: int) -> bool:
    from .db import get_last_nudge_epoch
    try:
        last = get_last_nudge_epoch(repo, number)
    except (psycopg2.OperationalError, RuntimeError):
        # DB unavailable — don't nudge (safer than spamming without dedup).
        return True
    if last is None:
        return False
    return (time.time() - last) < _cooldown_hours() * 3600.0


def check_repo(repo: str, post_fn: Callable[[str, str], None]) -> int:
    """
    Nudge stale open PRs in one repo. Returns the number of PRs nudged.
    Never raises — logs and skips on GitHub / DB errors.
    """
    from .db import record_pr_nudge

    threshold = _stale_days()
    try:
        open_prs = gh.list_pull_requests(repo, state="open", limit=100)
    except gh.GitHubError as e:
        if "rate limit" in str(e).lower():
            log.warning("[git_stale] rate limited — skipping %s: %s", repo, e)
        else:
            log.warning("[git_stale] could not list PRs for %s: %s", repo, e)
        return 0

    nudged = 0
    channel = _channel()
    for pr in open_prs:
        if pr.get("draft"):
            continue
        age = _age_days(pr)
        if age is None or age < threshold:
            continue
        if _recently_nudged(repo, pr["number"]):
            continue
        if _has_approval(repo, pr["number"]):
            continue

        dm_targets, unlinked = _nudge_targets(pr)
        delivered = False
        for slack_id in dm_targets:
            try:
                post_fn(slack_id, _dm_text(repo, pr, age))
                delivered = True
            except Exception:
                log.exception("[git_stale] failed DM to %s for %s#%s",
                              slack_id, repo, pr["number"])
        # Fall back to a channel post when nobody linked could be DM'd.
        if (not dm_targets or unlinked) and channel:
            try:
                post_fn(channel, _channel_text(repo, pr, age, unlinked))
                delivered = True
            except Exception:
                log.exception("[git_stale] failed channel post for %s#%s",
                              repo, pr["number"])

        if delivered:
            nudged += 1
            try:
                record_pr_nudge(repo, pr["number"])
            except (psycopg2.OperationalError, RuntimeError) as e:
                log.warning("[git_stale] could not record nudge for %s#%s: %s",
                            repo, pr["number"], e)

    if nudged:
        log.info("[git_stale] nudged %d stale PR(s) in %s", nudged, repo)
    return nudged


def _check_all(post_fn: Callable[[str, str], None]) -> None:
    for repo in _watched_repos():
        check_repo(repo, post_fn)


def start_git_stale(slack_post_fn: Callable[[str, str], None]) -> BackgroundScheduler | None:
    """Start the stale-PR nudge scheduler. No-op (returns None) if GITHUB_TOKEN
    or GIT_WATCH_REPOS are unset. Runs every GIT_STALE_INTERVAL_HOURS."""
    global _scheduler
    if not os.environ.get("GITHUB_TOKEN"):
        log.info("[git_stale] GITHUB_TOKEN unset — stale-PR nudger disabled.")
        return None
    if not _watched_repos():
        log.info("[git_stale] GIT_WATCH_REPOS empty — stale-PR nudger disabled.")
        return None

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _check_all,
        trigger=IntervalTrigger(hours=_interval_hours()),
        args=[slack_post_fn],
        id="git-stale-nudge",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("[git_stale] started: repos=%s stale>=%.1fd cooldown=%.0fh every %.0fh",
             _watched_repos(), _stale_days(), _cooldown_hours(), _interval_hours())
    return _scheduler
