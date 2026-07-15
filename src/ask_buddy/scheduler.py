"""
In-process reminder scheduler for Ask Buddy.

Uses APScheduler's BackgroundScheduler with cron-style triggers so recurring
broadcast reminders (e.g. "every weekday at 9AM PST") fire without any
external cron process. Reminders are persisted in Postgres
(ask_buddy_reminders) so start_scheduler() can reload every active row on
boot — a bot restart doesn't lose scheduled reminders.

Caveat: this only fires while the bot process is running. A reminder whose
fire time falls while the bot is down is simply skipped, not queued/caught up.
"""

from __future__ import annotations

import logging
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import get_active_reminders

log = logging.getLogger("ask_buddy.scheduler")

_scheduler: BackgroundScheduler | None = None
_slack_post_fn: Callable[[str, str], None] | None = None


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
    _scheduler.add_job(
        _fire,
        trigger=trigger,
        id=_job_id(reminder_id),
        args=[reminder_id, channel_id, message],
        replace_existing=True,
    )


def start_scheduler(slack_post_fn: Callable[[str, str], None]) -> BackgroundScheduler:
    """
    Start the background scheduler and load all active reminders from the DB.

    Call once at bot startup. `slack_post_fn(channel_id, text)` is used to
    deliver fired reminders — pass a plain chat_postMessage wrapper, not the
    feedback-button-attaching one (reminders aren't rated Q&A answers).
    """
    global _scheduler, _slack_post_fn
    _slack_post_fn = slack_post_fn
    _scheduler = BackgroundScheduler()
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
