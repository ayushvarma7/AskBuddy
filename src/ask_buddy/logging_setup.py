"""
Structured logging with a per-request correlation ID.

Ask Buddy handles each Slack message on its own thread, so log lines from
concurrent requests interleave. Reading a single question end-to-end meant
eyeballing `[supervisor]`, `[github]`, `[citations]` and `[feedback]` lines and
guessing which belonged together.

Every record now carries a short `request_id`, set once when a message arrives
and propagated automatically to everything that runs under it — including
`asyncio` tasks, since ``contextvars`` are copied into them.

Two output formats:

  text (default)  2026-08-19 10:04:11 [INFO] ask_buddy.slack [a1b2c3d4] message
  json            {"ts": "...", "level": "INFO", "request_id": "a1b2c3d4", ...}

Choose with ``ASK_BUDDY_LOG_FORMAT=json`` and set the level with
``ASK_BUDDY_LOG_LEVEL`` (default INFO). JSON is what you want anywhere logs are
shipped to a collector; text stays readable in a terminal.

This module is stdlib-only so it can be imported and tested without the
project's dependencies installed.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import uuid

# Empty string rather than None so format strings never render "None".
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ask_buddy_request_id", default=""
)

# Attributes present on every LogRecord; anything else a caller attached with
# `extra=` is emitted as a JSON field so structured context isn't lost.
_STANDARD_RECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    "request_id",
})


def new_request_id() -> str:
    """Short random id — long enough to be unique per in-flight request,
    short enough to scan by eye in a terminal."""
    return uuid.uuid4().hex[:8]


def set_request_id(request_id: str | None = None) -> str:
    """
    Bind a correlation id to the current context, returning it.

    Call once per inbound Slack event, on the thread that handles it. Threads
    start with a fresh copy of the context, so per-request ids never leak
    between concurrent messages.
    """
    value = request_id or new_request_id()
    _request_id.set(value)
    return value


def get_request_id() -> str:
    """The current correlation id, or '' outside a request."""
    return _request_id.get()


def clear_request_id() -> None:
    """Unbind the correlation id (useful in tests)."""
    _request_id.set("")


class RequestIdFilter(logging.Filter):
    """Attaches the context's request_id to every record.

    Implemented as a filter rather than a formatter concern so both formatters
    — and any handler added later — see the attribute.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id.get()
        return True


class TextFormatter(logging.Formatter):
    """Human format, with the request id in brackets when one is set."""

    default_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    request_fmt = "%(asctime)s [%(levelname)s] %(name)s [%(request_id)s]: %(message)s"

    def format(self, record: logging.LogRecord) -> str:
        has_id = bool(getattr(record, "request_id", ""))
        self._style._fmt = self.request_fmt if has_id else self.default_fmt
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", "")
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Preserve anything passed via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


def _formatter_for(fmt: str) -> logging.Formatter:
    return JsonFormatter() if fmt.lower() == "json" else TextFormatter()


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """
    Install the correlation-id filter and formatter on the root logger.

    Replaces the handlers already on root rather than adding to them, so
    calling this after logging.basicConfig() (or twice) does not produce
    duplicate lines.
    """
    level_name = (level or os.environ.get("ASK_BUDDY_LOG_LEVEL", "INFO")).upper()
    fmt_name = fmt or os.environ.get("ASK_BUDDY_LOG_FORMAT", "text")

    handler = logging.StreamHandler()
    handler.setFormatter(_formatter_for(fmt_name))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))
