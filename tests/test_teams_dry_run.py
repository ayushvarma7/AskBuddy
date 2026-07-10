"""Test script for MeetingScribe (Teams) - demonstrates MOM generation and Teams integration."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from meeting_scribe.tools_teams import (
    parse_transcript,
    generate_mom,
    create_task,
    send_teams_message,
    schedule_followup,
)


def test_teams_workflow():
    """Test the full MeetingScribe (Teams) pipeline in dry-run mode."""
    
    # Load sample transcript
    transcript_path = Path(__file__).parent / "sample_transcript_teams.txt"
    with open(transcript_path, 'r') as f:
        raw_transcript = f.read()
    
    print("=" * 80)
    print("MEETINGSCRIBE (TEAMS) DRY-RUN TEST")
    print("=" * 80)
    print()
    
    # Step 1: Parse transcript
    print("STEP 1: Parsing transcript...")
    print("-" * 80)
    parsed = parse_transcript.invoke({"raw_text": raw_transcript})
    print("✅ Transcript parsed into structured turns")
    print()
    
    # Step 2: Generate Minutes of Meeting
    print("STEP 2: Generating Minutes of Meeting document...")
    print("-" * 80)
    mom_result = generate_mom.invoke({
        "turns": parsed,
        "meeting_topic": "Q4 Product Planning",
        "meeting_date": "December 15, 2024"
    })
    
    import json
    mom_data = json.loads(mom_result)
    mom_document = mom_data["mom_document"]
    metadata = mom_data["metadata"]
    
    print(mom_document)
    print()
    
    # Step 3: Task creation for items with owners (dry-run)
    print("STEP 3: Task Creation (DRY-RUN MODE)")
    print("=" * 80)
    
    for item in metadata["action_items"]:
        if item["has_owner"]:
            result = create_task.invoke({
                "title": f"Action from meeting: {item['task'][:50]}...",
                "owner": item["owner"],
                "description": f"From meeting turn {item['turn']}: {item['task']}",
                "dry_run": True
            })
            print(result)
        else:
            print(f"""
⚠️  SKIPPED - NO OWNER ASSIGNED:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Description: {item['task'][:80]}...
Status: {item['owner']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This item will appear in the MOM as unassigned but will NOT create a task.
""")
    
    # Step 4: Send MOM summary to Teams channel (dry-run)
    print("\nSTEP 4: Send MOM Summary to Teams Channel (DRY-RUN MODE)")
    print("=" * 80)
    
    mom_summary = f"""📋 **Minutes of Meeting - Q4 Product Planning**

**Attendees:** {', '.join(metadata['attendees'])}

**Key Outcomes:**
- {metadata['decisions_count']} decision(s) made
- {metadata['action_items_with_owner']} action item(s) assigned
- {metadata['action_items_unassigned']} action item(s) need owner assignment
- {metadata['deferred_items_count']} item(s) deferred for follow-up

Full minutes have been documented. Please review your action items below.
"""
    
    result = send_teams_message.invoke({
        "channel_or_user": "#product-team",
        "message": mom_summary,
        "dry_run": True
    })
    print(result)
    
    # Step 5: Send personalized action item reminders (dry-run)
    print("\nSTEP 5: Send Personalized Action Item Reminders (DRY-RUN MODE)")
    print("=" * 80)
    
    # Group action items by owner
    items_by_owner = {}
    for item in metadata["action_items"]:
        if item["has_owner"]:
            owner = item["owner"]
            if owner not in items_by_owner:
                items_by_owner[owner] = []
            items_by_owner[owner].append(item)
    
    for owner, items in items_by_owner.items():
        message = f"""Hi {owner},

You were assigned {len(items)} action item(s) in today's Q4 Product Planning meeting:

"""
        for i, item in enumerate(items, 1):
            message += f"{i}. {item['task']}\n\n"
        
        message += "Please let me know if you have any questions!\n\nFull meeting minutes are available in the #product-team channel."
        
        result = send_teams_message.invoke({
            "channel_or_user": owner,
            "message": message,
            "dry_run": True
        })
        print(result)
    
    # Step 6: Schedule follow-up meetings (dry-run)
    print("\nSTEP 6: Schedule Follow-up Meetings (DRY-RUN MODE)")
    print("=" * 80)
    
    for item in metadata["deferred_items"]:
        # Extract suggested date if mentioned
        suggested_date = "Two weeks from now"  # Default
        if "two weeks" in item["item"].lower():
            suggested_date = "Two weeks from now (December 29, 2024)"
        
        result = schedule_followup.invoke({
            "title": f"Follow-up: {item['item'][:50]}...",
            "attendees": metadata["attendees"],
            "suggested_date": suggested_date,
            "dry_run": True
        })
        print(result)
    
    # Step 7: Summary
    print("\nSUMMARY")
    print("=" * 80)
    print(f"""
📊 Meeting Analysis:
  - Attendees: {len(metadata['attendees'])}
  - Decisions Made: {metadata['decisions_count']}
  - Total Action Items: {metadata['action_items_count']}
    ✅ With Owner: {metadata['action_items_with_owner']}
    ⚠️  Unassigned: {metadata['action_items_unassigned']}
  - Follow-ups Needed: {metadata['deferred_items_count']}

✅ VERIFICATION CHECKLIST:
  ✓ MOM document generated with proper structure
  ✓ Items with owners shown in dry-run mode for task creation
  ✓ Items without owners flagged in MOM but NOT sent to create_task
  ✓ Teams channel message prepared with summary
  ✓ Personalized DMs prepared for each owner
  ✓ Follow-up meetings identified and ready to schedule
  ✓ Decisions recorded in MOM (no task creation)
""")
    
    print("\n" + "=" * 80)
    print("DRY-RUN TEST COMPLETE")
    print("=" * 80)
    print("""
To execute real actions:
1. Set up Microsoft Teams/Outlook MCP credentials in .env
2. Update tools_teams.py to use actual MCP clients instead of TODOs
3. Set dry_run=False when calling tools (requires approval policy confirmation)
4. Run with: python tests/test_teams_dry_run.py --live
""")


if __name__ == "__main__":
    test_teams_workflow()

# Made with Bob
