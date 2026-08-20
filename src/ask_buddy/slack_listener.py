"""
Slack Bolt Socket Mode listener for Ask Buddy.

Run with:
    python -m src.ask_buddy.slack_listener

Required environment variables (set in .env):
    SLACK_BOT_TOKEN   — xoxb-...  (Bot User OAuth Token)
    SLACK_APP_TOKEN   — xapp-...  (App-Level Token, connections:write scope)
    GOOGLE_API_KEY    — AIza...
    ASK_BUDDY_DB_DSN  — postgresql://user:pass@host:port/db
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .lifecycle import install_signal_handlers, registry as service_registry
from .logging_setup import configure_logging, new_request_id, set_request_id
from .ratelimit import limiter

load_dotenv()

# Every record gets a request_id so one Slack message reads as one trace.
# ASK_BUDDY_LOG_FORMAT=json switches to line-delimited JSON for collectors.
configure_logging()
log = logging.getLogger("ask_buddy.slack")

# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"ERROR: Required environment variable '{name}' is not set. "
            "Please add it to your .env file and restart.",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


SLACK_BOT_TOKEN = _require_env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = _require_env("SLACK_APP_TOKEN")

# Validate these are set before importing the agent
_require_env("GOOGLE_API_KEY")
_require_env("ASK_BUDDY_DB_DSN")

# ---------------------------------------------------------------------------
# Slack app setup
# ---------------------------------------------------------------------------

# slack_bolt verifies the bot token by calling auth.test during construction.
# That is a useful startup diagnostic in a real deployment, but it makes this
# module unimportable with placeholder credentials — which is exactly what CI
# and local offline test runs use. ASK_BUDDY_SKIP_SLACK_VERIFY=1 turns it off.
_SKIP_SLACK_VERIFY = os.environ.get(
    "ASK_BUDDY_SKIP_SLACK_VERIFY", "").strip().lower() in ("1", "true", "yes")

app = App(token=SLACK_BOT_TOKEN,
          token_verification_enabled=not _SKIP_SLACK_VERIFY)

# Ensure the feedback table exists at startup (idempotent)
try:
    from .db import init_feedback_schema
    init_feedback_schema()
    log.info("ask_buddy_feedback table ready.")
except Exception as _e:
    log.warning("Could not initialise feedback schema: %s", _e)

# Ensure the reminders table exists at startup (idempotent)
try:
    from .db import init_reminders_schema
    init_reminders_schema()
    log.info("ask_buddy_reminders table ready.")
except Exception as _e:
    log.warning("Could not initialise reminders schema: %s", _e)

# Ensure the git-watch table exists at startup (idempotent)
try:
    from .db import init_git_watch_schema
    init_git_watch_schema()
    log.info("ask_buddy_git_watch table ready.")
except Exception as _e:
    log.warning("Could not initialise git-watch schema: %s", _e)

# Ensure the git-identities table exists at startup (idempotent)
try:
    from .db import init_git_identities_schema
    init_git_identities_schema()
    log.info("ask_buddy_git_identities table ready.")
except Exception as _e:
    log.warning("Could not initialise git-identities schema: %s", _e)

# Ensure the pending-approvals audit table exists at startup (idempotent)
try:
    from .db import init_pending_approvals_schema
    init_pending_approvals_schema()
    log.info("ask_buddy_pending_approvals table ready.")
except Exception as _e:
    log.warning("Could not initialise pending-approvals schema: %s", _e)


# ---------------------------------------------------------------------------
# Channel name -> ID resolution (needed by the scheduler_agent's reminder
# tools, since users name a channel like 'svl-interns-2026', not its ID)
# ---------------------------------------------------------------------------

_CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{8,}$")
_channel_name_to_id: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Pending GitHub write-action approvals
#
# A ToolApproval-gated tool (merge_pull_request, set_issue_state) pauses the
# CugaSupervisor's graph instead of running. The paused supervisor uses an
# in-memory checkpointer (see cuga/sdk.py CugaSupervisor.graph) — resuming
# MUST reuse the exact same supervisor object, so we keep it alive here,
# keyed by thread_id, until the user clicks Confirm/Cancel in Slack.
# ---------------------------------------------------------------------------

PAUSED_MARKER = "Execution paused for approval"
_APPROVAL_TTL_SECONDS = 30 * 60  # 30 minutes

_pending_approvals: dict[str, dict] = {}
_pending_approvals_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Supervisor cache — one CugaSupervisor per (channel, user), kept alive so
# the in-memory MemorySaver checkpointer retains conversation history across
# turns.  Entries are evicted after the same TTL as pending approvals to
# prevent unbounded growth on idle bots.
# ---------------------------------------------------------------------------

_supervisor_cache: dict[str, dict] = {}  # thread_id -> {supervisor, last_used}
_supervisor_cache_lock = threading.Lock()
_SUPERVISOR_TTL_SECONDS = _APPROVAL_TTL_SECONDS  # 30 minutes idle eviction


def _get_or_build_supervisor(thread_id: str,
                              slack_post_fn: Callable[[str, str], None],
                              resolve_channel_fn: Callable[[str], tuple[str, str]],
                              created_by: str | None) -> Any:
    """Return the cached CugaSupervisor for this thread_id, building one if needed."""
    from .agent import build_supervisor
    now = time.time()
    with _supervisor_cache_lock:
        entry = _supervisor_cache.get(thread_id)
        if entry:
            entry["last_used"] = now
            return entry["supervisor"]
    # Build outside the lock — supervisor construction is slow and we don't
    # want to block other threads while it runs.
    supervisor = build_supervisor(
        slack_post_fn=slack_post_fn,
        resolve_channel_fn=resolve_channel_fn,
        created_by=created_by,
    )
    with _supervisor_cache_lock:
        # Another thread may have built one while we were constructing — let
        # that one win so we don't accumulate duplicates.
        existing = _supervisor_cache.get(thread_id)
        if existing:
            existing["last_used"] = now
            return existing["supervisor"]
        _supervisor_cache[thread_id] = {"supervisor": supervisor, "last_used": now}
    return supervisor


def _evict_idle_supervisors() -> None:
    """Remove supervisors that haven't been used for _SUPERVISOR_TTL_SECONDS."""
    now = time.time()
    with _supervisor_cache_lock:
        evicted = [tid for tid, v in _supervisor_cache.items()
                   if now - v["last_used"] > _SUPERVISOR_TTL_SECONDS]
        for tid in evicted:
            del _supervisor_cache[tid]
    if evicted:
        log.info("[supervisor_cache] evicted %d idle supervisor(s)", len(evicted))


