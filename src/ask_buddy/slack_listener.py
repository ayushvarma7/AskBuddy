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

# Ensure the newer feature tables exist at startup (idempotent):
#   digest history (trends), PR-nudge dedup, per-user memory, eval runs.
for _init_name in (
    "init_git_digest_history_schema",
    "init_pr_nudge_schema",
    "init_user_memory_schema",
    "init_eval_runs_schema",
):
    try:
        from . import db as _db
        getattr(_db, _init_name)()
        log.info("%s ok.", _init_name)
    except Exception as _e:
        log.warning("Could not run %s: %s", _init_name, _e)


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


def _get_or_build_supervisor(thread_id: str, slack_post_fn, resolve_channel_fn,
                              created_by: str | None):
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


def _stash_pending_approval(thread_id: str, supervisor, channel: str, user_text: str,
                            agent_config: str, retrieved_chunk_ids: list[int],
                            thread_ts: str | None = None) -> None:
    with _pending_approvals_lock:
        _pending_approvals[thread_id] = {
            "supervisor": supervisor,
            "channel": channel,
            "user_text": user_text,
            "agent_config": agent_config,
            "retrieved_chunk_ids": list(retrieved_chunk_ids),
            "thread_ts": thread_ts,
            "created_at": time.time(),
        }


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


def _start_approval_sweep() -> None:
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

# Proactive GitHub triage watcher (polls GitHub, posts to GIT_WATCH_CHANNEL)
try:
    from .git_watch import start_git_watch
    start_git_watch(_plain_post)   # reuse the no-feedback-buttons poster
except Exception as _e:
    log.warning("Could not start git watch: %s", _e)

# Daily GitHub repo digest (scheduled summary of open/closed issues + PRs)
try:
    from .git_digest import start_git_digest
    start_git_digest(_plain_post)
except Exception as _e:
    log.warning("Could not start git digest: %s", _e)

# Stale-PR nudger (DMs reviewers about PRs waiting too long for review)
try:
    from .git_stale import start_git_stale
    start_git_stale(_plain_post)
except Exception as _e:
    log.warning("Could not start stale-PR nudger: %s", _e)

# Weekly per-user proactive digest (DMs linked users their PRs + policy spotlight)
try:
    from .personal_digest import start_personal_digest
    start_personal_digest(_plain_post)
except Exception as _e:
    log.warning("Could not start personal digest: %s", _e)

# Nightly LLM-as-judge retrieval eval (self-tuning quality signal)
try:
    from .eval_runner import start_eval_scheduler
    start_eval_scheduler(_plain_post)
except Exception as _e:
    log.warning("Could not start nightly eval: %s", _e)

# Sweep pending GitHub write-action approvals that were never confirmed
try:
    _start_approval_sweep()
