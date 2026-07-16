"""
Ask Buddy — CugaSupervisor with domain-specific sub-agents.

The supervisor routes each query to the right sub-agent:
  - hr_agent   — answers HR policy questions (PTO, benefits, leave, …)
  - it_agent   — answers IT security questions (passwords, VPN, MFA, …)

Each sub-agent has its own retrieval tool scoped to its corpus, plus a
shared post_slack_message tool to deliver the final answer.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from cuga import CugaAgent, CugaSupervisor
from dotenv import load_dotenv

from .retrieve import hr_retrieve, it_retrieve, hybrid_retrieve
from .db import get_github_login

load_dotenv()

log = logging.getLogger("ask_buddy.agent")

# ---------------------------------------------------------------------------
# System prompts — one per domain agent
# ---------------------------------------------------------------------------

_SHARED_RULES = """\

== PROCESS ==

1. Call your retrieval tool with the user's question.

2. Read every returned chunk carefully. Ask yourself:
   - Does at least one chunk directly answer the question?
   - Is the answer unambiguous given the retrieved text?
   If results are weak, partial, or off-topic, try ONE reformulated \
query before giving up (rephrase using synonyms or a narrower/broader scope).

3. If you have a solid, source-backed answer, respond EXACTLY as:

   <your answer text — clear, concise, in plain English>

   Source(s): <source_filename> — <section> (effective <YYYY-MM-DD>)

   Rules for the Sources line:
   - List ALL sources you drew on, one per line.
   - Use the exact source_filename, section, and effective_date from the chunk metadata.
   - If a chunk has no effective_date, omit the "(effective …)" part.
   - An answer with no Source(s) line is INVALID and must never be sent.
   - Do NOT fabricate or guess any filename, section name, or date not \
present in the retrieved chunk metadata.

4. If nothing retrieved actually answers the question, respond with \
EXACTLY this sentence and nothing else:

   No results found in our documents for that question — please \
reach out to the appropriate team for help.

== HARD RULES ==
- Never answer from general knowledge.
- Never blend a partial guess with the "No results found" message.
- Never print internal reasoning, tool call details, or raw JSON \
in the final Slack message.
- When multiple document versions exist, default to the version with the \
LATEST effective_date unless the user explicitly asks about a past version.
"""

HR_SYSTEM_PROMPT = """\
You are the HR Agent within Ask Buddy. You answer questions about \
HR topics ONLY: time off, leave, benefits, expenses, performance \
reviews, remote work, and workplace conduct. You have no other source \
of information — no general knowledge, no internet access, no \
assumptions beyond what is explicitly in the retrieved chunks.

Use hr_retrieve to search the HR document corpus.
""" + _SHARED_RULES

IT_SYSTEM_PROMPT = """\
You are the IT Security Agent within Ask Buddy. You answer questions \
about IT and information security topics ONLY: passwords, \
authentication, MFA, VPN, remote access, device management, endpoint \
security, data classification, data handling, and acceptable use of \
company IT resources. You have no other source of information.

Use it_retrieve to search the IT security document corpus.
""" + _SHARED_RULES

SCHEDULER_SYSTEM_PROMPT = """\
You are the Scheduler Agent within Ask Buddy. You manage recurring \
broadcast reminders posted to Slack channels — e.g. "remind \
#svl-interns-2026 to submit timecards every day at 9am PST".

You have three tools: create_reminder, list_reminders, cancel_reminder.

== CREATING A REMINDER ==

Extract from the user's request:
  - channel: the Slack channel name (strip any leading '#')
  - message: the exact text to broadcast
  - cron_expression: standard 5-field cron syntax (minute hour day month \
day-of-week), translated from the user's natural-language schedule
  - timezone: an IANA timezone name

Cron translation examples:
  "every day at 9:00 AM"        -> "0 9 * * *"
  "every Friday at 9:00 AM"     -> "0 9 * * 5"
  "every weekday at 2:30 PM"    -> "30 14 * * 1-5"
  "every Monday and Wednesday at 8 AM" -> "0 8 * * 1,3"

