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
import logging
import os
import re
import sys
import threading
import traceback

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
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

app = App(token=SLACK_BOT_TOKEN)

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


# ---------------------------------------------------------------------------
# Channel name -> ID resolution (needed by the scheduler_agent's reminder
# tools, since users name a channel like 'svl-interns-2026', not its ID)
# ---------------------------------------------------------------------------

_CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{8,}$")
_channel_name_to_id: dict[str, str] = {}


def _refresh_channel_cache() -> None:
    global _channel_name_to_id
    cache: dict[str, str] = {}
    cursor = None
    while True:
        resp = app.client.conversations_list(
            types="public_channel,private_channel", limit=200, cursor=cursor
        )
        for ch in resp.get("channels", []):
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


try:
    from .scheduler import start_scheduler
    start_scheduler(_plain_post)
    log.info("Reminder scheduler started.")
except Exception as _e:
    log.warning("Could not start reminder scheduler: %s", _e)


# ---------------------------------------------------------------------------
# Core: run agent in a background thread to avoid blocking Bolt's event loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Core: post answer with Block Kit feedback buttons
# ---------------------------------------------------------------------------

def _post_answer_with_feedback(
    channel: str,
    answer_text: str,
    question: str,
    retrieved_chunk_ids: list[int] | None = None,
    agent_config: str | None = None,
) -> None:
    """
    Post the answer as a Block Kit message with 👍 / 👎 buttons,
    and pre-insert a pending row in ask_buddy_feedback.

    Refusals are tagged is_refusal=TRUE and store no chunk IDs (the agent
    refuses without using retrieval), so downstream chunk-quality scoring
    only counts real answers.
    """
    from .feedback import (
        new_response_id, build_answer_blocks,
        store_feedback_row, extract_sources, is_refusal_text,
    )
    response_id = new_response_id()
    sources = extract_sources(answer_text)
    refusal = is_refusal_text(answer_text)
    chunk_ids = None if refusal else retrieved_chunk_ids
    blocks = build_answer_blocks(answer_text, question, response_id,
                                 is_refusal=refusal)

    try:
        store_feedback_row(
            response_id, question, answer_text, sources,
            retrieved_chunk_ids=chunk_ids,
            is_refusal=refusal,
            agent_config=agent_config,
        )
    except Exception:
        log.warning("[feedback] could not store pending row:\n%s",
                    traceback.format_exc())

    app.client.chat_postMessage(
        channel=channel,
        text=answer_text,          # fallback for notifications
        blocks=blocks,
    )
    log.info("[feedback] posted response_id=%s refusal=%s chunks=%s",
             response_id, refusal, chunk_ids)


# ---------------------------------------------------------------------------
# Core: run agent in a background thread to avoid blocking Bolt's event loop
# ---------------------------------------------------------------------------

def _run_agent_for_message(user_text: str, channel: str, user: str,
                            thread_ts: str | None = None) -> None:
    """
    Run the CugaSupervisor in a plain thread.

    The supervisor decides whether the query is HR, IT, or cross-domain,
    then delegates to the appropriate sub-agent(s).
    """
    posted_answers: list[str] = []
    retrieved_chunk_ids: list[int] = []

    from src.ask_buddy.agent import current_agent_config
    agent_config = current_agent_config()

    def _post(ch: str, text: str) -> None:
        _post_answer_with_feedback(
            ch, text, user_text,
            retrieved_chunk_ids=list(retrieved_chunk_ids),
            agent_config=agent_config,
        )
        posted_answers.append(text)

    prompt = (
        f"A Slack user (id: {user}) in channel {channel} asks:\n\n"
        f"{user_text}\n\n"
        f"Route this to the right agent, then call "
        f"post_slack_message with channel='{channel}' to deliver your response."
    )

    log.info("[supervisor] starting | user=%s channel=%s query=%r",
             user, channel, user_text[:120])

    try:
        from src.ask_buddy.retrieve import _hybrid_retrieve_core
        chunks = _hybrid_retrieve_core(user_text, top_k=5)
        if chunks and "error" in chunks[0]:
            raise RuntimeError(f"retrieve error: {chunks[0]['error']}")
        retrieved_chunk_ids[:] = [c["id"] for c in chunks if "id" in c]
        log.info("[supervisor] pre-retrieved %d chunks: %s",
                 len(chunks), [c.get("source_filename") for c in chunks])

        from src.ask_buddy.agent import build_supervisor
        supervisor = build_supervisor(
            slack_post_fn=_post,
            resolve_channel_fn=resolve_channel,
            created_by=user,
        )
        result = asyncio.run(
            supervisor.invoke(prompt, thread_id=f"slack-{channel}-{user}")
        )
        log.info("[supervisor] invoke complete | answer[:120]=%r",
                 (getattr(result, "answer", None) or "")[:120])

        answer = getattr(result, "answer", None)
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
        )


# ---------------------------------------------------------------------------
# Message handler — DMs
# ---------------------------------------------------------------------------

@app.event("message")
def handle_dm_message(event, say, logger):
    """Handle direct messages."""
    if event.get("bot_id") or event.get("subtype"):
        return

    user_text: str = event.get("text", "").strip()
    channel: str = event["channel"]
    user: str = event.get("user", "unknown")

    if not user_text:
        return

    say(text="⏳ Looking that up…", channel=channel)

    t = threading.Thread(
        target=_run_agent_for_message,
        args=(user_text, channel, user),
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# Slash command — /askbuddy <question>
# ---------------------------------------------------------------------------

@app.command("/askbuddy")
def handle_slash_command(ack, command, respond):
    """Handle the /askbuddy slash command, usable in any channel or DM."""
    ack()  # must acknowledge within 3s or Slack shows "app did not respond"

    user_text: str = command.get("text", "").strip()
    channel: str = command["channel_id"]
    user: str = command["user_id"]

    if not user_text:
        respond("Ask me something, e.g. `/askbuddy how many PTO days do I get?`")
        return

    respond("⏳ Looking that up…")

    t = threading.Thread(
        target=_run_agent_for_message,
        args=(user_text, channel, user),
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# Feedback action handlers
# ---------------------------------------------------------------------------

def _parse_response_id(body: dict) -> str | None:
    """Recover the response_id from a feedback button's JSON value payload."""
    import json
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
def handle_feedback_positive(body, ack):
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
def handle_feedback_negative(body, ack, client):
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
def handle_reason_submission(ack, body, view, logger):
    """Record the 👎 rating together with the reason chosen in the modal."""
    import json
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
# App-mention handler — channel @mentions
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_app_mention(event, say, logger):
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

    say(text="⏳ Looking that up…", channel=channel,
        thread_ts=thread_ts)

    t = threading.Thread(
        target=_run_agent_for_message,
        args=(user_text, channel, user, thread_ts),
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting Ask Buddy in Socket Mode…")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