def _stash_pending_approval(thread_id: str, supervisor: Any, channel: str,
                            user_text: str, agent_config: str,
                            retrieved_chunk_ids: list[int],
                            thread_ts: str | None = None) -> bool:
    """
    Park a paused supervisor until the user confirms or cancels.

    Returns False when this thread already has an unresolved approval. Two
    concurrent messages from the same user in the same channel share a
    thread_id, so overwriting would strand the buttons already showing in
    Slack — clicking them would find nothing to resume. Keeping the first
    pending action and telling the user is the safe choice.
    """
    with _pending_approvals_lock:
        if thread_id in _pending_approvals:
            return False
        _pending_approvals[thread_id] = {
            "supervisor": supervisor,
            "channel": channel,
            "user_text": user_text,
            "agent_config": agent_config,
            "retrieved_chunk_ids": list(retrieved_chunk_ids),
            "thread_ts": thread_ts,
            "created_at": time.time(),
        }
        return True


def _pop_pending_approval(thread_id: str) -> dict | None:
    with _pending_approvals_lock:
        return _pending_approvals.pop(thread_id, None)


def _sweep_expired_approvals() -> None:
    now = time.time()
    with _pending_approvals_lock:
        expired = [tid for tid, v in _pending_approvals.items()
                   if now - v["created_at"] > _APPROVAL_TTL_SECONDS]
        for tid in expired:
            del _pending_approvals[tid]
    if expired:
        log.info("[git_approval] swept %d expired pending approval(s)", len(expired))
        try:
            from .db import resolve_pending_approval
            for tid in expired:
                resolve_pending_approval(tid, outcome="expired")
        except Exception:
            log.warning("[git_approval] could not mark swept approvals expired:\n%s",
                        traceback.format_exc())


def _start_approval_sweep() -> Any:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    sweep_scheduler = BackgroundScheduler()
    sweep_scheduler.add_job(
        _sweep_expired_approvals,
        trigger=IntervalTrigger(minutes=5),
        id="git-approval-sweep",
        replace_existing=True,
    )
    sweep_scheduler.add_job(
        _evict_idle_supervisors,
        trigger=IntervalTrigger(minutes=5),
        id="supervisor-cache-evict",
        replace_existing=True,
    )
    sweep_scheduler.start()
    log.info("[git_approval] pending-approval sweep + supervisor-cache eviction started "
             "(TTL=%ds)", _APPROVAL_TTL_SECONDS)
    return sweep_scheduler


def _build_approval_blocks(thread_id: str, prompt_text: str,
                           action_summary: str = "") -> list[dict]:
    payload = json.dumps({"thread_id": thread_id})
    expires_at = datetime.fromtimestamp(
        time.time() + _APPROVAL_TTL_SECONDS, tz=timezone.utc
    ).strftime("%H:%M UTC")
    footer = f"_Expires at {expires_at}_"
    if action_summary:
        footer = f"*Action:* {action_summary}\n{footer}"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": prompt_text}},
    ]
    if footer:
        blocks.append({"type": "context",
                        "elements": [{"type": "mrkdwn", "text": footer}]})
    blocks.append({
        "type": "actions",
        "block_id": f"git_approval_{thread_id}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Confirm", "emoji": True},
                "style": "primary",
                "action_id": "git_approval_confirm",
                "value": payload,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ Cancel", "emoji": True},
                "style": "danger",
                "action_id": "git_approval_deny",
                "value": payload,
            },
        ],
    })
    return blocks


def _refresh_channel_cache() -> None:
    global _channel_name_to_id
    cache: dict[str, str] = {}
    cursor = None
    while True:
        resp = app.client.conversations_list(
            types="public_channel,private_channel", limit=200, cursor=cursor
        )
        channels: list[dict] = resp.get("channels", []) or []
        for ch in channels:
            cache[ch["name"]] = ch["id"]
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    _channel_name_to_id = cache


