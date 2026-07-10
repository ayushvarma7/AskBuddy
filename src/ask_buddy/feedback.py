"""
Feedback helpers for Ask Buddy.

Provides:
  - build_answer_blocks()  — Block Kit message with thumbs-up/down buttons
  - store_feedback_row()   — insert a pending row before the user clicks
  - record_feedback()      — update row when a button is clicked
  - mark_buttons_used()    — replace buttons with a "thanks" confirmation
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .db import get_conn


# ---------------------------------------------------------------------------
# Block Kit builder
# ---------------------------------------------------------------------------

def build_answer_blocks(
    answer_text: str,
    question: str,
    response_id: str,
) -> list[dict[str, Any]]:
    """
    Return a Slack Block Kit block list:

        <answer text>
        ─────────────────────
        Was this helpful?   👍  👎

    The button values carry a JSON payload so the action handler can
    recover the response_id without a DB lookup on every click.
    """
    payload_base = json.dumps({"response_id": response_id, "question": question[:200]})

    return [
        # Answer text
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": answer_text},
        },
        # Thin divider
        {"type": "divider"},
        # Feedback prompt + buttons
        {
            "type": "actions",
            "block_id": f"feedback_{response_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👍  Helpful", "emoji": True},
                    "style": "primary",
                    "action_id": "feedback_positive",
                    "value": payload_base,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👎  Not helpful", "emoji": True},
                    "action_id": "feedback_negative",
                    "value": payload_base,
                },
            ],
        },
    ]


def build_thankyou_blocks(answer_text: str, sentiment: str) -> list[dict[str, Any]]:
    """Replace the feedback buttons with a one-line confirmation."""
    icon = "✅" if sentiment == "positive" else "🙏"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": answer_text},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{icon} _Thanks for the feedback!_",
            },
        },
    ]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def new_response_id() -> str:
    return str(uuid.uuid4())


def store_feedback_row(
    response_id: str,
    question: str,
    answer_text: str,
    sources_cited: str,
) -> None:
    """
    Insert a pending row into ask_buddy_feedback when the answer is posted.
    feedback/user_id are NULL until the user clicks a button.
    """
    sql = """
        INSERT INTO ask_buddy_feedback
            (response_id, question, answer_text, sources_cited)
        VALUES
            (%(response_id)s, %(question)s, %(answer_text)s, %(sources_cited)s)
        ON CONFLICT (response_id) DO NOTHING;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "response_id": response_id,
                "question": question,
                "answer_text": answer_text,
                "sources_cited": sources_cited,
            })


def record_feedback(response_id: str, sentiment: str, user_id: str) -> str | None:
    """
    Set feedback + user_id on an existing row.
    Returns the answer_text so the caller can rebuild the blocks,
    or None if the response_id is not found.
    """
    sql = """
        UPDATE ask_buddy_feedback
           SET feedback = %(sentiment)s,
               user_id  = %(user_id)s
         WHERE response_id = %(response_id)s
           AND feedback IS NULL          -- prevent double-click overwrites
        RETURNING answer_text;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "sentiment": sentiment,
                "user_id": user_id,
                "response_id": response_id,
            })
            row = cur.fetchone()
    return dict(row)["answer_text"] if row else None


# ---------------------------------------------------------------------------
# Source extraction helper
# ---------------------------------------------------------------------------

def extract_sources(answer_text: str) -> str:
    """
    Pull the 'Source(s): …' lines out of an answer for storage.
    Returns an empty string if no sources line is present.
    """
    lines = answer_text.splitlines()
    collecting = False
    sources: list[str] = []
    for line in lines:
        if line.strip().startswith("Source(s):"):
            collecting = True
        if collecting and line.strip():
            sources.append(line.strip())
    return "\n".join(sources)
