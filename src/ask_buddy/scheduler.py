"""
Reminder scheduler for Ask Buddy.

Uses APScheduler with cron-style triggers so recurring broadcast reminders
(e.g. "every weekday at 9AM PST") fire without any external cron process.
Reminders are persisted in Postgres (ask_buddy_reminders) and reloaded by
start_scheduler() on boot, so a restart doesn't lose them.

Missed fires
------------
The job store itself is also backed by Postgres, and jobs carry a misfire
grace period (ASK_BUDDY_MISFIRE_GRACE_SECONDS, default 1 hour). A reminder
whose fire time passes while the bot is down therefore still fires once on the
next start, instead of being silently skipped. `coalesce=True` collapses a
backlog into a single run — coming back after a long outage posts one reminder,
not one per missed slot.

Anything longer than the grace period is genuinely dropped, which is the
intended behaviour: a stand-up reminder from three days ago is noise.

Only this scheduler is persistent. The triage watcher and digest schedulers
take a Slack post function as a job argument, which cannot be pickled into a
job store, and they need no catch-up — they poll current state.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import get_active_reminders

log = logging.getLogger("ask_buddy.scheduler")

_scheduler: BackgroundScheduler | None = None
_slack_post_fn: Callable[[str, str], None] | None = None

# Table APScheduler owns for its own job rows. Distinct from
# ask_buddy_reminders, which is our source of truth for what a reminder says;
# this one only holds scheduling state.
_JOBS_TABLE = "ask_buddy_scheduler_jobs"


def _misfire_grace_seconds() -> int:
    try:
        return max(1, int(os.environ.get("ASK_BUDDY_MISFIRE_GRACE_SECONDS", "3600")))
    except ValueError:
        return 3600


def _build_scheduler() -> BackgroundScheduler:
    """
    BackgroundScheduler with a Postgres job store when one is reachable.

    Falls back to the default in-memory store if SQLAlchemy is missing or the
    DSN is unset, so the bot still schedules reminders for the current process
    rather than refusing to start.
    """
    job_defaults = {
        "coalesce": True,
        "misfire_grace_time": _misfire_grace_seconds(),
        "max_instances": 1,
    }
    dsn = os.environ.get("ASK_BUDDY_DB_DSN")
    if dsn:
        try:
            from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
            jobstores = {"default": SQLAlchemyJobStore(url=dsn, tablename=_JOBS_TABLE)}
            log.info("[scheduler] using persistent job store (%s)", _JOBS_TABLE)
            return BackgroundScheduler(jobstores=jobstores, job_defaults=job_defaults)
        except Exception as exc:
            log.warning("[scheduler] persistent job store unavailable (%s) — "
                        "falling back to in-memory; reminders missed while the "
                        "bot is down will not be caught up", exc)
    return BackgroundScheduler(job_defaults=job_defaults)


def _job_id(reminder_id: int) -> str:
    return f"reminder-{reminder_id}"


def _fire(reminder_id: int, channel_id: str, message: str) -> None:
    if _slack_post_fn is None:
        log.warning("[scheduler] no post fn registered, dropping reminder %s", reminder_id)
        return
    try:
        _slack_post_fn(channel_id, message)
        log.info("[scheduler] fired reminder id=%s -> channel=%s", reminder_id, channel_id)
    except Exception:
        log.exception("[scheduler] failed to post reminder id=%s", reminder_id)


def _add_job(reminder_id: int, channel_id: str, message: str,
             cron_expression: str, timezone: str) -> None:
    if _scheduler is None:
        raise RuntimeError("Scheduler not started — call start_scheduler() first.")
    trigger = CronTrigger.from_crontab(cron_expression, timezone=timezone)
    # replace_existing keeps the boot-time reload idempotent: with a persistent
    # job store the row is already there, and re-adding must update it rather
    # than raising ConflictingIdError.
    _scheduler.add_job(
        _fire,
        trigger=trigger,
        id=_job_id(reminder_id),
        args=[reminder_id, channel_id, message],
        replace_existing=True,
        misfire_grace_time=_misfire_grace_seconds(),
        coalesce=True,
    )


def start_scheduler(slack_post_fn: Callable[[str, str], None]) -> BackgroundScheduler:
    """
    Start the background scheduler and load all active reminders from the DB.

    Call once at bot startup. `slack_post_fn(channel_id, text)` is used to
    deliver fired reminders — pass a plain chat_postMessage wrapper, not the
    feedback-button-attaching one (reminders aren't rated Q&A answers).

    ask_buddy_reminders stays the source of truth: every active row is
    re-registered here, so the DB and the job store cannot drift apart even if
    the job store is wiped.
    """
    global _scheduler, _slack_post_fn
    _slack_post_fn = slack_post_fn
    _scheduler = _build_scheduler()
    _scheduler.start()

    reminders = get_active_reminders()
    for r in reminders:
        _add_job(r["id"], r["channel_id"], r["message"],
                 r["cron_expression"], r["timezone"])

    log.info("[scheduler] started with %d active reminder(s)", len(reminders))
    return _scheduler


def schedule_new_reminder(reminder_id: int, channel_id: str, message: str,
                          cron_expression: str, timezone: str) -> None:
    """Register a newly created reminder with the already-running scheduler."""
    _add_job(reminder_id, channel_id, message, cron_expression, timezone)


def unschedule_reminder(reminder_id: int) -> None:
    """Remove a cancelled reminder's job. No-op if it isn't currently scheduled."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(reminder_id))
    except Exception:
        pass