Timezone translation:
  "PST"/"PT"/"Pacific"  -> "America/Los_Angeles"
  "EST"/"ET"/"Eastern"  -> "America/New_York"
  "CST"/"CT"/"Central"  -> "America/Chicago"
  If no timezone is mentioned, default to "America/Los_Angeles".

If the user does not specify a day (e.g. just "at 9am"), default to \
every day ("* " in the day-of-week field) — but ALWAYS state your \
interpreted schedule back to the user in plain English in your reply \
(e.g. "I've scheduled this to repeat every day at 9:00 AM Pacific") so \
they can correct it if that's wrong.

Call create_reminder with the extracted channel, message, cron_expression, \
and timezone. Report the confirmation (including the reminder id) back \
to the user via post_slack_message.

== LISTING / CANCELLING ==

Use list_reminders to show active reminders (optionally filtered by \
channel). Use cancel_reminder with the reminder id to cancel one. \
Always confirm the result via post_slack_message.

== HARD RULES ==
- Never fabricate a channel, cron expression, or reminder id.
- If the schedule or channel is genuinely ambiguous and cannot be \
reasonably inferred, ask the user to clarify via post_slack_message \
instead of guessing.
"""

GIT_ISSUE_SYSTEM_PROMPT = """\
You are the Git Issue Agent within Ask Buddy. You handle GitHub issues.

You have READ tools (list_issues, get_issue, search_issues) and WRITE tools \
(create_issue, add_issue_comment, add_labels, remove_label, assign_users, \
unassign_users, set_issue_state).

== PROCESS ==
1. Identify the repository as 'owner/name'. If missing, ask via post_slack_message.
2. Call the right tool. Read the result.
3. Reply in plain English. For lists: issue number, title, state, one per line. \
For a single issue: title, state, author, labels, assignees, 1-2 sentence body \
summary, URL.

== WRITE ACTION RULES ==
- Only take a write action when the request is unambiguous about repo, issue \
number, and what to change. If anything is ambiguous, ask first.
- set_issue_state (close/reopen) requires the user to confirm — the confirmation \
happens automatically via Slack; just call the tool.
- Never claim an action succeeded unless the tool result confirms it. Relay \
errors plainly.
- If the user asks to assign something "to me", call resolve_my_github_login \
first to get their actual GitHub login — never guess or use their Slack \
display name as a GitHub login.

== HARD RULES ==
- Never fabricate issue numbers, titles, authors, or URLs.
- On tool error, relay a short clear message; do not guess.
- Deliver the final answer via post_slack_message.
"""

GIT_PR_SYSTEM_PROMPT = """\
You are the Git PR Agent within Ask Buddy. You handle GitHub pull requests.

You have READ tools (list_pull_requests, get_pull_request, get_pr_reviews, \
get_pr_checks, get_pr_files, get_pr_merge_status) and WRITE tools \
(create_pull_request, add_issue_comment, add_labels, remove_label, assign_users, \
unassign_users, request_pr_reviewers, set_issue_state, merge_pull_request).

== PROCESS ==
1. Identify the repository as 'owner/name'; ask if missing.
2. For "is PR #N ready to merge?" call get_pr_merge_status — it already \
combines review verdicts, CI checks, and draft status into one result with a \
blocking_reasons list. Only fall back to get_pull_request / get_pr_reviews / \
get_pr_checks individually if you need more detail.
3. For "what does PR #N change?" call get_pr_files and summarize by file count \
and the largest diffs.
4. Reply in plain English. For lists: PR number, title, author, draft flag. For \
a single PR: title, author, base←head, reviewers + verdicts, CI summary, URL.

== WRITE ACTION RULES ==
- Only take a write action when the request is unambiguous about repo, PR number, \
and what to change. If anything is ambiguous, ask first.
- merge_pull_request and set_issue_state require the user to confirm — the \
confirmation happens automatically via Slack; just call the tool.
- Never claim an action succeeded unless the tool result confirms it. Relay \
errors plainly.
- If the user asks to assign something "to me" or request "me" as a reviewer, \
call resolve_my_github_login first to get their actual GitHub login — never \
guess or use their Slack display name as a GitHub login.