def resolve_channel(channel: str) -> tuple[str, str]:
    """
    Resolve a Slack channel name (with or without '#') or ID to
    (channel_id, channel_name). Requires the channels:read (and, for
    private channels, groups:read) Bot Token Scope.
    """
    raw = channel.strip()
    name = raw.lstrip("#")
    if _CHANNEL_ID_RE.match(name):
        return name, name
    if name not in _channel_name_to_id:
        _refresh_channel_cache()
    channel_id = _channel_name_to_id.get(name)
    if channel_id is None:
        raise ValueError(f"Could not find a Slack channel named '{name}'.")
    return channel_id, name


# ---------------------------------------------------------------------------
# Reminder scheduler startup — recurring broadcast reminders
# ---------------------------------------------------------------------------

def _plain_post(channel: str, text: str) -> None:
    """Post without feedback buttons — used for reminder broadcasts."""
    app.client.chat_postMessage(channel=channel, text=text)


# Each background service is started through the registry, which records
# whether it is running, disabled by config, or broken — and keeps the handle
# so shutdown can stop it. `/askbuddy status` reports the result, so a dead
# triage watcher is visible instead of being one WARNING line at boot.

def _start_reminders() -> Any:
    from .scheduler import start_scheduler
    return start_scheduler(_plain_post)


def _start_triage_watcher() -> Any:
    from .git_watch import start_git_watch
    return start_git_watch(_plain_post)   # reuse the no-feedback-buttons poster


def _start_daily_digest() -> Any:
    from .git_digest import start_git_digest
    return start_git_digest(_plain_post)


service_registry.start_service("reminder_scheduler", _start_reminders)
service_registry.start_service("git_triage_watcher", _start_triage_watcher)
service_registry.start_service("git_daily_digest", _start_daily_digest)
service_registry.start_service("approval_sweep", _start_approval_sweep)

# Release Postgres connections on the way out, after the schedulers and any
# in-flight request threads have stopped using them.
try:
    from .db import close_pool
    service_registry.register_shutdown_hook("db_pool", close_pool)
except Exception as _e:      # pragma: no cover - import guard only
    log.warning("Could not register db pool shutdown hook: %s", _e)


# ---------------------------------------------------------------------------
# Core: run agent in a background thread to avoid blocking Bolt's event loop
# ---------------------------------------------------------------------------

def _thread_kwargs(thread_ts: str | None) -> dict:
    """Keyword args that keep a reply in its thread. Empty for DMs, where
    there is no thread to join."""
    return {"thread_ts": thread_ts} if thread_ts else {}


def _is_direct_message(event: dict) -> bool:
    """
    True only for DMs.

    The 'message' event fires for channel messages too as soon as the app
    subscribes to message.channels or is invited to a channel with
    channels:history. Without this guard the bot would answer every message in
    those channels, and answer @mentions twice. Relying on the Slack event
    subscription alone leaves that one config change away from happening.
    """
    return event.get("channel_type") == "im"


def _void_approvals_from_previous_run() -> None:
    """
    Tell anyone whose confirmation was still pending when the bot stopped.

    The paused supervisor graph lives in an in-memory checkpointer, so a
    restart genuinely cannot resume it — there is no handle left to resume.
    What we can guarantee is that nobody is left staring at buttons: each
    affected conversation gets an explicit "nothing ran, ask again" message,
    and the row is closed out as `orphaned` for the audit trail.
    """
    try:
        from .db import orphan_unresolved_approvals
        orphaned = orphan_unresolved_approvals()
    except Exception:
        log.warning("[git_approval] could not check for orphaned approvals:\n%s",
                    traceback.format_exc())
        return

    for row in orphaned:
        log.warning("[git_approval] voiding approval from a previous run | "
                    "thread=%s request=%r", row["thread_id"], row["user_text"][:80])
        try:
            app.client.chat_postMessage(
                channel=row["channel"],
                text=("⚠️ Ask Buddy restarted while waiting for your confirmation, "
                      "so this request was cancelled:\n"
                      f"> {row['user_text'][:200]}\n"
                      "*Nothing was changed on GitHub.* Please ask again if you "
                      "still want it."),
                **_thread_kwargs(row.get("thread_ts")),
            )
        except Exception:
            log.warning("[git_approval] could not notify %s about the voided "
                        "approval:\n%s", row["channel"], traceback.format_exc())


try:
    _void_approvals_from_previous_run()
except Exception as _e:      # pragma: no cover - startup guard
    log.warning("Could not process orphaned approvals: %s", _e)


# ---------------------------------------------------------------------------
# Citation validation — verify the guardrail the prompts only ask for
# ---------------------------------------------------------------------------

