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
# Review-latency signal (point 3 — review-quality metric)
# ---------------------------------------------------------------------------

def _parse_iso(ts: str | None):
    """Parse a GitHub ISO8601 timestamp ('…Z') into an aware datetime, or None."""
    if not ts:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _avg_first_review_latency_hours(repo: str, open_prs: list[dict]) -> float | None:
    """
    Average time (hours) from a PR opening to its first review, across the
    currently-open non-draft PRs that have received at least one review.
    Returns None when no open PR has been reviewed yet. Costs one reviews
    call per open non-draft PR — fine for the small watched-repo set.
    """
    latencies: list[float] = []
    for pr in open_prs:
        if pr.get("draft"):
            continue
        opened = _parse_iso(pr.get("created_at"))
        if opened is None:
            continue
        try:
            reviews = gh.get_pr_reviews(repo, pr["number"])
        except gh.GitHubError:
            continue
        review_times = [t for t in (_parse_iso(r.get("submitted_at")) for r in reviews) if t]
        if not review_times:
            continue
        first = min(review_times)
        hours = (first - opened).total_seconds() / 3600.0
        if hours >= 0:
            latencies.append(hours)
    if not latencies:
        return None
    return round(sum(latencies) / len(latencies), 1)


# ---------------------------------------------------------------------------
# Digest formatter
# ---------------------------------------------------------------------------

def compute_digest_stats(repo: str, include_review_latency: bool = False) -> dict:
    """
    Fetch current repo state and return the raw counts used by the digest.
    Split out from build_digest so both the message and the persisted trend
    snapshot come from one computation. Raises GitHubError on any API failure.
    """
    open_issues   = gh.list_issues(repo, state="open",   limit=100)
    closed_issues = gh.list_issues(repo, state="closed", limit=100)
    open_prs      = gh.list_pull_requests(repo, state="open",   limit=100)
    closed_prs    = gh.list_pull_requests(repo, state="closed", limit=100)

    label_counts: dict[str, int] = {}
    for issue in open_issues:
        for label in issue.get("labels", []):
            label_counts[label] = label_counts.get(label, 0) + 1
    top_labels = sorted(label_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    draft_prs     = [p for p in open_prs if p.get("draft")]
    review_needed = [p for p in open_prs
                     if not p.get("draft") and not p.get("requested_reviewers")]

    latency = (_avg_first_review_latency_hours(repo, open_prs)
               if include_review_latency else None)

    return {
        "open_issues": len(open_issues),
        "closed_issues": len(closed_issues),
        "open_prs": len(open_prs),
        "closed_prs": len(closed_prs),   # closed includes merged on this endpoint
        "draft_prs": len(draft_prs),
        "review_needed": len(review_needed),
        "avg_review_latency_hours": latency,
        # kept for rendering only (not persisted)
        "_top_labels": top_labels,
        "_draft_pr_list": draft_prs,
        "_review_needed_list": review_needed,
        "_has_open_prs": bool(open_prs),
    }


def _fmt_delta(current: float | int | None, previous: float | int | None,
               suffix: str = "") -> str:
    """Render a signed delta with a directional arrow, e.g. '▲2' or '▼1.2h'."""
    if current is None or previous is None:
        return ""
    diff = round(current - previous, 1)
    if diff == 0:
        return "±0"
    arrow = "▲" if diff > 0 else "▼"
    return f"{arrow}{abs(diff)}{suffix}"


def _render_digest(repo: str, stats: dict, previous: dict | None) -> str:
    top_labels = stats["_top_labels"]
    draft_prs = stats["_draft_pr_list"]
    review_needed = stats["_review_needed_list"]

    lines = [
        f":bar_chart: *Daily repo digest — `{repo}`*",
        "",
        "*Issues*",
        f"  • Open: *{stats['open_issues']}*   |   Closed: *{stats['closed_issues']}*",
    ]

    if top_labels:
        label_str = "   ".join(f"`{lbl}` ×{cnt}" for lbl, cnt in top_labels)
        lines.append(f"  • Top labels: {label_str}")
    else:
        lines.append("  • No labels on open issues")

    lines += [
        "",
        "*Pull Requests*",
        f"  • Open: *{stats['open_prs']}*   |   Closed/Merged: *{stats['closed_prs']}*",
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

    if not stats["_has_open_prs"]:
        lines.append("  • No open PRs :white_check_mark:")

    if stats.get("avg_review_latency_hours") is not None:
        lines.append(f"  • Avg time-to-first-review: *{stats['avg_review_latency_hours']}h*")

    # --- trend deltas vs the last stored snapshot ---
    if previous:
        captured = previous.get("captured_at")
        when = captured.strftime("%b %-d") if hasattr(captured, "strftime") else "last digest"
        parts = []
        oi = _fmt_delta(stats["open_issues"], previous.get("open_issues"))
        op = _fmt_delta(stats["open_prs"], previous.get("open_prs"))
        rl = _fmt_delta(stats.get("avg_review_latency_hours"),
                        previous.get("avg_review_latency_hours"), suffix="h")
        if oi:
            parts.append(f"open issues {oi}")
        if op:
            parts.append(f"open PRs {op}")
        if rl:
            parts.append(f"review latency {rl}")
        if parts:
            lines += ["", f":chart_with_upwards_trend: _Since {when}: " + ", ".join(parts) + "_"]

    return "\n".join(lines)


def build_digest(repo: str, previous: dict | None = None,
                 include_review_latency: bool = False) -> str:
    """
    Fetch current repo state from GitHub and return a formatted Slack message.
    Pass `previous` (a prior snapshot dict) to append trend deltas, and
    `include_review_latency=True` to add the avg time-to-first-review line.
    Raises GitHubError on any API failure.
    """
    stats = compute_digest_stats(repo, include_review_latency=include_review_latency)
    return _render_digest(repo, stats, previous)


# ---------------------------------------------------------------------------
# Scheduled + on-demand posting
# ---------------------------------------------------------------------------

def post_digest(repo: str, post_fn: Callable[[str, str], None],
                with_trend: bool = True) -> None:
    """Fetch and post the digest for one repo. Logs on error, never raises.

    When `with_trend` is set (the scheduled path), the digest includes review
    latency + deltas vs the last stored snapshot, and persists a fresh snapshot
    afterwards. DB access is best-effort — a missing/unavailable DB downgrades
    gracefully to a plain digest rather than failing the post."""
    channel = _channel()
    if not channel:
        log.warning("[git_digest] GIT_WATCH_CHANNEL not set, skipping digest for %s", repo)
        return

    previous = None
    if with_trend:
        try:
            from .db import get_last_digest_snapshot
            previous = get_last_digest_snapshot(repo)
        except Exception as e:
            log.warning("[git_digest] could not load previous snapshot for %s: %s", repo, e)

    try:
        stats = compute_digest_stats(repo, include_review_latency=with_trend)
        message = _render_digest(repo, stats, previous)
        post_fn(channel, message)
        log.info("[git_digest] posted digest for %s", repo)
    except gh.GitHubError as e:
        log.warning("[git_digest] GitHub error for %s: %s", repo, e)
        return
    except Exception:
        log.exception("[git_digest] unexpected error posting digest for %s", repo)
        return

    if with_trend:
        try:
            from .db import insert_digest_snapshot
            insert_digest_snapshot(repo, stats)
        except Exception as e:
            log.warning("[git_digest] could not persist snapshot for %s: %s", repo, e)


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