== HARD RULES ==
- Never fabricate PR numbers, reviewers, verdicts, or check results.
- On tool error, relay a short clear message; do not guess.
- Deliver the final answer via post_slack_message.
"""

SUPERVISOR_PROMPT = """\
You are Ask Buddy, a workplace assistant for Acme Corp. You supervise \
specialist agents:

  • hr_agent — handles HR policy questions (PTO, vacation, sick leave, \
parental leave, benefits, health insurance, 401k, expense \
reimbursement, performance reviews, remote work, code of conduct, \
workplace behaviour, hiring, onboarding, offboarding, compensation).

  • it_agent — handles IT and security questions (passwords, MFA, VPN, \
firewalls, network access, device management, endpoint security, data \
classification, data handling, acceptable use, software licensing, \
encryption, helpdesk).

  • scheduler_agent — creates, lists, and cancels recurring broadcast \
reminders posted to Slack channels (e.g. "remind #channel to do X every \
day at 9am").

  • git_issue_agent — GitHub issue questions and write actions (list/summarize \
issues, labels, assignees, search; also comment, label, assign, open/close with \
confirmation) for a given 'owner/repo'.

  • git_pr_agent — GitHub pull request questions and write actions (list/summarize \
PRs, review status, CI checks; also comment, label, assign, request reviewers, \
merge/close with confirmation) for a given 'owner/repo'.

When a user asks a question or gives a command:
1. Determine which agent it belongs to and delegate to it.
2. If a question spans both hr_agent and it_agent, delegate to each and \
combine their answers.
3. Requests to create/list/cancel a reminder always go to scheduler_agent, \
never to hr_agent or it_agent.
4. If the request is clearly outside all three domains (e.g. personal \
advice, general trivia), respond directly with:
   No results found in our documents for that question — please \
reach out to the appropriate team for help.
5. GitHub *issue* questions go to git_issue_agent; GitHub *pull request* / PR / \
review / CI-check questions go to git_pr_agent. If a git question needs both \
(e.g. "summarize all activity on repo X"), delegate to both and combine. \
Write actions (comment, label, assign, close/merge) go to the respective git \
agent — high-risk actions (close/merge) will request Slack confirmation \
automatically before executing.

