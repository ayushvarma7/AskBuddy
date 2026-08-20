"""
Offline tests for the service registry and graceful shutdown.

lifecycle.py is stdlib-only, so this runs without the project's dependencies.
"""

from __future__ import annotations

import threading

from src.ask_buddy.lifecycle import (
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    Registry,
    install_signal_handlers,
)


class FakeScheduler:
    """Stands in for an APScheduler instance."""

    def __init__(self, fail_on_shutdown: bool = False):
        self.shutdown_calls: list[bool] = []
        self.fail_on_shutdown = fail_on_shutdown

    def shutdown(self, wait: bool = True) -> None:
        if self.fail_on_shutdown:
            raise RuntimeError("scheduler wedged")
        self.shutdown_calls.append(wait)


class TestStartService:
    def test_running_service_is_recorded_with_its_scheduler(self):
        reg = Registry()
        sched = FakeScheduler()
        record = reg.start_service("reminders", lambda: sched)
        assert record.status == STATUS_RUNNING
        assert record.scheduler is sched
        assert record.healthy

    def test_starter_returning_none_means_disabled_not_broken(self):
        """git_watch/git_digest return None when their env vars are unset —
        that is a deployment choice, not a fault."""
        reg = Registry()
        record = reg.start_service("git_watch", lambda: None)
        assert record.status == STATUS_DISABLED
        assert record.healthy

    def test_raising_starter_is_recorded_as_failed_not_propagated(self):
        reg = Registry()

        def boom():
            raise RuntimeError("no database")

        record = reg.start_service("digest", boom)     # must not raise
        assert record.status == STATUS_FAILED
        assert "no database" in record.detail
        assert not record.healthy

    def test_all_healthy_reflects_a_failure(self):
        reg = Registry()
        reg.start_service("ok", lambda: FakeScheduler())
        assert reg.all_healthy()
        reg.start_service("broken", lambda: (_ for _ in ()).throw(ValueError("x")))
        assert not reg.all_healthy()

    def test_restarting_a_service_replaces_its_record(self):
        reg = Registry()
        reg.start_service("svc", lambda: (_ for _ in ()).throw(ValueError("first")))
        reg.start_service("svc", lambda: FakeScheduler())
        assert len(reg.snapshot()) == 1
        assert reg.snapshot()[0].status == STATUS_RUNNING


class TestFormatStatus:
    def test_empty_registry(self):
        assert "No background services" in Registry().format_status()

    def test_shows_an_icon_and_status_per_service(self):
        reg = Registry()
        reg.start_service("reminders", lambda: FakeScheduler())
        reg.start_service("git_watch", lambda: None)
        reg.start_service("digest", lambda: (_ for _ in ()).throw(ValueError("bad cron")))
        out = reg.format_status()
        assert "✅ `reminders` — running" in out
        assert "⚪ `git_watch` — disabled" in out
        assert "❌ `digest` — failed" in out
        assert "bad cron" in out

    def test_services_are_listed_in_a_stable_order(self):
        reg = Registry()
        for name in ("zebra", "alpha", "middle"):
            reg.start_service(name, lambda: FakeScheduler())
        lines = reg.format_status().splitlines()
        assert [line.split("`")[1] for line in lines] == ["alpha", "middle", "zebra"]


class TestWorkerTracking:
    def test_finished_threads_are_pruned(self):
        reg = Registry()
        for _ in range(5):
            t = threading.Thread(target=lambda: None)
            t.start()
            t.join()
            reg.track_worker(t)
        # Every tracked thread has finished, so at most the last one is retained.
        assert reg.active_workers() == 0
        assert len(reg._workers) <= 1

    def test_active_workers_counts_live_threads(self):
        reg = Registry()
        release = threading.Event()
        t = threading.Thread(target=release.wait, daemon=True)
        t.start()
        reg.track_worker(t)
        assert reg.active_workers() == 1
        release.set()
        t.join()
        assert reg.active_workers() == 0


