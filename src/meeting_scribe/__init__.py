"""MeetingScribe - A CUGA agent that extracts and executes meeting outcomes."""

from .tools import (
    parse_transcript,
    extract_action_items,
    create_task,
    send_message,
    schedule_followup,
)
from .agent import create_meeting_scribe_agent

__all__ = [
    "parse_transcript",
    "extract_action_items",
    "create_task",
    "send_message",
    "schedule_followup",
    "create_meeting_scribe_agent",
]

# Made with Bob
