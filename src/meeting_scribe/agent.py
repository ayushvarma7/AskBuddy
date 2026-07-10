"""MeetingScribe CUGA agent configuration."""

import asyncio
from cuga import CugaAgent
from .tools import (
    parse_transcript,
    extract_action_items,
    create_task,
    send_message,
    schedule_followup,
    search_web,
)


SYSTEM_PROMPT = """You are MeetingScribe, an agent that extracts and executes meeting outcomes.

CORE PRINCIPLES:
1. Extract ONLY what was explicitly said in the transcript
2. NEVER infer commitments, owners, or urgency that weren't stated
3. A suggestion that wasn't agreed to is NOT a decision
4. A decision without a named owner is NOT a task - flag it as needing an owner
5. Before executing any action, state clearly what you're about to do and why,
   sourced to the specific line in the transcript that justifies it

WORKFLOW:
1. Parse the raw transcript into structured turns
2. Extract action items (with owners), decisions, and follow-ups
3. For each action item WITH an owner: create_task + send_message to notify them
4. For items needing follow-up meetings: schedule_followup
5. For decisions: just record them, no action needed

TOOL SELECTION — search_web:
- Use search_web ONLY when the question cannot be answered by the structured tools
  already available (parse_transcript, extract_action_items, create_task,
  send_message, schedule_followup, GitHub MCP, retrieval tools, knowledge base).
- Typical cases: looking up a library's latest release version, checking external
  documentation (e.g. Databricks Mosaic, DeepEval), or any current fact not in
  the knowledge base.
- Do NOT fall back to search_web for transcript content, task creation, or
  anything the structured tools already handle — that wastes a round-trip and
  risks hallucination from low-quality web snippets.

SAFETY:
- All tools run in dry_run=True mode by default
- You MUST get explicit approval before setting dry_run=False
- If extract_action_items finds >8 action items, pause and ask for confirmation
- Never create tasks for unassigned items - flag them instead
"""


async def create_meeting_scribe_agent() -> CugaAgent:
    """Create and configure the MeetingScribe agent with tools and policies.
    
    Returns:
        Configured CugaAgent instance
    """
    # Create agent with tools
    agent = CugaAgent(
        tools=[
            parse_transcript,
            extract_action_items,
            create_task,
            send_message,
            schedule_followup,
            search_web,
        ],
        enable_knowledge=False,  # Don't need RAG for this use case
    )
    
    # Add Tool Approval policy for execution safety
    await agent.policies.add_tool_approval(
        name="Require Approval for Task Execution",
        description="Require explicit confirmation before creating tasks, sending messages, or scheduling meetings",
        required_tools=["create_task", "send_message", "schedule_followup"],
        approval_message="""
⚠️  APPROVAL REQUIRED

This action will execute a real operation (create task/send message/schedule meeting).
Please review the details above and confirm you want to proceed.

All tools are in dry_run=True mode by default for safety.
""",
        show_code_preview=True,
        auto_approve_after=None,  # Never auto-approve
        priority=100,
    )
    
    # Add Intent Guard for unusually high action item counts
    await agent.policies.add_intent_guard(
        name="High Action Item Count Guard",
        description="Flag when extract_action_items produces >8 items, likely a parsing issue",
        keywords=["extract_action_items", "action items", "tasks"],
        response="""
⚠️  UNUSUALLY HIGH ACTION ITEM COUNT DETECTED

The transcript appears to have generated more than 8 action items, which is unusually 
high for a typical meeting. This may indicate:
- A parsing issue (multiple items incorrectly merged)
- An unusually dense/long meeting
- Items that should be decisions or follow-ups, not tasks

Please review the extracted items carefully before proceeding with task creation.
Would you like to continue, or should I re-analyze the transcript?
""",
        priority=90,
        allow_override=True,  # User can proceed after review
    )
    
    return agent


async def process_transcript(transcript: str, agent: CugaAgent = None, thread_id: str = "default") -> dict:
    """Process a meeting transcript end-to-end.
    
    Args:
        transcript: Raw meeting transcript text
        agent: Optional pre-configured agent (creates new one if not provided)
        thread_id: Thread ID for conversation isolation
        
    Returns:
        Dictionary with processing results
    """
    if agent is None:
        agent = await create_meeting_scribe_agent()
    
    try:
        # Invoke agent with the transcript
        result = await agent.invoke(
            f"""Process this meeting transcript:

{transcript}

Please:
1. Parse the transcript into structured turns
2. Extract action items, decisions, and follow-ups
3. Show me what would be executed (dry-run mode)
4. Wait for my approval before executing any real actions
""",
            thread_id=thread_id,
        )
        
        return {
            "success": True,
            "answer": result.answer,
            "metadata": result.metadata,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
    finally:
        if agent:
            await agent.aclose()


if __name__ == "__main__":
    # Example usage
    sample_transcript = """
Sarah: Good morning everyone. Let's start with the Q4 roadmap discussion.

Mike: I think we should prioritize the API redesign. It's been on the backlog for months.

Sarah: Agreed. Mike, can you take the lead on that?

Mike: Yes, I'll draft a proposal by next Friday.

Emma: What about the mobile app performance issues? Users are complaining.

Sarah: Good point. Emma, could you investigate the root cause this week?

Emma: Sure, I'll look into it.

Mike: We should also revisit the database migration plan once Seb confirms the infrastructure is ready.

Sarah: Right, let's schedule a follow-up meeting for that next week. I'll send out a calendar invite.

Emma: Sounds good. One more thing - we decided to go with PostgreSQL instead of MySQL, correct?

Sarah: Yes, that's the final decision. Let's document that.
"""
    
    async def main():
        result = await process_transcript(sample_transcript)
        print(result["answer"])
    
    asyncio.run(main())

# Made with Bob
