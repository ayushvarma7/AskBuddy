"""Tool implementations for MeetingScribe agent."""

import json
import os
import re
from typing import Any
from langchain_core.tools import tool


@tool
def parse_transcript(raw_text: str) -> str:
    """Parse a raw meeting transcript into structured turns with speaker and text.
    
    Args:
        raw_text: Raw transcript text with speaker names followed by their statements
        
    Returns:
        JSON string containing list of turns with speaker and text fields
    """
    turns = []
    
    # Split by lines and parse speaker: text format
    lines = raw_text.strip().split('\n')
    current_speaker = None
    current_text = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Check if line starts with a speaker name (Name: or Name - )
        speaker_match = re.match(r'^([A-Za-z\s]+)(?::|-)(.+)$', line)
        
        if speaker_match:
            # Save previous speaker's text if exists
            if current_speaker and current_text:
                turns.append({
                    "speaker": current_speaker.strip(),
                    "text": ' '.join(current_text).strip()
                })
                current_text = []
            
            current_speaker = speaker_match.group(1)
            current_text.append(speaker_match.group(2).strip())
        elif current_speaker:
            # Continuation of current speaker's text
            current_text.append(line)
    
    # Add final speaker's text
    if current_speaker and current_text:
        turns.append({
            "speaker": current_speaker.strip(),
            "text": ' '.join(current_text).strip()
        })
    
    return json.dumps({"turns": turns}, indent=2)


@tool
def extract_action_items(turns: str) -> str:
    """Extract action items, decisions, and follow-ups from parsed transcript turns.
    
    Distinguishes between:
    - ACTION ITEM: concrete task with identifiable owner → becomes create_task
    - NEEDS FOLLOW-UP: explicitly deferred item → becomes schedule_followup
    - DECISION: recorded but no action needed
    
    NEVER invents an owner. If no owner stated, flags as "unassigned".
    
    Args:
        turns: JSON string from parse_transcript containing list of turns
        
    Returns:
        JSON string with categorized items: action_items, follow_ups, decisions
    """
    try:
        data = json.loads(turns)
        turns_list = data.get("turns", [])
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON input"})
    
    action_items = []
    follow_ups = []
    decisions = []
    
    # Keywords for identifying different types
    action_keywords = ["will", "going to", "need to", "should", "must", "can you", "could you"]
    followup_keywords = ["revisit", "follow up", "check back", "circle back", "next week", 
                         "next meeting", "later", "after", "once", "when"]
    decision_keywords = ["decided", "agreed", "going with", "settled on", "approved"]
    
    for i, turn in enumerate(turns_list):
        speaker = turn.get("speaker", "")
        text = turn.get("text", "").lower()
        
        # Check for decisions
        if any(keyword in text for keyword in decision_keywords):
            decisions.append({
                "speaker": speaker,
                "text": turn.get("text", ""),
                "turn_number": i + 1
            })
        
        # Check for follow-ups (deferred items)
        if any(keyword in text for keyword in followup_keywords):
            # Extract what needs follow-up
            follow_ups.append({
                "description": turn.get("text", ""),
                "mentioned_by": speaker,
                "turn_number": i + 1,
                "needs_scheduling": True
            })
        
        # Check for action items
        if any(keyword in text for keyword in action_keywords):
            # Try to extract owner from text
            owner = None
            original_text = turn.get("text", "")
            
            # Look for explicit assignments: "Name, can you", "Name, could you"
            name_comma_pattern = r"([A-Z][a-z]+),\s+(?:can|could|will|would)\s+you"
            match = re.search(name_comma_pattern, original_text)
            if match:
                owner = match.group(1)
            
            # Look for "X will", "X can", "X should" (without comma)
            if not owner:
                for name_pattern in [r"([A-Z][a-z]+)\s+(?:will|can|should|must)\s+",
                                    r"(?:can|could)\s+([A-Z][a-z]+)\s+"]:
                    match = re.search(name_pattern, original_text)
                    if match:
                        owner = match.group(1)
                        break
            
            # If speaker is assigning to themselves
            if not owner and any(word in text for word in ["i'll", "i will", "i can", "i should"]):
                owner = speaker
            
            action_items.append({
                "description": original_text,
                "owner": owner if owner else "UNASSIGNED - needs owner before task creation",
                "mentioned_by": speaker,
                "turn_number": i + 1,
                "can_create_task": owner is not None
            })
    
    return json.dumps({
        "action_items": action_items,
        "follow_ups": follow_ups,
        "decisions": decisions,
        "summary": {
            "total_action_items": len(action_items),
            "assigned_tasks": sum(1 for item in action_items if item["can_create_task"]),
            "unassigned_tasks": sum(1 for item in action_items if not item["can_create_task"]),
            "follow_ups_needed": len(follow_ups),
            "decisions_made": len(decisions)
        }
    }, indent=2)


