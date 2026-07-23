"""
Proactive per-user digest for Ask Buddy (point 5 — push, not just pull).

Once a week (or on demand) DMs each Slack user who has linked a GitHub account
a short, personalised brief:

  • Your PRs that need attention — open PRs you authored or were asked to
    review across the watched repos.
  • A rotating "policy spotlight" — one snippet from the HR/IT corpus, so the
    docs stay in front of people instead of only being pulled on demand.

Only users with something to report get a DM (no empty-inbox spam). Uses the
ask_buddy_git_identities mapping to know who to message and which GitHub login
to query for.

Caveat (same as scheduler.py): only fires while the bot process is running.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from . import github_client as gh

log = logging.getLogger("ask_buddy.personal_digest")


def _watched_repos() -> list[str]:
    raw = os.environ.get("GIT_WATCH_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _repo_qualifier() -> str:
    """GitHub search qualifier restricting to the watched repos (OR'd)."""
    return " ".join(f"repo:{r}" for r in _watched_repos())


def _prs_for_login(login: str) -> tuple[list[dict], list[dict]]:
    """Return (review_requested, authored) open PRs for a GitHub login,
    scoped to the watched repos. Best-effort — returns ([],[]) on error."""
    repos = _repo_qualifier()
    if not repos:
        return [], []
    try:
        review = gh.search_issues(f"{repos} is:open is:pr review-requested:{login}", limit=10)
        authored = gh.search_issues(f"{repos} is:open is:pr author:{login}", limit=10)
    except gh.GitHubError as e:
        log.warning("[personal_digest] search failed for %s: %s", login, e)
        return [], []
    return review, authored


def _policy_spotlight() -> str | None:
    """One rotating policy snippet, or None if the corpus/DB is unavailable."""
    try:
        from .db import get_random_chunk_spotlight
        row = get_random_chunk_spotlight()
    except Exception:
        return None
    if not row:
        return None
    snippet = " ".join((row.get("chunk_text") or "").split())[:220]
    section = row.get("section") or ""
    src = row.get("source_filename") or ""
    return f":bulb: *Policy spotlight — {section or src}*\n>{snippet}…\n_Source: {src}_"


def build_user_digest(login: str) -> str | None:
    """Build one user's digest text, or None if there's nothing to say."""
    review, authored = _prs_for_login(login)
    spotlight = _policy_spotlight()

    if not review and not authored and not spotlight:
        return None

    lines = ["👋 *Your weekly Ask Buddy brief*"]
    if review:
        lines.append(f"\n*:eyes: PRs waiting on your review ({len(review)}):*")
        for pr in review[:5]:
            lines.append(f"  • #{pr['number']} {pr['title'][:70]} — {pr.get('url', '')}")
    if authored:
        lines.append(f"\n*:writing_hand: Your open PRs ({len(authored)}):*")
        for pr in authored[:5]:
            lines.append(f"  • #{pr['number']} {pr['title'][:70]} — {pr.get('url', '')}")
    if spotlight:
        lines.append("\n" + spotlight)
    return "\n".join(lines)


def post_all_user_digests(post_fn: Callable[[str, str], None]) -> int:
    """DM every linked user their personalised brief. Returns the number sent."""
    try:
        from .db import all_linked_slack_users
        users = all_linked_slack_users()
    except Exception as e:
        log.warning("[personal_digest] could not load linked users: %s", e)
        return 0

    sent = 0
    for u in users:
        try:
            text = build_user_digest(u["github_login"])
        except Exception:
            log.exception("[personal_digest] failed building digest for %s", u)
            continue
        if not text:
            continue
        try:
            post_fn(u["slack_user_id"], text)   # user id == DM channel
            sent += 1
        except Exception:
            log.exception("[personal_digest] failed DM to %s", u["slack_user_id"])
    log.info("[personal_digest] sent %d personalised digest(s)", sent)
    return sent


def start_personal_digest(slack_post_fn: Callable[[str, str], None]):
    """Start the weekly personal-digest scheduler. No-op (returns None) unless
    GITHUB_TOKEN + GIT_WATCH_REPOS are set. Cron via
    ASK_BUDDY_PERSONAL_DIGEST_CRON (default Monday 09:00 America/Los_Angeles)."""
    if not os.environ.get("GITHUB_TOKEN"):
        log.info("[personal_digest] GITHUB_TOKEN unset — personal digest disabled.")
        return None
    if not _watched_repos():
        log.info("[personal_digest] GIT_WATCH_REPOS empty — personal digest disabled.")
        return None

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    cron = os.environ.get("ASK_BUDDY_PERSONAL_DIGEST_CRON", "0 9 * * 1").strip()
    tz = os.environ.get("ASK_BUDDY_PERSONAL_DIGEST_TIMEZONE", "America/Los_Angeles").strip()
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        post_all_user_digests,
        trigger=CronTrigger.from_crontab(cron, timezone=tz),
        args=[slack_post_fn],
        id="ask-buddy-personal-digest",
        replace_existing=True,
    )
    scheduler.start()
    log.info("[personal_digest] scheduled via cron '%s' %s", cron, tz)
    return scheduler