Always deliver the final answer via post_slack_message.
"""


# ---------------------------------------------------------------------------
# Few-shot injection (shared, opt-in)
# ---------------------------------------------------------------------------

def _default_repo_block() -> str:
    """Optional prompt snippet naming a default repo, so users don't have
    to type 'owner/repo' on every git question."""
    repo = os.environ.get("GITHUB_DEFAULT_REPO", "").strip()
    if not repo:
        return ""
    return (
        f"\n\n== DEFAULT REPOSITORY ==\n"
        f"If the user's question doesn't name a repo, assume '{repo}'. "
        f"Only ask which repo they mean if the question is clearly about a "
        f"different project even with that default in mind."
    )


def _fewshot_block() -> str:
    try:
        n = int(os.environ.get("ASK_BUDDY_FEWSHOT", "0") or "0")
    except ValueError:
        n = 0
    if n <= 0:
        return ""

    try:
        from .db import get_positive_examples
        examples = get_positive_examples(limit=n)
    except Exception:
        return ""

    if not examples:
        return ""

    blocks = []
    for ex in examples:
        blocks.append(
            f"Q: {ex['question']}\nA: {ex['answer_text']}"
        )
    joined = "\n\n".join(blocks)
    return (
        "\n\n== EXAMPLES OF GOOD ANSWERS (users rated these helpful) ==\n"
        "Match this style and Sources formatting:\n\n" + joined
    )


# ---------------------------------------------------------------------------
# Agent config tracking (for A/B feedback comparison)
# ---------------------------------------------------------------------------

def current_agent_config() -> str:
    setting = os.environ.get("AGENT_SETTING_CONFIG", "default")
    model = os.environ.get("MODEL_NAME", "default")
    return f"{setting}:{model}"


# ---------------------------------------------------------------------------
# Agent builders
# ---------------------------------------------------------------------------

def _build_post_tool(slack_post_fn: Callable[[str, str], None]):
    """Create the post_slack_message @tool closure."""
    from langchain_core.tools import tool

    @tool
    def post_slack_message(channel: str, text: str) -> str:
        """
        Post `text` to the specified Slack `channel` or DM thread.
        Always call this as the final step to deliver your answer to the user.
        """
        slack_post_fn(channel, text)
        return "Message posted."

    return post_slack_message


def build_hr_agent(slack_post_fn: Callable[[str, str], None]) -> CugaAgent:
    """Build the HR-domain sub-agent."""
    post_tool = _build_post_tool(slack_post_fn)
    return CugaAgent(
        tools=[hr_retrieve, post_tool],
        enable_knowledge=False,
        special_instructions=HR_SYSTEM_PROMPT + _fewshot_block(),
    )


def build_it_agent(slack_post_fn: Callable[[str, str], None]) -> CugaAgent:
    """Build the IT-security-domain sub-agent."""
    post_tool = _build_post_tool(slack_post_fn)
    return CugaAgent(
        tools=[it_retrieve, post_tool],
        enable_knowledge=False,
        special_instructions=IT_SYSTEM_PROMPT,
    )


def _default_resolve_channel(channel: str) -> tuple[str, str]:
    """Fallback channel resolver: treats the given string as already an ID."""
    name = channel.lstrip("#")
    return channel, name


def _build_reminder_tools(
    resolve_channel_fn: Callable[[str], tuple[str, str]],
    created_by: str | None,
):
    """
    Build the create_reminder / list_reminders / cancel_reminder @tools.

    resolve_channel_fn(channel_str) -> (channel_id, channel_name), used to
    turn a bare channel name from the user's message into the Slack channel
    ID the Web API needs to post there.
    """
    from langchain_core.tools import tool

    @tool
    def create_reminder(channel: str, message: str, cron_expression: str,
                        timezone: str = "America/Los_Angeles") -> dict:
        """
        Schedule a recurring reminder message to be posted in a Slack channel.

        channel: Slack channel name (e.g. 'svl-interns-2026') or ID.
        message: the exact text to broadcast each time the reminder fires.
        cron_expression: standard 5-field cron syntax, e.g. '0 9 * * *' for
            every day at 9:00 AM, or '0 9 * * 5' for every Friday at 9:00 AM.
        timezone: IANA timezone name, e.g. 'America/Los_Angeles' for PT.

        Returns a dict: {id, channel_name, message, cron_expression, timezone}.
        """
        from .db import insert_reminder
        from .scheduler import schedule_new_reminder

        channel_id, channel_name = resolve_channel_fn(channel)
        reminder_id = insert_reminder(
            channel_id=channel_id, channel_name=channel_name, message=message,
            cron_expression=cron_expression, timezone=timezone,
            created_by=created_by,
        )
        schedule_new_reminder(reminder_id, channel_id, message, cron_expression, timezone)
        return {
            "id": reminder_id,
            "channel_name": channel_name,
            "message": message,
            "cron_expression": cron_expression,
            "timezone": timezone,
        }

    @tool
    def list_reminders(channel: str = "") -> list[dict]:
        """
        List active reminders, optionally filtered to one Slack channel
        (name or ID). Returns each reminder's id, channel_name, message,
        cron_expression, and timezone.
        """
        from .db import list_reminders_for_channel

        channel_id = None
        if channel:
            channel_id, _ = resolve_channel_fn(channel)
        rows = list_reminders_for_channel(channel_id)
        return [
            {
                "id": r["id"],
                "channel_name": r["channel_name"],
                "message": r["message"],
                "cron_expression": r["cron_expression"],
                "timezone": r["timezone"],
            }
            for r in rows
        ]

    @tool
    def cancel_reminder(reminder_id: int) -> str:
        """Cancel an active reminder by its id (from create_reminder or list_reminders)."""
        from .db import deactivate_reminder
        from .scheduler import unschedule_reminder

        found = deactivate_reminder(reminder_id)
        if not found:
            return f"No active reminder with id {reminder_id} found."
        unschedule_reminder(reminder_id)
        return f"Reminder #{reminder_id} cancelled."

    return create_reminder, list_reminders, cancel_reminder


def _build_git_issue_tools():
    from langchain_core.tools import tool
    from . import github_client as gh

    @tool
    def list_issues(repo: str, state: str = "open") -> list[dict]:
        """List GitHub issues for 'owner/repo'. state: 'open'|'closed'|'all'.
        Returns number, title, state, author, labels, assignees, url. Read-only."""
        try:
            return gh.list_issues(repo, state=state)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    @tool
    def get_issue(repo: str, number: int) -> dict:
        """Get one GitHub issue by number from 'owner/repo', including a truncated
        body. Read-only."""
        try:
            return gh.get_issue(repo, number)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def search_issues(query: str) -> list[dict]:
        """Search issues/PRs with GitHub search syntax, e.g.
        'repo:acme/backend is:open label:bug'. Read-only."""
        try:
            return gh.search_issues(query)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    return list_issues, get_issue, search_issues


def _build_git_pr_tools():
    from langchain_core.tools import tool
    from . import github_client as gh

    @tool
    def list_pull_requests(repo: str, state: str = "open") -> list[dict]:
        """List GitHub pull requests for 'owner/repo'. Read-only."""
        try:
            return gh.list_pull_requests(repo, state=state)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    @tool
    def get_pull_request(repo: str, number: int) -> dict:
        """Get one PR by number from 'owner/repo'. Read-only."""
        try:
            return gh.get_pull_request(repo, number)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def get_pr_reviews(repo: str, number: int) -> list[dict]:
        """List reviews (reviewer + APPROVED/CHANGES_REQUESTED/COMMENTED) for a PR."""
        try:
            return gh.get_pr_reviews(repo, number)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    @tool
    def get_pr_checks(repo: str, number: int) -> dict:
        """Summarize CI check-runs (passed/failed/pending) for a PR's head commit."""
        try:
            return gh.get_pr_checks(repo, number)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def get_pr_files(repo: str, number: int) -> list[dict]:
        """List files changed in a PR: filename, status, additions, deletions. Read-only."""
        try:
            return gh.get_pr_files(repo, number)
        except gh.GitHubError as e:
            return [{"error": str(e)}]

    @tool
    def get_pr_merge_status(repo: str, number: int) -> dict:
        """One-call merge-readiness verdict: mergeable, draft, approvals,
        changes-requested count, CI pass/fail, and blocking_reasons list
        (empty = nothing blocking). Read-only — reports status, does not merge."""
        try:
            return gh.get_pr_merge_status(repo, number)
        except gh.GitHubError as e:
            return {"error": str(e)}

    return (list_pull_requests, get_pull_request, get_pr_reviews, get_pr_checks,
            get_pr_files, get_pr_merge_status)