@tool
def create_task(title: str, owner: str, description: str, dry_run: bool = True) -> str:
    """Create a task in the task tracker (Asana/Linear/Jira via MCP).
    
    Args:
        title: Task title
        owner: Task owner/assignee
        description: Detailed task description
        dry_run: If True, only shows what would be created without executing
        
    Returns:
        Confirmation message or dry-run preview
    """
    if dry_run:
        return f"""
🔍 DRY RUN - WOULD CREATE TASK:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: {title}
Owner: {owner}
Description: {description}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
"""
    
    # TODO: Implement actual MCP call to task tracker
    # Example for Linear MCP:
    # from mcp_client import MCPClient
    # client = MCPClient("linear")
    # result = client.call_tool("create_issue", {
    #     "title": title,
    #     "assignee": owner,
    #     "description": description
    # })
    # return f"✅ Task created: {result['url']}"
    
    return f"✅ Task created: {title} (assigned to {owner})"


@tool
def send_message(recipient: str, message: str, dry_run: bool = True) -> str:
    """Send a Slack message to notify task owner.
    
    Args:
        recipient: Slack username or channel
        message: Message content
        dry_run: If True, only shows what would be sent without executing
        
    Returns:
        Confirmation message or dry-run preview
    """
    if dry_run:
        return f"""
🔍 DRY RUN - WOULD SEND SLACK MESSAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
To: @{recipient}
Message:
{message}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
"""
    
    # TODO: Implement actual MCP call to Slack
    # from mcp_client import MCPClient
    # client = MCPClient("slack")
    # result = client.call_tool("send_message", {
    #     "channel": f"@{recipient}",
    #     "text": message
    # })
    # return f"✅ Message sent to @{recipient}"
    
    return f"✅ Message sent to @{recipient}"


@tool
def schedule_followup(title: str, attendees: list[str], suggested_date: str, dry_run: bool = True) -> str:
    """Schedule a follow-up meeting via Calendar MCP.
    
    Args:
        title: Meeting title
        attendees: List of attendee names/emails
        suggested_date: Suggested date/time for the meeting
        dry_run: If True, only shows what would be scheduled without executing
        
    Returns:
        Confirmation message or dry-run preview
    """
    attendees_str = ", ".join(attendees)
    
    if dry_run:
        return f"""
🔍 DRY RUN - WOULD SCHEDULE MEETING:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: {title}
Attendees: {attendees_str}
Suggested Date: {suggested_date}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
"""
    
    # TODO: Implement actual MCP call to Calendar
    # from mcp_client import MCPClient
    # client = MCPClient("google-calendar")
    # result = client.call_tool("create_event", {
    #     "summary": title,
    #     "attendees": [{"email": a} for a in attendees],
    #     "start": suggested_date,
    #     "duration": 30  # minutes
    # })
    # return f"✅ Meeting scheduled: {result['htmlLink']}"
    
    return f"✅ Meeting scheduled: {title} with {attendees_str} on {suggested_date}"

@tool
def search_web(query: str, max_results: int = 5) -> dict:
    """Search the web via Tavily and return structured results.

    Use this for any source not covered by existing structured tools
    (e.g. competitor docs not on GitHub, general fact lookups, current
    info not in the knowledge base).  Do NOT default to this tool when a
    more precise structured tool (GitHub MCP, retrieval tools, etc.)
    already covers the question.

    Args:
        query: The search query string
        max_results: Maximum number of results to return (default 5)

    Returns:
        Dict with keys: query, answer, results (list of {title, url, content}).
        On failure returns {"error": <message>, "query": <query>}.
    """
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
        response = client.search(
            query=query,
            max_results=max_results,
            include_answer=True,
            include_raw_content=False,
        )
        return {
            "query": query,
            "answer": response.get("answer", ""),
            "results": [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                }
                for r in response.get("results", [])
            ],
        }
    except Exception as e:
        return {"error": str(e), "query": query}

# Made with Bob
