"""
Per-user throttling and a daily request ceiling.

Every message costs an embedding call plus one or more LLM calls, and nothing
stopped a single user — or a loop someone wired up by accident — from spending
the whole month's budget in an afternoon. Two independent limits:

  per-user   a sliding window: at most N requests per user per window
  daily cap  a process-wide ceiling on total requests per calendar day (UTC)

Both are opt-in and default to permissive, so an existing deployment behaves
exactly as before until the env vars are set:

    ASK_BUDDY_RATE_LIMIT_PER_USER    max requests per user per window (0 = off)
    ASK_BUDDY_RATE_LIMIT_WINDOW_SEC  window length in seconds (default 60)
    ASK_BUDDY_DAILY_REQUEST_CAP      max requests per UTC day (0 = off)

State is in-memory and per-process, which matches how the rest of the bot's
runtime state works today (supervisor cache, pending approvals). A restart
clears the counters; with one bot process that is the whole picture.

Stdlib-only, so it is testable offline and cheap on the hot path.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Decision:
    """Outcome of a limit check."""
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0

    def slack_message(self) -> str:
        """User-facing explanation. Deliberately plain — a throttled user
        should understand what happened without reading the logs."""
        if self.allowed:
            return ""
        if self.reason == "per_user":
            wait = max(1, self.retry_after_seconds)
            return (f"⏳ You've sent a few requests in quick succession. "
                    f"Try again in about {wait}s.")
        if self.reason == "daily_cap":
            return ("🛑 Ask Buddy has hit its daily request limit. "
                    "It'll reset at midnight UTC — reach out to whoever runs "
                    "the bot if you need the cap raised.")
        return "⏳ Ask Buddy is rate limited right now — please try again shortly."


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(0, value)


def per_user_limit() -> int:
    return _int_env("ASK_BUDDY_RATE_LIMIT_PER_USER", 0)


def window_seconds() -> int:
    return max(1, _int_env("ASK_BUDDY_RATE_LIMIT_WINDOW_SEC", 60))


def daily_cap() -> int:
    return _int_env("ASK_BUDDY_DAILY_REQUEST_CAP", 0)


class RateLimiter:
    """Sliding-window per-user limiter plus a UTC-day counter."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._day: str = ""
        self._day_count: int = 0
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _prune(self, user_id: str, now: float, window: int) -> deque[float]:
        hits = self._hits.setdefault(user_id, deque())
        cutoff = now - window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    # -- the check ----------------------------------------------------------

    def check(self, user_id: str, now: float | None = None) -> Decision:
        """
        Record a request for `user_id` and say whether it may proceed.

        A rejected request is *not* counted — being throttled must not push the
        retry further away, or a user hammering the bot could lock themselves
        out indefinitely.

        The daily cap is checked first: when the bot is out of budget the
        specific reason matters more than which user asked.
        """
        now = time.monotonic() if now is None else now
        user_max = per_user_limit()
        window = window_seconds()
        cap = daily_cap()

        with self._lock:
            today = self._today()
            if today != self._day:
                self._day = today
                self._day_count = 0

            if cap and self._day_count >= cap:
                return Decision(allowed=False, reason="daily_cap")

            if user_max:
                hits = self._prune(user_id, now, window)
                if len(hits) >= user_max:
                    retry_after = int(window - (now - hits[0])) + 1
                    return Decision(allowed=False, reason="per_user",
                                    retry_after_seconds=max(1, retry_after))
                hits.append(now)

            self._day_count += 1
            return Decision(allowed=True)

    # -- inspection ---------------------------------------------------------

    def usage_today(self) -> tuple[int, int]:
        """(requests today, cap) — cap 0 means uncapped."""
        with self._lock:
            if self._today() != self._day:
                return 0, daily_cap()
            return self._day_count, daily_cap()

    def format_status(self) -> str:
        """One line for `/askbuddy status`."""
        used, cap = self.usage_today()
        user_max = per_user_limit()
        parts = [f"requests today: {used}" + (f"/{cap}" if cap else " (uncapped)")]
        parts.append(
            f"per-user limit: {user_max}/{window_seconds()}s" if user_max
            else "per-user limit: off"
        )
        return " | ".join(parts)

    def reset(self) -> None:
        """Clear all state (tests)."""
        with self._lock:
            self._hits.clear()
            self._day = ""
            self._day_count = 0


# Process-wide limiter.
limiter = RateLimiter()