class TestShutdown:
    def test_stops_schedulers_and_marks_them_stopped(self):
        reg = Registry()
        sched = FakeScheduler()
        reg.start_service("reminders", lambda: sched)
        reg.shutdown(wait_seconds=0.1)
        assert sched.shutdown_calls == [False]
        assert reg.snapshot()[0].status == STATUS_STOPPED

    def test_disabled_service_needs_no_stopping(self):
        reg = Registry()
        reg.start_service("git_watch", lambda: None)
        reg.shutdown(wait_seconds=0.1)          # must not raise
        assert reg.snapshot()[0].status == STATUS_DISABLED

    def test_a_wedged_scheduler_does_not_stop_the_rest(self):
        reg = Registry()
        bad = FakeScheduler(fail_on_shutdown=True)
        good = FakeScheduler()
        reg.start_service("bad", lambda: bad)
        reg.start_service("good", lambda: good)
        reg.shutdown(wait_seconds=0.1)
        assert good.shutdown_calls == [False]

    def test_hooks_run_in_registration_order(self):
        reg = Registry()
        order: list[str] = []
        reg.register_shutdown_hook("first", lambda: order.append("first"))
        reg.register_shutdown_hook("second", lambda: order.append("second"))
        reg.shutdown(wait_seconds=0.1)
        assert order == ["first", "second"]

    def test_a_failing_hook_does_not_block_the_others(self):
        reg = Registry()
        ran: list[str] = []

        def boom():
            raise RuntimeError("pool already closed")

        reg.register_shutdown_hook("boom", boom)
        reg.register_shutdown_hook("close_pool", lambda: ran.append("closed"))
        reg.shutdown(wait_seconds=0.1)
        assert ran == ["closed"]

    def test_in_flight_worker_is_awaited(self):
        reg = Registry()
        finished: list[str] = []
        release = threading.Event()

        def work():
            release.wait(timeout=2)
            finished.append("done")

        t = threading.Thread(target=work, daemon=True)
        t.start()
        reg.track_worker(t)
        release.set()
        reg.shutdown(wait_seconds=2)
        assert finished == ["done"]

    def test_a_hung_worker_is_abandoned_at_the_deadline(self):
        reg = Registry()
        never = threading.Event()
        t = threading.Thread(target=never.wait, daemon=True)
        t.start()
        reg.track_worker(t)
        reg.shutdown(wait_seconds=0.05)   # returns rather than hanging forever
        assert t.is_alive()
        never.set()

    def test_shutdown_is_idempotent(self):
        reg = Registry()
        sched = FakeScheduler()
        reg.start_service("reminders", lambda: sched)
        calls: list[str] = []
        reg.register_shutdown_hook("h", lambda: calls.append("x"))
        reg.shutdown(wait_seconds=0.1)
        reg.shutdown(wait_seconds=0.1)
        assert sched.shutdown_calls == [False]
        assert calls == ["x"]

    def test_shutting_down_flag(self):
        reg = Registry()
        assert not reg.shutting_down
        reg.shutdown(wait_seconds=0.1)
        assert reg.shutting_down

    def test_schedulers_stop_before_hooks_release_resources(self):
        """A hook closing the DB pool must not run while a scheduler job could
        still be using it."""
        reg = Registry()
        order: list[str] = []

        class OrderedScheduler:
            def shutdown(self, wait: bool = True) -> None:
                order.append("scheduler")

        reg.start_service("s", lambda: OrderedScheduler())
        reg.register_shutdown_hook("pool", lambda: order.append("pool"))
        reg.shutdown(wait_seconds=0.1)
        assert order == ["scheduler", "pool"]


class TestSignalHandlers:
    def test_installs_on_the_main_thread(self):
        reg = Registry()
        assert install_signal_handlers(reg, wait_seconds=0.1) is True

    def test_reports_failure_off_the_main_thread_instead_of_raising(self):
        """signal.signal() only works on the main thread; some test runners
        and WSGI hosts import from elsewhere."""
        reg = Registry()
        result: list[bool] = []

        def attempt():
            result.append(install_signal_handlers(reg, wait_seconds=0.1))

        t = threading.Thread(target=attempt)
        t.start()
        t.join()
        assert result == [False]