except Exception as _e:
    log.warning("Could not start approval sweep: %s", _e)


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
    thread_ts: str | None = None,
) -> None:
    """
    Post the answer as a Block Kit message with 👍 / 👎 buttons,
    and pre-insert a pending row in ask_buddy_feedback.

    Refusals are tagged is_refusal=TRUE and store no chunk IDs (the agent
    refuses without using retrieval), so downstream chunk-quality scoring
    only counts real answers.

    When thread_ts is set the reply lands in that Slack thread, so a channel
    conversation stays threaded instead of flat.
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

    _post_kwargs = {"channel": channel, "text": answer_text, "blocks": blocks}
    if thread_ts:
        _post_kwargs["thread_ts"] = thread_ts
    try:
        app.client.chat_postMessage(**_post_kwargs)
    except Exception as _post_err:
        from slack_sdk.errors import SlackApiError
        if isinstance(_post_err, SlackApiError) and _post_err.response.get("error") == "channel_not_found":
            log.error(
                "[feedback] channel_not_found for channel=%s — "
                "the bot must be invited to the channel first "
                "(run `/invite @AskBuddy` in that channel).",
                channel,
            )
        else:
            raise
    log.info("[feedback] posted response_id=%s refusal=%s chunks=%s",
             response_id, refusal, chunk_ids)


# ---------------------------------------------------------------------------
# Core: run agent in a background thread to avoid blocking Bolt's event loop
# ---------------------------------------------------------------------------

def _run_agent_for_message(user_text: str, channel: str, user: str,
                            thread_ts: str | None = None) -> None:
    """
    Run the CugaSupervisor in a background thread.

    Supervisors are cached per (channel, user) so the in-memory MemorySaver
    checkpointer preserves conversation history across turns.  A separate
    invoke_id is used for each invocation so concurrent messages from the
    same user don't clobber each other's checkpoint slot.
    """
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

    # thread_id keys both the supervisor cache and the approval stash. When the
    # message is in a Slack thread, key per-thread so each thread is its own
    # conversation with isolated memory; otherwise key per (channel, user) so a
    # flat DM stays one continuous conversation.
    if thread_ts:
        thread_id = f"slack-{channel}-{thread_ts}"
    else:
        thread_id = f"slack-{channel}-{user}"

    prompt = (
        f"A Slack user (id: {user}) in channel {channel} asks:\n\n"
        f"{user_text}\n\n"
        f"Route this to the right agent, then call "
        f"post_slack_message with channel='{channel}' to deliver your response."
    )

    log.info("[supervisor] starting | user=%s channel=%s query=%r",
             user, channel, user_text[:120])

    try:
        from .retrieve import _hybrid_retrieve_core
        chunks = _hybrid_retrieve_core(user_text, top_k=5)
        if chunks and "error" in chunks[0]:
            log.warning("[supervisor] pre-retrieval error (non-fatal): %s",
                        chunks[0]["error"])
        else:
            retrieved_chunk_ids[:] = [c["id"] for c in chunks if "id" in c]
            log.info("[supervisor] pre-retrieved %d chunks: %s",
                     len(chunks), [c.get("source_filename") for c in chunks])

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
            _stash_pending_approval(
                thread_id, supervisor, channel, user_text,
                agent_config, retrieved_chunk_ids, thread_ts=thread_ts,
            )
            blocks = _build_approval_blocks(
                thread_id,
                "⚠️ *This action needs your confirmation before it runs on "
                "GitHub* (e.g. merging a pull request or closing an issue).",
                action_summary=user_text[:120],
            )
            _approval_kwargs = {
                "channel": channel,
                "text": "Confirmation needed before this action runs.",
                "blocks": blocks,
            }
            if thread_ts:
                _approval_kwargs["thread_ts"] = thread_ts
            app.client.chat_postMessage(**_approval_kwargs)
            return

        if answer and not posted_answers:
            _post(channel, answer)

    except Exception:
        log.error("[supervisor] UNHANDLED EXCEPTION:\n%s", traceback.format_exc())
        try:
            app.client.chat_postMessage(
                channel=channel,
                text=(
                    "⚠️ Something went wrong looking that up — "
                    "check the bot logs for the full error."
                ),
            )
        except Exception:
            log.error("[supervisor] could not post error message to channel=%s:\n%s",
                      channel, traceback.format_exc())


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
    # A DM can itself be threaded; preserve the thread so follow-ups in that
    # thread continue the same conversation.
    thread_ts: str | None = event.get("thread_ts")

    if not user_text:
        return

    say(text="⏳ Looking that up…", channel=channel, thread_ts=thread_ts)

    t = threading.Thread(
        target=_run_agent_for_message,
        args=(user_text, channel, user, thread_ts),
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

    # ---------------------------------------------------------------------------
    # Built-in subcommands — handled directly, no agent invocation needed
    # ---------------------------------------------------------------------------

    # /askbuddy git digest [owner/repo]
    # Posts an on-demand repo digest to the current channel immediately.
    lower = user_text.lower()
    if lower == "git digest" or lower.startswith("git digest "):
        from .git_digest import post_digest, post_all_digests
        from .git_watch import _watched_repos

        # optional repo argument: /askbuddy git digest owner/repo
        parts = user_text.split(None, 2)
        specific_repo = parts[2].strip() if len(parts) == 3 else None

        def _run_digest():
            try:
                if specific_repo:
                    post_digest(specific_repo, lambda ch, txt: app.client.chat_postMessage(
                        channel=channel, text=txt))
                else:
                    repos = _watched_repos()
                    if not repos:
                        app.client.chat_postMessage(
                            channel=channel,
                            text="⚠️ No repos configured — set `GIT_WATCH_REPOS` in `.env`.",
                        )
                        return
                    for repo in repos:
                        post_digest(repo, lambda ch, txt: app.client.chat_postMessage(
                            channel=channel, text=txt))
            except Exception:
                log.exception("[git_digest] on-demand digest failed")
                app.client.chat_postMessage(
                    channel=channel,
                    text="⚠️ Could not fetch the digest — check bot logs.",
                )

        respond("⏳ Fetching repo digest…")
        t = threading.Thread(target=_run_digest, daemon=True)
        t.start()
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
            "  `/askbuddy pr card owner/repo #17` — interactive merge-readiness card\n"
            "  `/askbuddy link github <your-login>` — link your GitHub account\n"
            "  `/askbuddy remember <fact>` — teach me a durable fact about you\n"
            "  `/askbuddy forget me` — clear everything I remember about you\n"
            "  `/askbuddy config` — best agent config by feedback so far\n"
            "  `/askbuddy status` — show watched repos and current watermarks\n"
            "  `/askbuddy help` — this message"
        )
        return

    # /askbuddy status
    if lower == "status":
        from .git_watch import _watched_repos
        from .db import get_git_watermark
        repos = _watched_repos()
        if not repos:
            respond("No repos configured — set `GIT_WATCH_REPOS` in `.env`.")
            return
        lines = ["*Ask Buddy — watcher status*\n"]
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

    # /askbuddy pr card owner/repo #N   (also: "merge card …")
    # Posts an interactive merge-readiness card with Merge buttons.
    if lower.startswith(("pr card", "merge card")):
        import re as _re
        repo_m = _re.search(r"([\w.-]+/[\w.-]+)", user_text)
        # Strip the repo before hunting for the PR number so a digit inside the
        # repo name (e.g. 'acme2/backend') can't be mistaken for the number.
        remainder = user_text.replace(repo_m.group(1), " ") if repo_m else user_text
        num_m = _re.search(r"(\d+)", remainder)
        if not repo_m or not num_m:
            respond("Usage: `/askbuddy pr card owner/repo #17`")
            return
        repo = repo_m.group(1)
        number = int(num_m.group(1))

        def _run_card():
            try:
                from .git_ui import fetch_merge_card
                fallback, blocks = fetch_merge_card(repo, number)
                app.client.chat_postMessage(channel=channel, text=fallback, blocks=blocks)
            except Exception:
                log.exception("[merge_card] failed for %s#%s", repo, number)
                app.client.chat_postMessage(
                    channel=channel,
                    text=f"⚠️ Couldn't load PR #{number} in `{repo}` — check the repo/number and bot logs.",
                )

        respond("⏳ Building merge-readiness card…")
        threading.Thread(target=_run_card, daemon=True).start()
        return

    # /askbuddy remember <fact about me>
    if lower.startswith("remember "):
        fact = user_text.split(None, 1)[1].strip()
        if not fact:
            respond("Usage: `/askbuddy remember I work in the SF office`")
            return
        from .db import add_user_memory
        try:
            add_user_memory(user, fact)
            respond(f"✅ Noted — I'll remember: _{fact}_")
        except Exception:
            log.exception("[user_memory] failed to store fact for %s", user)
            respond("⚠️ Couldn't save that — check bot logs.")
        return

    # /askbuddy forget me
    if lower in ("forget me", "forget"):
        from .db import clear_user_memory
        try:
            removed = clear_user_memory(user)
            respond(f"🧹 Cleared {removed} remembered fact(s) about you.")
        except Exception:
            log.exception("[user_memory] failed to clear facts for %s", user)
            respond("⚠️ Couldn't clear your facts — check bot logs.")
        return

    # /askbuddy config  — show the best-performing agent config from feedback
    if lower == "config":
        from .agent import recommend_agent_config
        rec = recommend_agent_config()
        if rec["negative_rate"] is None:
            respond("Not enough rated feedback yet to recommend a config.")
            return
        verdict = ("👉 *Consider switching* — set `AGENT_SETTING_CONFIG` / `MODEL_NAME` accordingly."
                   if rec["should_switch"] else "✅ Current config is the best so far.")
        lines = [f"*Agent config recommendation*",
                 f"• Current: `{rec['current']}`",
                 f"• Best by feedback: `{rec['best']}` ({rec['negative_rate']*100:.0f}% negative)",
                 verdict, "", "_Candidates (min 20 rated):_"]
        for c in rec["candidates"][:5]:
            lines.append(f"  • `{c['config']}` — {c['negative_rate']*100:.0f}% neg ({c['rated']} rated)")
        respond("\n".join(lines))
        return

    # ---------------------------------------------------------------------------
    # All other text → route to the agent as normal
    # ---------------------------------------------------------------------------

    # Post the question publicly so it's visible in the channel, then a
    # loading indicator.  Fall back to ephemeral respond() if the bot isn't
    # a member of the channel (e.g. a private channel the bot hasn't joined).
    try:
        app.client.chat_postMessage(
            channel=channel,
            text=f"<@{user}> asked: {user_text}",
        )
        app.client.chat_postMessage(
            channel=channel,
            text="⏳ Looking that up…",
        )
    except Exception:
        from slack_sdk.errors import SlackApiError
        log.warning("[slash] could not post question to channel=%s, falling back to ephemeral", channel)
        respond(f"⏳ Looking that up… _(question: {user_text})_")

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
def handle_git_approval_confirm(body, ack):
    ack()
    _handle_git_approval(body, confirmed=True)


@app.action("git_approval_deny")
def handle_git_approval_deny(body, ack):
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
        app.client.chat_update(
            channel=channel, ts=message_ts,
            text="This approval has expired or was already handled.",
            blocks=[{"type": "section", "text": {
                "type": "mrkdwn",
                "text": "_This approval has expired or was already handled._",
            }}],
        )
        return

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


def _resume_after_approval(pending: dict, thread_id: str, confirmed: bool,
                           channel: str, user_id: str) -> None:
    """Resume the SAME paused supervisor object with the user's decision."""
    from cuga.backend.cuga_graph.nodes.human_in_the_loop.followup_model import (
        ActionResponse, ActionType,
    )

    supervisor = pending["supervisor"]
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

        _thread_ts = pending.get("thread_ts")
        if answer and PAUSED_MARKER in answer:
            # Chained approval (rare — e.g. two gated tools in one turn).
            # Stash again and prompt once more instead of dropping it.
            _stash_pending_approval(
                thread_id, supervisor, channel,
                pending["user_text"], pending["agent_config"],
                pending["retrieved_chunk_ids"], thread_ts=_thread_ts,
            )
            blocks = _build_approval_blocks(
                thread_id, "⚠️ *One more confirmation needed before this completes.*",
            )
            _chain_kwargs = {"channel": channel,
                             "text": "Another confirmation needed.", "blocks": blocks}
            if _thread_ts:
                _chain_kwargs["thread_ts"] = _thread_ts
            app.client.chat_postMessage(**_chain_kwargs)
            return

        if not answer:
            answer = ("Action cancelled — nothing was changed on GitHub."
                      if not confirmed else "Done.")

        _post_answer_with_feedback(
            channel, answer, pending["user_text"],
            retrieved_chunk_ids=pending["retrieved_chunk_ids"],
            agent_config=pending["agent_config"],
            thread_ts=_thread_ts,
        )
    except Exception:
        log.error("[git_approval] UNHANDLED EXCEPTION resuming thread=%s:\n%s",
                  thread_id, traceback.format_exc())
        app.client.chat_postMessage(
            channel=channel,
            text="⚠️ Something went wrong completing that action — check the bot logs.",
        )


