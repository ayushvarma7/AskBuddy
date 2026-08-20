"""Offline tests for per-user throttling and the daily request ceiling."""

from __future__ import annotations

import pytest

from src.ask_buddy.ratelimit import RateLimiter, daily_cap, per_user_limit, window_seconds


@pytest.fixture
def limiter():
    return RateLimiter()


@pytest.fixture(autouse=True)
def _no_ambient_limits(monkeypatch):
    """Start every test from the shipped defaults (all limits off)."""
    for var in ("ASK_BUDDY_RATE_LIMIT_PER_USER", "ASK_BUDDY_RATE_LIMIT_WINDOW_SEC",
                "ASK_BUDDY_DAILY_REQUEST_CAP"):
        monkeypatch.delenv(var, raising=False)


class TestConfigParsing:
    def test_defaults_are_permissive(self):
        assert per_user_limit() == 0
        assert daily_cap() == 0
        assert window_seconds() == 60

    def test_values_come_from_env(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_PER_USER", "5")
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_WINDOW_SEC", "30")
        monkeypatch.setenv("ASK_BUDDY_DAILY_REQUEST_CAP", "1000")
        assert per_user_limit() == 5
        assert window_seconds() == 30
        assert daily_cap() == 1000

    def test_garbage_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_PER_USER", "lots")
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_WINDOW_SEC", "")
        assert per_user_limit() == 0
        assert window_seconds() == 60

    def test_negative_values_are_clamped_to_off(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_PER_USER", "-3")
        assert per_user_limit() == 0

    def test_zero_window_is_clamped_to_one_second(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_WINDOW_SEC", "0")
        assert window_seconds() == 1


class TestDisabledByDefault:
    def test_unlimited_when_nothing_is_configured(self, limiter):
        for i in range(100):
            assert limiter.check("U1", now=float(i)).allowed


class TestPerUserLimit:
    @pytest.fixture(autouse=True)
    def _limit_two_per_ten(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_PER_USER", "2")
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_WINDOW_SEC", "10")

    def test_allows_up_to_the_limit(self, limiter):
        assert limiter.check("U1", now=0).allowed
        assert limiter.check("U1", now=1).allowed

    def test_blocks_beyond_the_limit(self, limiter):
        limiter.check("U1", now=0)
        limiter.check("U1", now=1)
        decision = limiter.check("U1", now=2)
        assert not decision.allowed
        assert decision.reason == "per_user"

    def test_window_slides(self, limiter):
        limiter.check("U1", now=0)
        limiter.check("U1", now=1)
        assert not limiter.check("U1", now=5).allowed
        # First hit ages out at t=10, freeing a slot.
        assert limiter.check("U1", now=11).allowed

    def test_users_are_independent(self, limiter):
        limiter.check("U1", now=0)
        limiter.check("U1", now=1)
        assert not limiter.check("U1", now=2).allowed
        assert limiter.check("U2", now=2).allowed

    def test_retry_after_is_positive_and_within_the_window(self, limiter):
        limiter.check("U1", now=0)
        limiter.check("U1", now=1)
        decision = limiter.check("U1", now=2)
        assert 1 <= decision.retry_after_seconds <= 10

    def test_a_blocked_request_is_not_counted(self, limiter):
        """Otherwise a user hammering the bot would extend their own lockout
        indefinitely."""
        limiter.check("U1", now=0)
        limiter.check("U1", now=1)
        for t in range(2, 9):
            assert not limiter.check("U1", now=float(t)).allowed
        # The window still clears based on the two real hits, not the retries.
        assert limiter.check("U1", now=11).allowed

    def test_blocked_requests_do_not_consume_daily_budget(self, limiter, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_DAILY_REQUEST_CAP", "100")
        limiter.check("U1", now=0)
        limiter.check("U1", now=1)
        limiter.check("U1", now=2)        # blocked
        used, _ = limiter.usage_today()
        assert used == 2


class TestDailyCap:
    @pytest.fixture(autouse=True)
    def _cap_three(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_DAILY_REQUEST_CAP", "3")

    def test_allows_up_to_the_cap(self, limiter):
        for i in range(3):
            assert limiter.check(f"U{i}", now=float(i)).allowed

    def test_blocks_past_the_cap_regardless_of_user(self, limiter):
        for i in range(3):
            limiter.check(f"U{i}", now=float(i))
        decision = limiter.check("someone-new", now=4)
        assert not decision.allowed
        assert decision.reason == "daily_cap"

    def test_cap_is_checked_before_the_per_user_window(self, limiter, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_PER_USER", "1")
        limiter.check("U1", now=0)
        limiter.check("U2", now=0)
        limiter.check("U3", now=0)
        # U1 is over its own window too, but out-of-budget is the useful reason.
        assert limiter.check("U1", now=1).reason == "daily_cap"

    def test_counter_resets_on_a_new_day(self, limiter):
        for i in range(3):
            limiter.check(f"U{i}", now=float(i))
        assert not limiter.check("U9", now=4).allowed
        limiter._day = "1999-01-01"          # simulate the clock rolling over
        assert limiter.check("U9", now=5).allowed

    def test_usage_today_reports_against_the_cap(self, limiter):
        limiter.check("U1", now=0)
        assert limiter.usage_today() == (1, 3)


class TestSlackMessages:
    def test_allowed_decision_has_no_message(self, limiter):
        assert limiter.check("U1", now=0).slack_message() == ""

    def test_per_user_message_names_the_wait(self, limiter, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_PER_USER", "1")
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_WINDOW_SEC", "30")
        limiter.check("U1", now=0)
        message = limiter.check("U1", now=1).slack_message()
        assert "try again in about" in message.lower()

    def test_daily_cap_message_mentions_the_reset(self, limiter, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_DAILY_REQUEST_CAP", "1")
        limiter.check("U1", now=0)
        message = limiter.check("U2", now=1).slack_message()
        assert "daily request limit" in message
        assert "midnight UTC" in message


class TestStatusLine:
    def test_reports_off_when_unconfigured(self, limiter):
        status = limiter.format_status()
        assert "uncapped" in status
        assert "per-user limit: off" in status

    def test_reports_configured_limits(self, limiter, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_PER_USER", "5")
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_WINDOW_SEC", "60")
        monkeypatch.setenv("ASK_BUDDY_DAILY_REQUEST_CAP", "500")
        limiter.check("U1", now=0)
        status = limiter.format_status()
        assert "requests today: 1/500" in status
        assert "per-user limit: 5/60s" in status


class TestReset:
    def test_reset_clears_everything(self, limiter, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_RATE_LIMIT_PER_USER", "1")
        limiter.check("U1", now=0)
        assert not limiter.check("U1", now=1).allowed
        limiter.reset()
        assert limiter.check("U1", now=2).allowed


class TestThreadSafety:
    def test_concurrent_checks_never_exceed_the_cap(self, monkeypatch):
        """Requests arrive on separate threads; the cap must hold exactly."""
        import threading
        monkeypatch.setenv("ASK_BUDDY_DAILY_REQUEST_CAP", "50")
        limiter = RateLimiter()
        allowed: list[bool] = []
        lock = threading.Lock()

        def hit():
            decision = limiter.check("U1")
            with lock:
                allowed.append(decision.allowed)

        threads = [threading.Thread(target=hit) for _ in range(200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(allowed) == 50
