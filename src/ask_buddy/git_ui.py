"""
Interactive GitHub Block Kit cards for Ask Buddy.

The merge-readiness card turns github_client.get_pr_merge_status() (which
already fuses draft state, review verdicts, and CI into one verdict) into a
live Slack card. When nothing is blocking, the card offers Merge buttons that
flow straight into a confirm step and then the gated merge — the card itself
is the review surface, collapsing "check readiness" and "merge" into one place.

Builders here are pure (no network) so they're unit-testable; the network call
(fetch_merge_card) and the button handlers live in slack_listener.
"""

from __future__ import annotations

import json
from typing import Any

from . import github_client as gh


def _payload(**kw) -> str:
    return json.dumps(kw)


def build_merge_card(pr: dict, status: dict) -> list[dict[str, Any]]:
    """
    Render a PR + its merge-readiness verdict as Block Kit blocks.

    `pr` is a trimmed github_client PR dict; `status` is the dict from
    get_pr_merge_status. When status['blocking_reasons'] is empty the card
    shows Merge buttons; otherwise it lists what's blocking and omits them.
    """
    repo_number = _payload(repo=pr.get("_repo", ""), number=pr["number"])

    checks = status.get("checks_summary", {})
    blocking = status.get("blocking_reasons", [])
    ready = not blocking

    def tick(ok: bool) -> str:
        return "✅" if ok else "❌"

    readiness = [
        f"{tick(not status.get('draft'))} Not a draft",
        f"{tick(status.get('approved_count', 0) > 0)} "
        f"Approvals: {status.get('approved_count', 0)}",
        f"{tick(status.get('changes_requested_count', 0) == 0)} "
        f"Changes requested: {status.get('changes_requested_count', 0)}",
        f"{tick(status.get('checks_passed'))} "
        f"CI: {checks.get('success', 0)}✓ / {checks.get('failure', 0)}✗ / "
        f"{checks.get('pending', 0)}⏳",
    ]

    blocks: list[dict[str, Any]] = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"PR #{pr['number']} — {pr['title'][:130]}", "emoji": True}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f"`{pr.get('base')}` ← `{pr.get('head')}`  ·  by "
                           f"{pr.get('author', '?')}\n{pr.get('url', '')}")}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(readiness)}},
    ]

    if ready:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn",
                                     "text": ":white_check_mark: *Nothing blocking — ready to merge.*"}]})
        blocks.append({
            "type": "actions",
            "block_id": f"pr_merge_actions_{pr['number']}",
            "elements": [
                {"type": "button",
                 "text": {"type": "plain_text", "text": "🔀 Squash & merge", "emoji": True},
                 "style": "primary",
                 "action_id": "pr_merge_request",
                 "value": _payload(repo=pr.get("_repo", ""), number=pr["number"], method="squash")},
                {"type": "button",
                 "text": {"type": "plain_text", "text": "Merge commit", "emoji": True},
                 "action_id": "pr_merge_request",
                 "value": _payload(repo=pr.get("_repo", ""), number=pr["number"], method="merge")},
            ],
        })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "*:no_entry: Blocked:*\n"
                             + "\n".join(f"• {r}" for r in blocking)},
        })

    return blocks


def build_merge_confirm_card(pr_number: int, repo: str, method: str) -> list[dict[str, Any]]:
    """Second-step confirmation shown after a Merge button is clicked."""
    payload = _payload(repo=repo, number=pr_number, method=method)
    return [
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f":warning: *Merge PR #{pr_number} in `{repo}` "
                           f"using `{method}`?*\nThis writes to GitHub and cannot "
                           f"be undone from here.")}},
        {"type": "actions",
         "block_id": f"pr_merge_confirm_block_{pr_number}",
         "elements": [
             {"type": "button",
              "text": {"type": "plain_text", "text": "✅ Confirm merge", "emoji": True},
              "style": "primary", "action_id": "pr_merge_confirm", "value": payload},
             {"type": "button",
              "text": {"type": "plain_text", "text": "❌ Cancel", "emoji": True},
              "style": "danger", "action_id": "pr_merge_cancel", "value": payload},
         ]},
    ]


def build_result_blocks(text: str) -> list[dict[str, Any]]:
    """Terminal single-section card replacing the buttons after an action."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def fetch_merge_card(repo: str, number: int) -> tuple[str, list[dict]]:
    """
    Fetch PR + merge status from GitHub and return (fallback_text, blocks).
    Raises github_client.GitHubError on API failure.
    """
    pr = gh.get_pull_request(repo, number)
    pr["_repo"] = repo   # carry repo into the pure builder for button payloads
    status = gh.get_pr_merge_status(repo, number)
    fallback = f"PR #{number} in {repo}: {'ready to merge' if not status['blocking_reasons'] else 'blocked'}"
    return fallback, build_merge_card(pr, status)