def _validate_answer_citations(answer_text: str, is_refusal: bool,
                                question: str) -> str | None:
    """
    Check the answer's Source(s) block against real corpus metadata.

    Returns the verdict string to store, or None if validation itself could
    not run (no DB, etc.) — an unavailable check records nothing rather than
    recording a false clean bill of health.
    """
    from .citations import validate_citations

    try:
        from .db import get_known_sources
        known = get_known_sources()
    except Exception:
        log.warning("[citations] could not load known sources, skipping check:\n%s",
                    traceback.format_exc())
        return None

    verdict = validate_citations(answer_text, known, is_refusal=is_refusal)

    if not verdict.ok:
        # The known-sources set is cached for a few minutes, and ingest runs in
        # a separate process so it cannot invalidate that cache. A document
        # added moments ago would otherwise be reported as fabricated. Re-check
        # once against fresh metadata before making that accusation — this only
        # costs a query on the failure path.
        try:
            from .db import get_known_sources as _fresh
            verdict = validate_citations(answer_text, _fresh(force_refresh=True),
                                         is_refusal=is_refusal)
        except Exception:
            log.warning("[citations] could not re-check against fresh sources:\n%s",
                        traceback.format_exc())

    if verdict.ok:
        log.info("[citations] %s | q=%r", verdict.summary(), question[:80])
    else:
        # Loud: this is the anti-hallucination guarantee failing, and the
        # answer has already been composed — someone needs to see it.
        log.warning("[citations] FAILED: %s | q=%r", verdict.summary(), question[:120])
    return verdict.status


# ---------------------------------------------------------------------------
# Core: post answer with Block Kit feedback buttons
# ---------------------------------------------------------------------------