# ---------------------------------------------------------------------------
# Interactive merge-readiness card handlers (point 3)
# ---------------------------------------------------------------------------

def _parse_action_value(body: dict) -> dict | None:
    try:
        return json.loads(body["actions"][0]["value"])
    except (KeyError, IndexError, json.JSONDecodeError):
        log.warning("[merge_card] could not parse action value")
        return None


@app.action("pr_merge_request")
def handle_pr_merge_request(body, ack):
    """First click on a Merge button — replace card with a Confirm/Cancel step."""
    ack()
    data = _parse_action_value(body)
    if data is None:
        return
    from .git_ui import build_merge_confirm_card
    app.client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"],
        text="Confirm merge?",
        blocks=build_merge_confirm_card(data["number"], data["repo"],
                                        data.get("method", "squash")),
    )


@app.action("pr_merge_cancel")
def handle_pr_merge_cancel(body, ack):
    ack()
    from .git_ui import build_result_blocks
    app.client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"],
        text="Merge cancelled.",
        blocks=build_result_blocks("❌ _Merge cancelled — nothing changed on GitHub._"),
    )


@app.action("pr_merge_confirm")
def handle_pr_merge_confirm(body, ack):
    """Confirmed — run the gated merge in a background thread, update the card."""
    ack()
    data = _parse_action_value(body)
    if data is None:
        return
    channel = body["channel"]["id"]
    ts = body["message"]["ts"]
    user_id = body["user"]["id"]
    method = data.get("method", "squash")

    from .git_ui import build_result_blocks
    app.client.chat_update(
        channel=channel, ts=ts, text="Merging…",
        blocks=build_result_blocks(
            f"⏳ _Merging PR #{data['number']} (requested by <@{user_id}>)…_"),
    )

    def _do_merge():
        from . import github_client as gh
        try:
            res = gh.merge_pull_request(data["repo"], data["number"], method)
            if res.get("merged"):
                txt = (f"✅ *Merged PR #{data['number']}* in `{data['repo']}` "
                       f"via `{method}` (by <@{user_id}>).")
            else:
                txt = (f"⚠️ GitHub declined to merge PR #{data['number']}: "
                       f"{res.get('message', 'no reason given')}")
        except gh.GitHubError as e:
            txt = f"⚠️ Merge failed: {e}"
        except Exception:
            log.exception("[merge_card] merge failed for %s#%s", data["repo"], data["number"])
            txt = "⚠️ Merge failed — check bot logs."
        app.client.chat_update(channel=channel, ts=ts, text=txt,
                               blocks=build_result_blocks(txt))

    threading.Thread(target=_do_merge, daemon=True).start()


