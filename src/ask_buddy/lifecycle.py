"""
Background-service health registry and graceful shutdown.

Ask Buddy starts four background services at import time (reminder scheduler,
GitHub triage watcher, daily digest, approval sweep), each wrapped in
``try/except`` so a failure degrades the bot instead of killing it. That is the
right call — but it used to mean a dead triage watcher was invisible: the only
trace was one WARNING line at boot, and ``/askbuddy status`` reported watermarks
without saying whether anything was still polling them.

This module keeps a record of what started, what didn't, and why, so the
running bot can be asked. It also owns shutdown: on SIGTERM/SIGINT, stop the
schedulers, give in-flight worker threads a moment to finish delivering their
answers, and close the DB pool — instead of having the process vanish
mid-request.

Stdlib-only and free of project imports, so it is testable offline and safe to
import from anywhere.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("ask_buddy.lifecycle")

STATUS_RUNNING = "running"
STATUS_DISABLED = "disabled"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"


@dataclass
class ServiceRecord:
    """One background service's outcome at startup."""
    name: str
    status: str
    detail: str = ""
    scheduler: Any = None          # APScheduler instance, when there is one

    @property
    def healthy(self) -> bool:
        """Disabled-by-config counts as healthy: not every deployment wants
        the triage watcher. Only a failure is unhealthy."""
        return self.status in (STATUS_RUNNING, STATUS_DISABLED)


@dataclass
class Registry:
    """Thread-safe record of background services and shutdown hooks."""
    services: dict[str, ServiceRecord] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _shutdown_hooks: list[tuple[str, Callable[[], None]]] = field(
        default_factory=list, repr=False)
    _workers: list[threading.Thread] = field(default_factory=list, repr=False)
    _shutting_down: bool = field(default=False, repr=False)

    # -- registration -------------------------------------------------------

    def record(self, name: str, status: str, detail: str = "",
               scheduler: Any = None) -> ServiceRecord:
        record = ServiceRecord(name=name, status=status, detail=detail,
                               scheduler=scheduler)
        with self._lock:
            self.services[name] = record
        return record

    def register_shutdown_hook(self, name: str, fn: Callable[[], None]) -> None:
        """Hooks run in registration order during shutdown."""
        with self._lock:
            self._shutdown_hooks.append((name, fn))

    def start_service(self, name: str, starter: Callable[[], Any]) -> ServiceRecord:
        """
        Run `starter` and record what happened.

        A starter returning None means "disabled by configuration" — the
        convention git_watch.start_git_watch and git_digest.start_git_digest
        already follow when their env vars are unset. A raised exception is
        recorded as a failure with the message, and never propagated: one dead
        background service must not stop the bot from answering questions.
        """
        try:
            result = starter()
        except Exception as exc:
            log.warning("[lifecycle] %s failed to start: %s", name, exc)
            return self.record(name, STATUS_FAILED, detail=str(exc))

        if result is None:
            log.info("[lifecycle] %s disabled by configuration", name)
            return self.record(name, STATUS_DISABLED,
                               detail="not configured")

        log.info("[lifecycle] %s running", name)
        return self.record(name, STATUS_RUNNING, scheduler=result)

    # -- inspection ---------------------------------------------------------

    def snapshot(self) -> list[ServiceRecord]:
        with self._lock:
            return list(self.services.values())

    def all_healthy(self) -> bool:
        return all(r.healthy for r in self.snapshot())

    def format_status(self) -> str:
        """Slack-ready summary for `/askbuddy status`."""
        records = self.snapshot()
        if not records:
            return "_No background services registered._"

        icons = {
            STATUS_RUNNING: "✅",
            STATUS_DISABLED: "⚪",
            STATUS_FAILED: "❌",
            STATUS_STOPPED: "⏹",
        }
        lines = []
        for record in sorted(records, key=lambda r: r.name):
            icon = icons.get(record.status, "•")
            line = f"{icon} `{record.name}` — {record.status}"
            if record.detail:
                line += f" ({record.detail})"
            lines.append(line)
        return "\n".join(lines)

    # -- shutdown -----------------------------------------------------------

    @property
    def shutting_down(self) -> bool:
        """True once shutdown has begun. Handlers check this so a request
        that arrives mid-drain is refused rather than half-served."""
        return self._shutting_down

    def track_worker(self, thread: threading.Thread) -> None:
        """
        Remember a request-handling thread so shutdown can wait for it.

        Finished threads are pruned on each call, so this stays bounded
        without a reaper.
        """
        with self._lock:
            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(thread)

    def active_workers(self) -> int:
        with self._lock:
            return sum(1 for t in self._workers if t.is_alive())

    def shutdown(self, wait_seconds: float = 10.0) -> None:
        """
        Stop background services, let in-flight requests finish, release
        resources. Safe to call twice — the second call is a no-op.

        Order matters: stop the schedulers first so nothing new is queued,
        then drain the workers that are mid-answer, then run the hooks that
        tear down shared resources those workers were still using.
        """
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            records = list(self.services.values())
            hooks = list(self._shutdown_hooks)
            workers = [t for t in self._workers if t.is_alive()]

        log.info("[lifecycle] shutting down: %d scheduler(s), %d in-flight request(s)",
                 sum(1 for r in records if r.scheduler is not None), len(workers))

        for record in records:
            if record.scheduler is None:
                continue
            try:
                # wait=False: we drain worker threads ourselves below, and a
                # scheduler job blocking on the network shouldn't hold us here.
                record.scheduler.shutdown(wait=False)
                record.status = STATUS_STOPPED
            except Exception as exc:
                log.warning("[lifecycle] could not stop %s: %s", record.name, exc)

        deadline_each = wait_seconds / len(workers) if workers else 0.0
        for thread in workers:
            thread.join(timeout=deadline_each)
            if thread.is_alive():
                log.warning("[lifecycle] request thread %s still running at "
                            "shutdown deadline — abandoning it", thread.name)

        for name, hook in hooks:
            try:
                hook()
                log.info("[lifecycle] ran shutdown hook: %s", name)
            except Exception as exc:
                log.warning("[lifecycle] shutdown hook %s failed: %s", name, exc)

        log.info("[lifecycle] shutdown complete")


# The process-wide registry. One per bot process.
registry = Registry()


def install_signal_handlers(reg: Registry | None = None,
                            wait_seconds: float = 10.0,
                            on_signal: Callable[[], None] | None = None) -> bool:
    """
    Run `reg.shutdown()` on SIGTERM/SIGINT.

    `on_signal` is invoked first, for anything that must be unblocked before
    shutdown can proceed — the Socket Mode handler's blocking ``start()``, in
    practice.

    Returns False when handlers can't be installed (signal.signal only works
    on the main thread, so this is a no-op under some test runners and WSGI
    hosts) rather than raising.
    """
    import signal

    target = reg if reg is not None else registry

    def _handle(signum: int, _frame: Any) -> None:
        log.info("[lifecycle] received signal %s", signum)
        if on_signal is not None:
            try:
                on_signal()
            except Exception as exc:
                log.warning("[lifecycle] on_signal hook failed: %s", exc)
        target.shutdown(wait_seconds=wait_seconds)

    try:
        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
    except (ValueError, OSError) as exc:
        log.warning("[lifecycle] could not install signal handlers: %s", exc)
        return False
    return True