def _build_git_write_tools():
    """Low-risk write tools: comment, label/remove-label, assign/unassign,
    request review, create issue/PR.
    Not approval-gated — easily reversible, low blast radius."""
    from langchain_core.tools import tool
    from . import github_client as gh

    @tool
    def add_issue_comment(repo: str, number: int, body: str) -> dict:
        """Post a comment on an issue or PR in 'owner/repo'."""
        try:
            return gh.add_issue_comment(repo, number, body)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def add_labels(repo: str, number: int, labels: list[str]) -> list[str]:
        """Add labels to an issue or PR. Labels must already exist on the repo."""
        try:
            return gh.add_labels(repo, number, labels)
        except gh.GitHubError as e:
            return [f"error: {e}"]

    @tool
    def remove_label(repo: str, number: int, label: str) -> dict:
        """Remove a single label from an issue or PR in 'owner/repo'."""
        try:
            return gh.remove_label(repo, number, label)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def assign_users(repo: str, number: int, assignees: list[str]) -> list[str]:
        """Assign GitHub users (by login) to an issue or PR."""
        try:
            return gh.assign_users(repo, number, assignees)
        except gh.GitHubError as e:
            return [f"error: {e}"]

    @tool
    def unassign_users(repo: str, number: int, assignees: list[str]) -> list[str]:
        """Remove GitHub users (by login) from an issue or PR's assignee list."""
        try:
            return gh.unassign_users(repo, number, assignees)
        except gh.GitHubError as e:
            return [f"error: {e}"]

    @tool
    def request_pr_reviewers(repo: str, number: int, reviewers: list[str]) -> list[str]:
        """Request GitHub users (by login) as reviewers on a PR."""
        try:
            return gh.request_pr_reviewers(repo, number, reviewers)
        except gh.GitHubError as e:
            return [f"error: {e}"]

    @tool
    def create_issue(repo: str, title: str, body: str = "",
                     labels: list[str] | None = None,
                     assignees: list[str] | None = None) -> dict:
        """Create a new GitHub issue in 'owner/repo'. Returns number, url, state."""
        try:
            return gh.create_issue(repo, title, body=body,
                                   labels=labels, assignees=assignees)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def create_pull_request(repo: str, title: str, head: str, base: str,
                            body: str = "", draft: bool = False) -> dict:
        """Open a new pull request from head -> base in 'owner/repo'.
        head: source branch (or 'fork:branch'). Returns trimmed PR dict."""
        try:
            return gh.create_pull_request(repo, title, head, base,
                                          body=body, draft=draft)
        except gh.GitHubError as e:
            return {"error": str(e)}

    return (add_issue_comment, add_labels, remove_label,
            assign_users, unassign_users, request_pr_reviewers,
            create_issue, create_pull_request)