# ---------------------------------------------------------------------------
# App Home dashboard (point 5) — feedback health + watcher status at a glance
# ---------------------------------------------------------------------------

def _build_home_view(user_id: str) -> dict:
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🤖 Ask Buddy", "emoji": True}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": "Your workplace assistant — HR/IT policy, GitHub, reminders."}]},
        {"type": "divider"},
    ]

    # Feedback health
    try:
        from .db import get_feedback_summary
        s = get_feedback_summary()
        rated = s["positive"] + s["negative"]
        pos_pct = (s["positive"] / rated * 100) if rated else 0
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*📊 Answer feedback*\n"
            f"• Total answers: *{s['total']}*\n"
            f"• 👍 {s['positive']}  ·  👎 {s['negative']}  ·  unrated {s['unrated']}\n"
            f"• Helpful rate: *{pos_pct:.0f}%*")}})
    except Exception:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "*📊 Answer feedback*\n_unavailable_"}})

    # Latest nightly eval
    try:
        from .db import get_eval_run_history
        hist = get_eval_run_history(limit=1)
        if hist and hist[0]["total"]:
            h = hist[0]
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*🧪 Latest retrieval eval*\n"
                f"• Pass rate: *{h['passed']}/{h['total']}*\n"
                f"• Avg judge score: *{round(h['avg_score'], 2) if h['avg_score'] is not None else 'n/a'}*")}})
    except Exception:
        pass

    # Watched repos + watermarks
    try:
        from .git_watch import _watched_repos
        from .db import get_git_watermark
        repos = _watched_repos()
        if repos:
            lines = ["*🔭 Watched repos*"]
            for repo in repos:
                try:
                    wm = get_git_watermark(repo)
                    lines.append(f"• `{repo}` — issue #{wm['last_issue_number']}, PR #{wm['last_pr_number']}")
                except Exception:
                    lines.append(f"• `{repo}`")
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    except Exception:
        pass

    # Personalisation for this user
    try:
        from .db import get_github_login, get_user_memories
        login = get_github_login(user_id)
        facts = get_user_memories(user_id, limit=5)
        parts = ["*🙋 You*"]
        parts.append(f"• GitHub: `{login}`" if login
                     else "• GitHub: _not linked_ — `/askbuddy link github <login>`")
        if facts:
            parts.append("• I remember: " + "; ".join(f[:60] for f in facts))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(parts)}})
    except Exception:
        pass

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": "Type `/askbuddy help` for everything I can do."}]})
    return {"type": "home", "blocks": blocks}


@app.event("app_home_opened")
def handle_app_home_opened(event, client):
    """Render the App Home dashboard when a user opens the bot's Home tab."""
    user_id = event.get("user")
    if not user_id:
        return
    try:
        client.views_publish(user_id=user_id, view=_build_home_view(user_id))
    except Exception:
        log.warning("[home] could not publish home view:\n%s", traceback.format_exc())


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
