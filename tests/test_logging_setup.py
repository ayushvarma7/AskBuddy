"""
Offline tests for structured logging and correlation IDs.

logging_setup.py is stdlib-only, so this runs without the project's
dependencies installed.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

from src.ask_buddy.logging_setup import (
    JsonFormatter,
    RequestIdFilter,
    TextFormatter,
    clear_request_id,
    configure_logging,
    get_request_id,
    new_request_id,
    set_request_id,
)


@pytest.fixture(autouse=True)
def _clean_context():
    clear_request_id()
    yield
    clear_request_id()


def _record(msg="hello", name="ask_buddy.test", level=logging.INFO, **extra):
    record = logging.LogRecord(name, level, "f.py", 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestRequestId:
    def test_new_request_id_is_short_hex(self):
        rid = new_request_id()
        assert len(rid) == 8
        assert all(c in "0123456789abcdef" for c in rid)

    def test_ids_are_unique(self):
        assert len({new_request_id() for _ in range(200)}) == 200

    def test_set_and_get(self):
        assert set_request_id("abcd1234") == "abcd1234"
        assert get_request_id() == "abcd1234"

    def test_set_without_argument_generates_one(self):
        rid = set_request_id()
        assert rid and get_request_id() == rid

    def test_empty_outside_a_request(self):
        assert get_request_id() == ""

    def test_threads_do_not_share_ids(self):
        """Each Slack message runs on its own thread; ids must not leak."""
        set_request_id("parent00")
        seen: dict[str, str] = {}

        def worker(tag: str) -> None:
            set_request_id(tag)
            seen[tag] = get_request_id()

        threads = [threading.Thread(target=worker, args=(f"child{i:03d}",))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert seen == {f"child{i:03d}": f"child{i:03d}" for i in range(5)}
        # The parent context is untouched by the children.
        assert get_request_id() == "parent00"


class TestRequestIdFilter:
    def test_attaches_current_id(self):
        set_request_id("deadbeef")
        record = _record()
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "deadbeef"

    def test_attaches_empty_string_outside_a_request(self):
        record = _record()
        RequestIdFilter().filter(record)
        assert record.request_id == ""

    def test_does_not_overwrite_an_explicit_id(self):
        set_request_id("contextid")
        record = _record(request_id="explicit")
        RequestIdFilter().filter(record)
        assert record.request_id == "explicit"


class TestTextFormatter:
    def test_includes_request_id_when_set(self):
        record = _record("looking that up", request_id="a1b2c3d4")
        out = TextFormatter().format(record)
        assert "[a1b2c3d4]" in out
        assert "looking that up" in out

    def test_omits_bracket_when_no_id(self):
        record = _record("startup", request_id="")
        out = TextFormatter().format(record)
        assert "[]" not in out
        assert "startup" in out

    def test_formatter_is_reusable_across_both_shapes(self):
        """The formatter mutates its own style per record — make sure that
        doesn't leak between calls."""
        formatter = TextFormatter()
        with_id = formatter.format(_record("a", request_id="11112222"))
        without_id = formatter.format(_record("b", request_id=""))
        again_with_id = formatter.format(_record("c", request_id="33334444"))
        assert "[11112222]" in with_id
        assert "[]" not in without_id
        assert "[33334444]" in again_with_id


class TestJsonFormatter:
    def test_emits_one_json_object(self):
        record = _record("posted answer", request_id="a1b2c3d4")
        payload = json.loads(JsonFormatter().format(record))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "ask_buddy.test"
        assert payload["message"] == "posted answer"
        assert payload["request_id"] == "a1b2c3d4"
        assert "ts" in payload

    def test_omits_request_id_when_absent(self):
        payload = json.loads(JsonFormatter().format(_record(request_id="")))
        assert "request_id" not in payload

    def test_interpolates_message_args(self):
        record = logging.LogRecord("n", logging.INFO, "f.py", 1,
                                   "retrieved %d chunks", (5,), None)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "retrieved 5 chunks"

    def test_extra_fields_are_preserved(self):
        record = _record("routed", request_id="x", repo="acme/backend", pr=17)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["repo"] == "acme/backend"
        assert payload["pr"] == 17

    def test_exception_is_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = logging.LogRecord("n", logging.ERROR, "f.py", 1,
                                       "failed", None, sys.exc_info())
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in payload["exception"]

    def test_unserialisable_values_do_not_crash(self):
        payload = json.loads(JsonFormatter().format(_record(obj=object())))
        assert isinstance(payload["obj"], str)


class TestConfigureLogging:
    def test_replaces_handlers_so_lines_are_not_duplicated(self):
        root = logging.getLogger()
        logging.basicConfig(level=logging.INFO)      # simulate an earlier setup
        configure_logging()
        configure_logging()                          # idempotent
        assert len(root.handlers) == 1

    def test_json_format_selected_by_env(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_LOG_FORMAT", "json")
        configure_logging()
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)

    def test_text_is_the_default(self, monkeypatch):
        monkeypatch.delenv("ASK_BUDDY_LOG_FORMAT", raising=False)
        configure_logging()
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, TextFormatter)

    def test_level_from_env(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_LOG_LEVEL", "warning")
        configure_logging()
        assert logging.getLogger().level == logging.WARNING

    def test_unknown_level_falls_back_to_info(self, monkeypatch):
        monkeypatch.setenv("ASK_BUDDY_LOG_LEVEL", "not-a-level")
        configure_logging()
        assert logging.getLogger().level == logging.INFO

    def test_handler_carries_the_filter(self):
        configure_logging()
        handler = logging.getLogger().handlers[0]
        assert any(isinstance(f, RequestIdFilter) for f in handler.filters)
