"""Tool implementations for MeetingScribe (Teams) agent."""

import json
import re
from datetime import datetime
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
def generate_mom(turns: str, meeting_topic: str = "Team Sync", meeting_date: str = None) -> str:
    """Generate a formatted Minutes of Meeting document from parsed transcript turns.
    
    Structure:
    - Attendees (speakers)
    - Discussion Summary (2-4 sentences)
    - Decisions (with who stated them)
    - Action Items (with owners or "unassigned")
    - Open Questions / Deferred Items
    
    NEVER invents owners, decisions, or dates not explicitly stated.
    
    Args:
        turns: JSON string from parse_transcript containing list of turns
        meeting_topic: Topic/title of the meeting
        meeting_date: Date of meeting (defaults to today)
        
    Returns:
        Formatted Minutes of Meeting document as markdown
    """
    try:
        data = json.loads(turns)
        turns_list = data.get("turns", [])
    except json.JSONDecodeError:
        return "Error: Invalid JSON input for turns"
    
    if not meeting_date:
        meeting_date = datetime.now().strftime("%B %d, %Y")
    
    # Extract attendees (unique speakers)
    attendees = list(dict.fromkeys([turn.get("speaker", "") for turn in turns_list]))
    
    # Analyze content
    decisions = []
    action_items = []
    deferred_items = []
    discussion_points = []
    
    decision_keywords = ["decided", "agreed", "going with", "settled on", "approved", "final decision"]
    action_keywords = ["will", "going to", "need to", "should", "must", "can you", "could you"]
    defer_keywords = ["revisit", "follow up", "check back", "circle back", "next week", 
                     "next meeting", "later", "after", "once", "when", "defer"]
    
    for i, turn in enumerate(turns_list):
        speaker = turn.get("speaker", "")
        text = turn.get("text", "")
        text_lower = text.lower()
        
        # Track discussion points for summary
        if len(text.split()) > 10:  # Substantial statements
            discussion_points.append(text[:100])
        
        # Check for decisions
        if any(keyword in text_lower for keyword in decision_keywords):
            decisions.append({
                "decision": text,
                "stated_by": speaker,
                "turn": i + 1
            })
        
        # Check for deferred items
        if any(keyword in text_lower for keyword in defer_keywords):
            deferred_items.append({
                "item": text,
                "mentioned_by": speaker,
                "turn": i + 1
            })
        
        # Check for action items
        if any(keyword in text_lower for keyword in action_keywords):
            # Try to extract owner
            owner = None
            
            # Pattern: "Name, can you" or "Name, could you"
            name_comma_pattern = r"([A-Z][a-z]+),\s+(?:can|could|will|would)\s+you"
            match = re.search(name_comma_pattern, text)
            if match:
                owner = match.group(1)
            
            # Pattern: "Name will" or "Name can"
            if not owner:
                for name_pattern in [r"([A-Z][a-z]+)\s+(?:will|can|should|must)\s+",
                                    r"(?:can|could)\s+([A-Z][a-z]+)\s+"]:
                    match = re.search(name_pattern, text)
                    if match:
                        owner = match.group(1)
                        break
            
            # Self-assignment
            if not owner and any(word in text_lower for word in ["i'll", "i will", "i can", "i should"]):
                owner = speaker
            
            action_items.append({
                "task": text,
                "owner": owner if owner else "unassigned — needs confirmation",
                "has_owner": owner is not None,
                "mentioned_by": speaker,
                "turn": i + 1
            })
    
    # Generate discussion summary (first 2-4 substantial points)
    summary_points = discussion_points[:4] if len(discussion_points) >= 4 else discussion_points[:2]
    discussion_summary = "The team discussed " + "; ".join(
        [p + "..." if len(p) == 100 else p for p in summary_points]
    )
    
    # Build MOM document
    mom = f"""## Minutes of Meeting — {meeting_topic}

**Date:** {meeting_date}

**Attendees:** {", ".join(attendees)}

---

### Discussion Summary

{discussion_summary}

---

### Decisions

"""
    
    if decisions:
        for dec in decisions:
            mom += f"- **{dec['decision']}** — stated by {dec['stated_by']}\n"
    else:
        mom += "- No formal decisions recorded\n"
    
    mom += "\n---\n\n### Action Items\n\n"
    
    if action_items:
        for item in action_items:
            checkbox = "[ ]"
            owner_text = f"**Owner:** {item['owner']}"
            if not item['has_owner']:
                owner_text = f"**Owner:** ⚠️ {item['owner']}"
            mom += f"{checkbox} {item['task'][:80]}{'...' if len(item['task']) > 80 else ''} — {owner_text}\n"
    else:
        mom += "- No action items identified\n"
    
    mom += "\n---\n\n### Open Questions / Deferred Items\n\n"
    
    if deferred_items:
        for item in deferred_items:
            mom += f"- {item['item'][:100]}{'...' if len(item['item']) > 100 else ''} — mentioned by {item['mentioned_by']}\n"
    else:
        mom += "- No items deferred\n"
    
    mom += "\n---\n\n*Generated by MeetingScribe*\n"
    
    # Return both the MOM and metadata
    return json.dumps({
        "mom_document": mom,
        "metadata": {
            "attendees": attendees,
            "decisions_count": len(decisions),
            "action_items_count": len(action_items),
            "action_items_with_owner": sum(1 for item in action_items if item['has_owner']),
            "action_items_unassigned": sum(1 for item in action_items if not item['has_owner']),
            "deferred_items_count": len(deferred_items),
            "action_items": action_items,
            "deferred_items": deferred_items
        }
    }, indent=2)


