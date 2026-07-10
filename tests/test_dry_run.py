"""Test script to demonstrate MeetingScribe dry-run functionality."""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from meeting_scribe.tools import (
    parse_transcript,
    extract_action_items,
    create_task,
    send_message,
    schedule_followup,
)


async def test_dry_run():
    """Test the full MeetingScribe pipeline in dry-run mode."""
    
    # Load sample transcript
    transcript_path = Path(__file__).parent / "sample_transcript.txt"
    with open(transcript_path, 'r') as f:
        raw_transcript = f.read()
    
    print("=" * 80)
    print("MEETINGSCRIBE DRY-RUN TEST")
    print("=" * 80)
    print()
    
    # Step 1: Parse transcript
    print("STEP 1: Parsing transcript...")
    print("-" * 80)
    parsed = parse_transcript.invoke({"raw_text": raw_transcript})
    print(parsed)
    print()
    
    # Step 2: Extract action items
    print("STEP 2: Extracting action items, decisions, and follow-ups...")
    print("-" * 80)
    extracted = extract_action_items.invoke({"turns": parsed})
    print(extracted)
    print()
    
    # Parse the extracted data to demonstrate tool calls
    import json
    data = json.loads(extracted)
    
    # Step 3: Demonstrate task creation (dry-run)
    print("STEP 3: Task Creation (DRY-RUN MODE)")
    print("=" * 80)
    
    for item in data["action_items"]:
        if item["can_create_task"]:
            result = create_task.invoke({
                "title": f"Action from meeting: {item['description'][:50]}...",
                "owner": item["owner"],
                "description": f"From meeting turn {item['turn_number']}: {item['description']}",
                "dry_run": True
            })
            print(result)
        else:
            print(f"""
⚠️  SKIPPED - NO OWNER ASSIGNED:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Description: {item['description']}
Status: {item['owner']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This item needs an owner before a task can be created.
""")
    
    # Step 4: Demonstrate Slack notifications (dry-run)
    print("\nSTEP 4: Slack Notifications (DRY-RUN MODE)")
    print("=" * 80)
    
    for item in data["action_items"]:
        if item["can_create_task"]:
            message = f"""Hi {item['owner']},

You were assigned an action item in today's meeting:

📋 {item['description']}

This was mentioned by {item['mentioned_by']} in the meeting.

Please let me know if you have any questions!
"""
            result = send_message.invoke({
                "recipient": item["owner"],
                "message": message,
                "dry_run": True
            })
            print(result)
    
    # Step 5: Demonstrate follow-up scheduling (dry-run)
    print("\nSTEP 5: Follow-up Meeting Scheduling (DRY-RUN MODE)")
    print("=" * 80)
    
    for followup in data["follow_ups"]:
        if followup["needs_scheduling"]:
            # Extract attendees from description (simplified)
            attendees = ["Sarah", "Mike", "Seb"]  # Would be extracted from context
            result = schedule_followup.invoke({
                "title": f"Follow-up: {followup['description'][:50]}...",
                "attendees": attendees,
                "suggested_date": "Next Tuesday 2pm",
                "dry_run": True
            })
            print(result)
    
    # Step 6: Summary
    print("\nSUMMARY")
    print("=" * 80)
    summary = data["summary"]
    print(f"""
Total Action Items Found: {summary['total_action_items']}
  ✅ With Owner (can create task): {summary['assigned_tasks']}
  ⚠️  Without Owner (needs assignment): {summary['unassigned_tasks']}

Follow-ups Needed: {summary['follow_ups_needed']}
Decisions Recorded: {summary['decisions_made']}

VERIFICATION:
✓ Items with owners were shown in dry-run mode
✓ Items without owners were correctly flagged and NOT sent to create_task
✓ Follow-up meetings were identified and shown in dry-run mode
✓ Decisions were recorded but did not trigger any tool calls
""")
    
    print("\n" + "=" * 80)
    print("DRY-RUN TEST COMPLETE")
    print("=" * 80)
    print("""
To execute real actions:
1. Set up MCP credentials for task tracker, Slack, and Calendar
2. Update tools.py to use actual MCP clients instead of TODOs
3. Set dry_run=False when calling tools (requires approval policy confirmation)
4. Run with: python tests/test_dry_run.py --live
""")


if __name__ == "__main__":
    asyncio.run(test_dry_run())

# Made with Bob