def _build_git_dangerous_tools():
    """High-risk write tools: close/reopen/merge.
    MUST be approval-gated via agent.policies.add_tool_approval."""
    from langchain_core.tools import tool
    from . import github_client as gh

    @tool
    def set_issue_state(repo: str, number: int, state: str) -> dict:
        """Close or reopen an issue or PR. state: 'open' or 'closed'."""
        try:
            return gh.set_issue_state(repo, number, state)
        except gh.GitHubError as e:
            return {"error": str(e)}

    @tool
    def merge_pull_request(repo: str, number: int, merge_method: str = "merge") -> dict:
        """Merge a PR. merge_method: 'merge', 'squash', or 'rebase'."""
        try:
            return gh.merge_pull_request(repo, number, merge_method)
        except gh.GitHubError as e:
            return {"error": str(e)}

    return set_issue_state, merge_pull_request


def _build_git_identity_tools(slack_user_id: str | None):
    from langchain_core.tools import tool

    @tool
    def resolve_my_github_login() -> str:
        """Return the GitHub login linked to the Slack user who sent this
        message. Use this before assigning/requesting review 'to me'. If it
        returns an error, tell the user to run '/askbuddy link github
        <login>' first — do not guess a login."""
        if not slack_user_id:
            return "error: no Slack user context available for this request."
        login = get_github_login(slack_user_id)
        if not login:
            return (
                "error: this Slack user hasn't linked a GitHub account yet. "
                "Ask them to run '/askbuddy link github <their-login>'."
            )
        return login

    return (resolve_my_github_login,)


def build_git_issue_agent(slack_post_fn: Callable[[str, str], None],
                          user_id: str | None = None) -> CugaAgent:
    import asyncio
    post_tool = _build_post_tool(slack_post_fn)
    list_issues, get_issue, search_issues = _build_git_issue_tools()
    (add_comment, add_labels_tool, remove_label_tool,
     assign_tool, unassign_tool, _request_reviewers,
     create_issue_tool, _create_pr) = _build_git_write_tools()
    set_state_tool, _ = _build_git_dangerous_tools()
    (resolve_login,) = _build_git_identity_tools(user_id)
    agent = CugaAgent(
        tools=[list_issues, get_issue, search_issues,
               create_issue_tool, add_comment,
               add_labels_tool, remove_label_tool,
               assign_tool, unassign_tool,
               set_state_tool, resolve_login, post_tool],
        enable_knowledge=False,
        special_instructions=GIT_ISSUE_SYSTEM_PROMPT + _default_repo_block(),
    )
    try:
        asyncio.run(agent.policies.add_tool_approval(
            name="Approve issue state change",
            required_tools=["set_issue_state"],
            approval_message="This will close or reopen the issue on GitHub. Confirm?",
            policy_id="git_issue_state_approval",
        ))
    except Exception:
        log.debug("git_issue_state_approval policy already registered", exc_info=True)
    return agent