def _post_answer_with_feedback(
    channel: str,
    answer_text: str,
    question: str,
    retrieved_chunk_ids: list[int] | None = None,
    agent_config: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """
    Post the answer as a Block Kit message with 👍 / 👎 buttons,
    and pre-insert a pending row in ask_buddy_feedback.

    Chunk IDs are attributed only to answers that actually cite the corpus
    (see citations.cites_documents). Refusals and non-RAG replies — a GitHub
    answer, a reminder confirmation — store none, so a 👎 on one of those
    can't drag down unrelated policy chunks in the retrieval re-ranker.

    Every answer's citations are also validated against real corpus metadata
    before posting. The verdict is logged and stored, never used to block the
    message — a wrong citation is worth knowing about, but silently dropping
    an answer the user is waiting on would be worse.
    """
    from .citations import cites_documents
    from .feedback import (
        new_response_id, build_answer_blocks,
        store_feedback_row, extract_sources, is_refusal_text,
    )
    response_id = new_response_id()
    sources = extract_sources(answer_text)
    refusal = is_refusal_text(answer_text)
    chunk_ids = retrieved_chunk_ids if cites_documents(answer_text, refusal) else None
    blocks = build_answer_blocks(answer_text, question, response_id,
                                 is_refusal=refusal)
    citation_status = _validate_answer_citations(answer_text, refusal, question)

    try:
        store_feedback_row(
            response_id, question, answer_text, sources,
            retrieved_chunk_ids=chunk_ids,
            is_refusal=refusal,
            agent_config=agent_config,
            citation_status=citation_status,
        )
    except Exception:
        log.warning("[feedback] could not store pending row:\n%s",
                    traceback.format_exc())

    app.client.chat_postMessage(
        channel=channel,
        text=answer_text,          # fallback for notifications
        blocks=blocks,
        **_thread_kwargs(thread_ts),
    )
    log.info("[feedback] posted response_id=%s refusal=%s chunks=%s",
             response_id, refusal, chunk_ids)


# ---------------------------------------------------------------------------
# Core: run agent in a background thread to avoid blocking Bolt's event loop
# ---------------------------------------------------------------------------

def _run_agent_for_message(user_text: str, channel: str, user: str,
                            thread_ts: str | None = None,
                            request_id: str | None = None) -> None:
    """
    Run the CugaSupervisor in a background thread.

    Supervisors are cached per (channel, user) so the in-memory MemorySaver
    checkpointer preserves conversation history across turns. That cache key
    is also the invoke thread_id, which means two messages from the same user
    in the same channel share one checkpoint slot — they are serialised by the
    checkpointer rather than isolated. A second gated action arriving while an
    approval is still pending is refused outright (see
    _stash_pending_approval) instead of silently replacing the first.
    """
    # threading.Thread starts with an empty context, so the id generated by
    # the Slack handler has to be re-bound here to reach these log lines.
    set_request_id(request_id)

    posted_answers: list[str] = []
    retrieved_chunk_ids: list[int] = []

    from .agent import current_agent_config
    agent_config = current_agent_config()

    def _post(ch: str, text: str) -> None:
        _post_answer_with_feedback(
            ch, text, user_text,
            retrieved_chunk_ids=list(retrieved_chunk_ids),
            agent_config=agent_config,
            thread_ts=thread_ts,
        )
        posted_answers.append(text)

    # Stable thread_id per (channel, user) — the supervisor cache key and the
    # key used for the approval stash after a pause.
    thread_id = f"slack-{channel}-{user}"

    prompt = (
        f"A Slack user (id: {user}) in channel {channel} asks:\n\n"
        f"{user_text}\n\n"
        f"Route this to the right agent, then call "
        f"post_slack_message with channel='{channel}' to deliver your response."
    )

    log.info("[supervisor] starting | user=%s channel=%s query=%r",
             user, channel, user_text[:120])

    # Best-effort, deliberately outside the main try block. These IDs only
    # feed the chunk-quality signal, so a retrieval failure must not sink a
    # request that needs no retrieval at all — a GitHub question or a reminder
    # would otherwise die because the embedding API was down.
    try:
        from .retrieve import _hybrid_retrieve_core
        chunks = _hybrid_retrieve_core(user_text, top_k=5)
        if chunks and "error" in chunks[0]:
            log.warning("[supervisor] pre-retrieval failed (%s) — continuing "
                        "without chunk attribution", chunks[0]["error"])
        else:
            retrieved_chunk_ids[:] = [c["id"] for c in chunks if "id" in c]
            log.info("[supervisor] pre-retrieved %d chunks: %s",
                     len(chunks), [c.get("source_filename") for c in chunks])
    except Exception:
        log.warning("[supervisor] pre-retrieval raised — continuing without "
                    "chunk attribution:\n%s", traceback.format_exc())

    try:
        supervisor = _get_or_build_supervisor(
            thread_id,
            slack_post_fn=_post,
            resolve_channel_fn=resolve_channel,
            created_by=user,
        )
        result = asyncio.run(
            supervisor.invoke(prompt, thread_id=thread_id)
        )
        log.info("[supervisor] invoke complete | answer[:120]=%r",
                 (getattr(result, "answer", None) or "")[:120])

        answer = getattr(result, "answer", None)

        if answer and PAUSED_MARKER in answer:
            stashed = _stash_pending_approval(
                thread_id, supervisor, channel, user_text,
                agent_config, retrieved_chunk_ids, thread_ts=thread_ts,
            )
            if stashed:
                try:
                    from .db import insert_pending_approval
                    insert_pending_approval(
                        thread_id=thread_id, channel=channel,
                        user_text=user_text, user_id=user,
                        thread_ts=thread_ts, agent_config=agent_config,
                    )
                except Exception:
                    # The audit row is best-effort: losing it must not stop the
                    # user from being able to confirm the action.
                    log.warning("[git_approval] could not record audit row:\n%s",
                                traceback.format_exc())
            if not stashed:
                log.warning("[git_approval] approval already pending for %s — "
                            "refusing to replace it", thread_id)
                app.client.chat_postMessage(
                    channel=channel,
                    text=("⚠️ You already have an action waiting for confirmation. "
                          "Please confirm or cancel that one first, then ask again."),
                    **_thread_kwargs(thread_ts),
                )
                return
            blocks = _build_approval_blocks(
                thread_id,
                "⚠️ *This action needs your confirmation before it runs on "
                "GitHub* (e.g. merging a pull request or closing an issue).",
                action_summary=user_text[:120],
            )
            app.client.chat_postMessage(
                channel=channel,
                text="Confirmation needed before this action runs.",
                blocks=blocks,
                **_thread_kwargs(thread_ts),
            )
            return

        if answer and not posted_answers:
            _post(channel, answer)

    except Exception:
        log.error("[supervisor] UNHANDLED EXCEPTION:\n%s", traceback.format_exc())
        app.client.chat_postMessage(
            channel=channel,
            text=(
                "⚠️ Something went wrong looking that up — "
                "check the bot logs for the full error."
            ),
            **_thread_kwargs(thread_ts),
        )


# ---------------------------------------------------------------------------
# Message handler — DMs
# ---------------------------------------------------------------------------

@app.event("message")
def handle_dm_message(event: dict, say: Any, logger: Any) -> None:
    """Handle direct messages. Channel messages are left to app_mention."""
    if event.get("bot_id") or event.get("subtype"):
        return
    if not _is_direct_message(event):
        return

    user_text: str = event.get("text", "").strip()
    channel: str = event["channel"]
    user: str = event.get("user", "unknown")

    if not user_text:
        return
    if service_registry.shutting_down:
        say(text="⏳ Ask Buddy is restarting — try again in a moment.",
            channel=channel)
        return

    decision = limiter.check(user)
    if not decision.allowed:
        log.warning("[ratelimit] blocked DM | user=%s reason=%s", user, decision.reason)
        say(text=decision.slack_message(), channel=channel)
        return

    request_id = set_request_id(new_request_id())
    log.info("[event] DM received | user=%s", user)

    say(text="⏳ Looking that up…", channel=channel)

    t = threading.Thread(
        target=_run_agent_for_message,
        args=(user_text, channel, user, None, request_id),
        daemon=True,
    )
    t.start()
    service_registry.track_worker(t)


# ---------------------------------------------------------------------------
# Slash command — /askbuddy <question>
# ---------------------------------------------------------------------------

@app.command("/askbuddy")
def handle_slash_command(ack: Any, command: dict, respond: Any) -> None:
    """Handle the /askbuddy slash command, usable in any channel or DM."""
    ack()  # must acknowledge within 3s or Slack shows "app did not respond"

    user_text: str = command.get("text", "").strip()
    channel: str = command["channel_id"]
    user: str = command["user_id"]

    request_id = set_request_id(new_request_id())
    log.info("[event] slash command received | user=%s", user)

    if not user_text:
        respond("Ask me something, e.g. `/askbuddy how many PTO days do I get?`")
        return

    # ---------------------------------------------------------------------------
    # Built-in subcommands — handled directly, no agent invocation needed
    # ---------------------------------------------------------------------------

    # /askbuddy git digest [owner/repo]
    # Posts an on-demand repo digest to the current channel immediately.
    lower = user_text.lower()
    if lower == "git digest" or lower.startswith("git digest "):
        from .git_digest import post_digest
        from .git_watch import _watched_repos

        # optional repo argument: /askbuddy git digest owner/repo
        parts = user_text.split(None, 2)
        specific_repo = parts[2].strip() if len(parts) == 3 else None

        def _post_here(_channel: str, text: str) -> None:
            """Post to the channel the command came from, ignoring the channel
            the digest would normally target — an on-demand digest belongs where
            it was asked for. Returns None: post_digest wants a
            Callable[[str, str], None], not the Slack response."""
            app.client.chat_postMessage(channel=channel, text=text)

        def _run_digest() -> None:
            try:
                if specific_repo:
                    post_digest(specific_repo, _post_here)
                else:
                    repos = _watched_repos()
                    if not repos:
                        app.client.chat_postMessage(
                            channel=channel,
                            text="⚠️ No repos configured — set `GIT_WATCH_REPOS` in `.env`.",
                        )
                        return
                    for repo in repos:
                        post_digest(repo, _post_here)
            except Exception:
                log.exception("[git_digest] on-demand digest failed")
                app.client.chat_postMessage(
                    channel=channel,
                    text="⚠️ Could not fetch the digest — check bot logs.",
                )

        respond("⏳ Fetching repo digest…")
        t = threading.Thread(target=_run_digest, daemon=True)
        t.start()
        service_registry.track_worker(t)
        return

    # /askbuddy help
    if lower in ("help", "--help"):
        respond(
            "*Ask Buddy — available commands*\n\n"
            "*HR & IT policy questions* — just ask:\n"
            "  `/askbuddy how many PTO days do I get?`\n"
            "  `/askbuddy what's the VPN policy?`\n\n"
            "*GitHub questions & write actions*\n"
            "  `/askbuddy list open issues in acme/backend`\n"
            "  `/askbuddy is PR #17 ready to merge in acme/frontend?`\n"
            "  `/askbuddy add label bug to issue #5 in acme/backend`\n"
            "  `/askbuddy merge PR #17 in acme/frontend` _(requires confirmation)_\n\n"
            "*Reminders*\n"
            "  `/askbuddy remind #eng-team to submit timecards every Friday at 9am`\n"
            "  `/askbuddy list reminders in #eng-team`\n"
            "  `/askbuddy cancel reminder 42`\n\n"
            "*Subcommands*\n"
            "  `/askbuddy git digest` — post a full repo state digest now\n"
            "  `/askbuddy git digest owner/repo` — digest for one specific repo\n"
            "  `/askbuddy link github <your-login>` — link your GitHub account\n"
            "  `/askbuddy status` — show watched repos and current watermarks\n"
            "  `/askbuddy help` — this message"
        )
        return

    # /askbuddy status
    if lower == "status":
        from .git_watch import _watched_repos
        from .db import get_git_watermark
        repos = _watched_repos()
        lines = ["*Ask Buddy — status*\n"]
        if not repos:
            lines.append("_No repos configured — set `GIT_WATCH_REPOS` in `.env`._")
        for repo in repos:
            try:
                wm = get_git_watermark(repo)
                iss = wm["last_issue_number"]
                pr = wm["last_pr_number"]
                watermark_str = (
                    f"issue watermark: {iss if iss >= 0 else '_not seeded_'}, "
                    f"PR watermark: {pr if pr >= 0 else '_not seeded_'}"
                )
            except Exception as exc:
                watermark_str = f"_error reading watermark: {exc}_"
            lines.append(f"• `{repo}` — {watermark_str}")
        supervisor_count = len(_supervisor_cache)
        lines.append(f"\n_Active supervisor sessions: {supervisor_count}_")
        lines.append(f"_In-flight requests: {service_registry.active_workers()}_")
        lines.append(f"_Rate limits: {limiter.format_status()}_")
        lines.append("\n*Background services*")
        lines.append(service_registry.format_status())
        respond("\n".join(lines))
        return

    # /askbuddy link github <login>
    if lower.startswith("link github "):
        github_login = user_text.split(None, 2)[2].strip().lstrip("@")
        if not github_login:
            respond("Usage: `/askbuddy link github <your-github-username>`")
            return
        from .db import link_github_identity
        try:
            link_github_identity(user, github_login)
            respond(f"✅ Linked your Slack account to GitHub user `{github_login}`.")
        except Exception:
            log.exception("[git_identity] failed to link %s -> %s", user, github_login)
            respond("⚠️ Could not save that link — check bot logs.")
        return

    # ---------------------------------------------------------------------------
    # All other text → route to the agent as normal
    # ---------------------------------------------------------------------------

    if service_registry.shutting_down:
        respond("⏳ Ask Buddy is restarting — try again in a moment.")
        return

    # Checked here, not at the top: the built-in subcommands above cost nothing
    # and `status` in particular must stay reachable while throttled.
    decision = limiter.check(user)
    if not decision.allowed:
        log.warning("[ratelimit] blocked slash command | user=%s reason=%s",
                    user, decision.reason)
        respond(decision.slack_message())
        return

    respond("⏳ Looking that up…")

    t = threading.Thread(
        target=_run_agent_for_message,
        args=(user_text, channel, user, None, request_id),
        daemon=True,
    )
    t.start()
    service_registry.track_worker(t)


# ---------------------------------------------------------------------------
# Feedback action handlers
# ---------------------------------------------------------------------------

def _parse_response_id(body: dict) -> str | None:
    """Recover the response_id from a feedback button's JSON value payload."""
    action = body["actions"][0]
    try:
        return json.loads(action["value"])["response_id"]
    except (KeyError, json.JSONDecodeError):
        log.warning("[feedback] could not parse action value: %r", action.get("value"))
        return None


def _record_and_update(
    response_id: str,
    sentiment: str,
    user_id: str,
    channel: str,
    message_ts: str,
    reason: str | None = None,
) -> None:
    """
    Persist a rating and replace the message's buttons with a thank-you line.
    Shared by the 👍 click and the 👎 modal submission.
    """
    from .feedback import record_feedback, build_thankyou_blocks
    answer_text = record_feedback(response_id, sentiment, user_id, reason=reason)

    if answer_text is None:
        # Already rated — silently ignore the duplicate.
        log.info("[feedback] duplicate rating ignored response_id=%s user=%s",
                 response_id, user_id)
        return

    log.info("[feedback] recorded sentiment=%s reason=%s response_id=%s user=%s",
             sentiment, reason, response_id, user_id)

    blocks = build_thankyou_blocks(answer_text, sentiment)
    try:
        app.client.chat_update(
            channel=channel,
            ts=message_ts,
            text=answer_text,
            blocks=blocks,
        )
    except Exception:
        log.warning("[feedback] could not update message:\n%s",
                    traceback.format_exc())


@app.action("feedback_positive")
def handle_feedback_positive(body: dict, ack: Any) -> None:
    """👍 records immediately — no extra prompt needed for a good answer."""
    ack()
    response_id = _parse_response_id(body)
    if response_id is None:
        return
    _record_and_update(
        response_id,
        sentiment="positive",
        user_id=body["user"]["id"],
        channel=body["channel"]["id"],
        message_ts=body["message"]["ts"],
    )


@app.action("feedback_negative")
def handle_feedback_negative(body: dict, ack: Any, client: Any) -> None:
    """
    👎 opens a modal asking *why* before recording — the reason is far
    richer signal than a bare thumbs-down. The row is only written on
    modal submission (see handle_reason_submission).
    """
    ack()
    response_id = _parse_response_id(body)
    if response_id is None:
        return

    from .feedback import build_reason_modal
    modal = build_reason_modal(
        response_id=response_id,
        channel=body["channel"]["id"],
        message_ts=body["message"]["ts"],
    )
    try:
        client.views_open(trigger_id=body["trigger_id"], view=modal)
    except Exception:
        # If the modal can't open (e.g. Interactivity misconfigured), fall
        # back to recording the negative rating without a reason so the
        # signal isn't lost.
        log.warning("[feedback] could not open reason modal, recording bare 👎:\n%s",
                    traceback.format_exc())
        _record_and_update(
            response_id,
            sentiment="negative",
            user_id=body["user"]["id"],
            channel=body["channel"]["id"],
            message_ts=body["message"]["ts"],
        )


@app.view("feedback_reason_submit")
def handle_reason_submission(ack: Any, body: dict, view: dict, logger: Any) -> None:
    """Record the 👎 rating together with the reason chosen in the modal."""
    ack()

    try:
        meta = json.loads(view["private_metadata"])
        selected = (
            view["state"]["values"]["reason_block"]["reason_choice"]["selected_option"]
        )
        reason = selected["value"] if selected else None
    except (KeyError, json.JSONDecodeError, TypeError):
        log.warning("[feedback] could not parse modal submission:\n%s",
                    traceback.format_exc())
        return

    _record_and_update(
        meta["response_id"],
        sentiment="negative",
        user_id=body["user"]["id"],
        channel=meta["channel"],
        message_ts=meta["message_ts"],
        reason=reason,
    )


# ---------------------------------------------------------------------------
# GitHub write-action approval handlers (merge / close / reopen confirmation)
# ---------------------------------------------------------------------------

@app.action("git_approval_confirm")
def handle_git_approval_confirm(body: dict, ack: Any) -> None:
    ack()
    _handle_git_approval(body, confirmed=True)


@app.action("git_approval_deny")
def handle_git_approval_deny(body: dict, ack: Any) -> None:
    ack()
    _handle_git_approval(body, confirmed=False)


def _handle_git_approval(body: dict, confirmed: bool) -> None:
    action = body["actions"][0]
    try:
        thread_id = json.loads(action["value"])["thread_id"]
    except (KeyError, json.JSONDecodeError):
        log.warning("[git_approval] could not parse action value: %r", action.get("value"))
        return

    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    user_id = body["user"]["id"]

    pending = _pop_pending_approval(thread_id)
    if pending is None:
        # No live supervisor to resume. The audit row says which case this is,
        # so the user gets a reason rather than a shrug.
        reason = "_This approval has expired or was already handled._"
        try:
            from .db import get_unresolved_approvals
            still_open = any(r["thread_id"] == thread_id
                             for r in get_unresolved_approvals())
            if not still_open:
                reason = ("_This confirmation is no longer valid — it was already "
                          "handled, it expired, or Ask Buddy restarted. Nothing "
                          "was changed on GitHub._")
        except Exception:
            log.warning("[git_approval] could not classify a stale approval:\n%s",
                        traceback.format_exc())
        app.client.chat_update(
            channel=channel, ts=message_ts,
            text="This approval is no longer valid.",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": reason}}],
        )
        return

    try:
        from .db import resolve_pending_approval
        resolve_pending_approval(thread_id,
                                 outcome="confirmed" if confirmed else "cancelled",
                                 resolved_by=user_id)
    except Exception:
        log.warning("[git_approval] could not close out the audit row:\n%s",
                    traceback.format_exc())

    verb = "Confirmed" if confirmed else "Cancelled"
    app.client.chat_update(
        channel=channel, ts=message_ts,
        text=f"{verb} — processing…",
        blocks=[{"type": "section", "text": {
            "type": "mrkdwn", "text": f"_{verb} by <@{user_id}> — processing…_",
        }}],
    )

    t = threading.Thread(
        target=_resume_after_approval,
        args=(pending, thread_id, confirmed, channel, user_id),
        daemon=True,
    )
    t.start()
    service_registry.track_worker(t)


def _resume_after_approval(pending: dict, thread_id: str, confirmed: bool,
                           channel: str, user_id: str) -> None:
    """Resume the SAME paused supervisor object with the user's decision."""
    from cuga.backend.cuga_graph.nodes.human_in_the_loop.followup_model import (
        ActionResponse, ActionType,
    )

    set_request_id(new_request_id())
    supervisor = pending["supervisor"]
    thread_ts = pending.get("thread_ts")
    approval = ActionResponse(
        action_id="tool_approval",
        response_type=ActionType.CONFIRMATION,
        confirmed=confirmed,
        timestamp=datetime.now(timezone.utc).isoformat(),
        user_id=user_id,
        session_id=thread_id,
    )

    try:
        result = asyncio.run(
            supervisor.invoke(None, thread_id=thread_id, action_response=approval)
        )
        answer = getattr(result, "answer", None)

        if answer and PAUSED_MARKER in answer:
            # Chained approval (rare — e.g. two gated tools in one turn).
            # Stash again and prompt once more instead of dropping it. The
            # previous entry was popped by the click, so this can't collide.
            _stash_pending_approval(
                thread_id, supervisor, channel,
                pending["user_text"], pending["agent_config"],
                pending["retrieved_chunk_ids"], thread_ts=thread_ts,
            )
            blocks = _build_approval_blocks(
                thread_id, "⚠️ *One more confirmation needed before this completes.*",
            )
            app.client.chat_postMessage(
                channel=channel, text="Another confirmation needed.", blocks=blocks,
                **_thread_kwargs(thread_ts),
            )
            return

        if not answer:
            answer = ("Action cancelled — nothing was changed on GitHub."
                      if not confirmed else "Done.")

        _post_answer_with_feedback(
            channel, answer, pending["user_text"],
            retrieved_chunk_ids=pending["retrieved_chunk_ids"],
            agent_config=pending["agent_config"],
            thread_ts=thread_ts,
        )
    except Exception:
        log.error("[git_approval] UNHANDLED EXCEPTION resuming thread=%s:\n%s",
                  thread_id, traceback.format_exc())
        app.client.chat_postMessage(
            channel=channel,
            text="⚠️ Something went wrong completing that action — check the bot logs.",
            **_thread_kwargs(thread_ts),
        )


# ---------------------------------------------------------------------------
# App-mention handler — channel @mentions
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_app_mention(event: dict, say: Any, logger: Any) -> None:
    """Handle @AskBuddy mentions in channels."""
    import re
    raw_text: str = event.get("text", "")
    user_text = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

    channel: str = event["channel"]
    user: str = event.get("user", "unknown")
    thread_ts: str | None = event.get("thread_ts") or event.get("ts")

    if not user_text:
        say(text="Hi! Ask me anything about Acme Corp HR or IT policies.",
            channel=channel, thread_ts=thread_ts)
        return

    if service_registry.shutting_down:
        say(text="⏳ Ask Buddy is restarting — try again in a moment.",
            channel=channel, thread_ts=thread_ts)
        return

    decision = limiter.check(user)
    if not decision.allowed:
        log.warning("[ratelimit] blocked mention | user=%s reason=%s",
                    user, decision.reason)
        say(text=decision.slack_message(), channel=channel, thread_ts=thread_ts)
        return

    request_id = set_request_id(new_request_id())
    log.info("[event] app_mention received | user=%s", user)

    say(text="⏳ Looking that up…", channel=channel,
        thread_ts=thread_ts)

    t = threading.Thread(
        target=_run_agent_for_message,
        args=(user_text, channel, user, thread_ts, request_id),
        daemon=True,
    )
    t.start()
    service_registry.track_worker(t)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting Ask Buddy in Socket Mode…")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    # SocketModeHandler.start() blocks, so the signal handler has to close it
    # before the registry drains — otherwise SIGTERM would be handled but the
    # process would sit in start() forever.
    def _close_socket() -> None:
        try:
            handler.close()
        except Exception:
            log.warning("[lifecycle] could not close Socket Mode handler",
                        exc_info=True)

    install_signal_handlers(
        service_registry,
        wait_seconds=float(os.environ.get("ASK_BUDDY_SHUTDOWN_WAIT", "10")),
        on_signal=_close_socket,
    )

    try:
        handler.start()
    finally:
        # Covers a clean return from start() as well as the signal path.
        service_registry.shutdown(
            wait_seconds=float(os.environ.get("ASK_BUDDY_SHUTDOWN_WAIT", "10"))
        )


if __name__ == "__main__":
    main()
