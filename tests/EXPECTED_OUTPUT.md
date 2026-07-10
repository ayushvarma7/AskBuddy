# Expected Dry-Run Output

This document shows the expected output when running `uv run python tests/test_tools_only.py` with the sample transcript.

## Test Results Summary

✅ **2 action items WITH owners** (Mike and Emma) → Tasks created + Slack messages sent (dry-run)
✅ **3 action items WITHOUT owners** → Correctly flagged as UNASSIGNED, NO tasks created
✅ **5 follow-up meetings** identified → Calendar invites shown (dry-run)
✅ **2 decisions** recorded → No tool calls triggered

## Detailed Output

### Action Items Extracted

1. **Mike** - API redesign proposal
   - Owner: Mike (correctly extracted from "Mike, can you take the lead")
   - Status: ✅ Can create task
   - Dry-run output: WOULD CREATE TASK + WOULD SEND SLACK MESSAGE

2. **Emma** - Investigate mobile crashes
   - Owner: Emma (correctly extracted from "Emma, could you investigate")
   - Status: ✅ Can create task
   - Dry-run output: WOULD CREATE TASK + WOULD SEND SLACK MESSAGE

3. **UNASSIGNED** - Database migration revisit
   - Owner: None (correctly identified - no explicit owner stated)
   - Status: ⚠️ SKIPPED - needs owner before task creation
   - Dry-run output: Warning message, NO task created

4. **UNASSIGNED** - Schedule follow-up meeting
   - Owner: None (question, not assignment)
   - Status: ⚠️ SKIPPED - needs owner before task creation
   - Dry-run output: Warning message, NO task created

5. **UNASSIGNED** - Update onboarding documentation
   - Owner: None ("someone should" - no specific person)
   - Status: ⚠️ SKIPPED - needs owner before task creation
   - Dry-run output: Warning message, NO task created

### Task Creation (Dry-Run)

```
🔍 DRY RUN - WOULD CREATE TASK:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Action: You're absolutely right. Mike, can you take the lead...
Owner: Mike
Description: From turn 3: You're absolutely right. Mike, can you take the lead on scoping out the API redesign? I'd like a proposal by next Friday.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.

🔍 DRY RUN - WOULD CREATE TASK:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Action: That's concerning. Emma, could you investigate the...
Owner: Emma
Description: From turn 6: That's concerning. Emma, could you investigate the root cause this week and report back?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
```

### Slack Notifications (Dry-Run)

```
🔍 DRY RUN - WOULD SEND SLACK MESSAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
To: @Mike
Message:
Hi Mike,

You were assigned an action item in today's meeting:

📋 You're absolutely right. Mike, can you take the lead on scoping out the API redesign? I'd like a proposal by next Friday.

This was mentioned by Sarah in the meeting.

Please let me know if you have any questions!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.

🔍 DRY RUN - WOULD SEND SLACK MESSAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
To: @Emma
Message:
Hi Emma,

You were assigned an action item in today's meeting:

📋 That's concerning. Emma, could you investigate the root cause this week and report back?

This was mentioned by Sarah in the meeting.

Please let me know if you have any questions!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
```

### Unassigned Items (Correctly Skipped)

```
⚠️  SKIPPED - NO OWNER ASSIGNED:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Description: One more thing - someone should update the onboarding documentation...
Status: UNASSIGNED - needs owner before task creation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This item needs an owner before a task can be created.
```

### Follow-up Meetings (Dry-Run)

```
🔍 DRY RUN - WOULD SCHEDULE MEETING:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Follow-up: Yes, let's do that. I'll send out a calendar invit...
Attendees: Sarah, Mike, Seb
Suggested Date: Next Tuesday 2pm
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  This is a preview. Set dry_run=False to execute.
```

## Verification Checklist

✅ **Owner extraction works correctly**
   - "Mike, can you" → Owner: Mike
   - "Emma, could you" → Owner: Emma
   - "someone should" → Owner: UNASSIGNED

✅ **Safety checks in place**
   - All tools default to dry_run=True
   - Unassigned items never call create_task
   - Clear preview messages before execution

✅ **Categorization correct**
   - Action items with owners → create_task + send_message
   - Action items without owners → flagged only
   - Follow-ups → schedule_followup
   - Decisions → recorded only (no tool calls)

✅ **No false positives**
   - Questions ("Should we schedule...") not treated as assignments
   - Vague statements ("someone should") correctly flagged
   - Decisions ("we decided") don't trigger task creation

## Next Steps

To move from dry-run to live execution:

1. Set up MCP credentials in `.env`
2. Update `tools.py` to use actual MCP clients
3. Set `dry_run=False` when calling tools
4. Tool Approval policy will still require confirmation before execution