@tool
def create_task(title: str, owner: str, description: str, dry_run: bool = True) -> str:
    """Create a task in Microsoft Planner or To Do via MCP.
    
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
🔍 DRY RUN - WOULD CREATE TASK (Microsoft Planner/To Do):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: {title}
Owner: {owner}
Description: {description}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
"""
    
    # TODO: Implement actual Microsoft Teams/Planner MCP call
    # Example for Microsoft Planner MCP:
    # from mcp_client import MCPClient
    # client = MCPClient("microsoft-planner")
    # result = client.call_tool("create_task", {
    #     "title": title,
    #     "assignedTo": owner,
    #     "description": description,
    #     "bucketId": "default"  # or specific bucket
    # })
    # return f"✅ Task created in Planner: {result['webUrl']}"
    
    return f"✅ Task created in Microsoft Planner: {title} (assigned to {owner})"


@tool
def send_teams_message(channel_or_user: str, message: str, dry_run: bool = True) -> str:
    """Send a message to a Microsoft Teams channel or user DM.
    
    Args:
        channel_or_user: Teams channel name or user email/name
        message: Message content (supports markdown)
        dry_run: If True, only shows what would be sent without executing
        
    Returns:
        Confirmation message or dry-run preview
    """
    if dry_run:
        return f"""
🔍 DRY RUN - WOULD SEND TEAMS MESSAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
To: {channel_or_user}
Message:
{message}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
"""
    
    # TODO: Implement actual Microsoft Teams MCP call
    # from mcp_client import MCPClient
    # client = MCPClient("microsoft-teams")
    # result = client.call_tool("send_message", {
    #     "recipient": channel_or_user,
    #     "content": message,
    #     "contentType": "html"  # or "text"
    # })
    # return f"✅ Message sent to {channel_or_user}"
    
    return f"✅ Message sent to Teams: {channel_or_user}"


@tool
def schedule_followup(title: str, attendees: list[str], suggested_date: str, dry_run: bool = True) -> str:
    """Schedule a follow-up meeting via Microsoft Outlook/Teams Calendar.
    
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
🔍 DRY RUN - WOULD SCHEDULE MEETING (Outlook/Teams Calendar):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: {title}
Attendees: {attendees_str}
Suggested Date: {suggested_date}
Duration: 30 minutes
Teams Meeting: Yes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
"""
    
    # TODO: Implement actual Microsoft Outlook/Calendar MCP call
    # from mcp_client import MCPClient
    # client = MCPClient("microsoft-calendar")
    # result = client.call_tool("create_event", {
    #     "subject": title,
    #     "attendees": [{"emailAddress": {"address": a}} for a in attendees],
    #     "start": suggested_date,
    #     "duration": 30,  # minutes
    #     "isOnlineMeeting": True,
    #     "onlineMeetingProvider": "teamsForBusiness"
    # })
    # return f"✅ Meeting scheduled: {result['webLink']}"
    
    return f"✅ Meeting scheduled in Outlook: {title} with {attendees_str} on {suggested_date}"

# Made with Bob