def build_git_pr_agent(slack_post_fn: Callable[[str, str], None],
                       user_id: str | None = None) -> CugaAgent:
    import asyncio
    post_tool = _build_post_tool(slack_post_fn)
    (list_prs, get_pr, get_reviews, get_checks,
     get_files, get_merge_status) = _build_git_pr_tools()
    (add_comment, add_labels_tool, remove_label_tool,
     assign_tool, unassign_tool, request_reviewers_tool,
     _create_issue, create_pr_tool) = _build_git_write_tools()
    set_state_tool, merge_tool = _build_git_dangerous_tools()
    (resolve_login,) = _build_git_identity_tools(user_id)
    agent = CugaAgent(
        tools=[list_prs, get_pr, get_reviews, get_checks, get_files, get_merge_status,
               create_pr_tool, add_comment,
               add_labels_tool, remove_label_tool,
               assign_tool, unassign_tool, request_reviewers_tool,
               set_state_tool, merge_tool, resolve_login, post_tool],
        enable_knowledge=False,
        special_instructions=GIT_PR_SYSTEM_PROMPT + _default_repo_block(),
    )
    try:
        asyncio.run(agent.policies.add_tool_approval(
            name="Approve PR merge",
            required_tools=["merge_pull_request"],
            approval_message="This will merge the pull request into its base branch. Confirm?",
            policy_id="git_pr_merge_approval",
        ))
    except Exception:
        log.debug("git_pr_merge_approval policy already registered", exc_info=True)
    try:
        asyncio.run(agent.policies.add_tool_approval(
            name="Approve PR state change",
            required_tools=["set_issue_state"],
            approval_message="This will close or reopen the pull request on GitHub. Confirm?",
            policy_id="git_pr_state_approval",
        ))
    except Exception:
        log.debug("git_pr_state_approval policy already registered", exc_info=True)
    return agent


def build_scheduler_agent(
    slack_post_fn: Callable[[str, str], None],
    resolve_channel_fn: Callable[[str], tuple[str, str]] | None = None,
    created_by: str | None = None,
) -> CugaAgent:
    """Build the reminder-scheduling sub-agent."""
    post_tool = _build_post_tool(slack_post_fn)
    create_reminder, list_reminders, cancel_reminder = _build_reminder_tools(
        resolve_channel_fn or _default_resolve_channel, created_by,
    )
    return CugaAgent(
        tools=[create_reminder, list_reminders, cancel_reminder, post_tool],
        enable_knowledge=False,
        special_instructions=SCHEDULER_SYSTEM_PROMPT,
    )


def build_supervisor(
    slack_post_fn: Callable[[str, str], None],
    resolve_channel_fn: Callable[[str], tuple[str, str]] | None = None,
    created_by: str | None = None,
) -> CugaSupervisor:
    """
    Build the CugaSupervisor that routes queries to domain sub-agents.

    The supervisor itself also has post_slack_message so it can deliver
    out-of-scope refusals or combined answers directly.
    """
    hr = build_hr_agent(slack_post_fn)
    it = build_it_agent(slack_post_fn)
    scheduler = build_scheduler_agent(slack_post_fn, resolve_channel_fn, created_by)
    git_issue = build_git_issue_agent(slack_post_fn, user_id=created_by)
    git_pr = build_git_pr_agent(slack_post_fn, user_id=created_by)

    supervisor = CugaSupervisor(
        agents={
            "hr_agent": hr,
            "it_agent": it,
            "scheduler_agent": scheduler,
            "git_issue_agent": git_issue,
            "git_pr_agent": git_pr,
        },
        special_instructions=SUPERVISOR_PROMPT,
    )
    return supervisor


def build_agent(slack_post_fn: Callable[[str, str], None]) -> CugaAgent:
    """
    Backward-compatible single-agent builder.

    Kept for tests and simple deployments that don't need multi-domain
    routing. Uses the original HR-scoped hybrid_retrieve.
    """
    post_tool = _build_post_tool(slack_post_fn)
    agent = CugaAgent(
        tools=[hybrid_retrieve, post_tool],
        enable_knowledge=False,
        special_instructions=HR_SYSTEM_PROMPT + _fewshot_block(),
    )
    return agent
