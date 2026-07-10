---
description: Prevents task creation or personalized messages for action items without explicit owners
enabled: true
id: guard_no_owner_assignment
name: No Owner Assignment Guard
priority: 95
triggers:
  natural_language:
  - create task without owner
  - send message to unassigned
  - no owner specified
  target: intent
  threshold: 0.7
type: intent_guard
intent_examples:
- Creating a task for an unassigned action item
- Sending a personalized message for an item with no owner
- Assigning a task to "someone" or "unassigned"
- Creating a task when owner is marked as "needs confirmation"
- Messaging an action item that has no explicit owner
response:
  response_type: natural_language
  content: |
    ⚠️  CANNOT CREATE TASK OR SEND MESSAGE - NO OWNER ASSIGNED
    
    This action item does not have an explicitly stated owner in the transcript.
    
    POLICY: Action items without named owners must NOT trigger:
    - create_task calls
    - Personalized send_teams_message calls
    
    Instead, these items should:
    - Appear in the Minutes of Meeting document as "unassigned — needs confirmation"
    - Be mentioned in the general Teams channel summary
    - NOT have individual tasks or DMs created
    
    If you need to assign this task, please:
    1. Confirm the owner with the meeting participants
    2. Update the MOM document with the confirmed owner
    3. Then create the task and send the notification
    
    Do not guess or invent an owner. Mark it as unassigned in the MOM.
allow_override: false
---

# No Owner Assignment Guard

## Purpose
Enforces the core safety principle: **never create tasks or send personalized messages for action items without explicit owners**. This prevents:
- Accidentally assigning work to the wrong person
- Creating orphaned tasks that nobody owns
- Sending confusing DMs about unassigned work
- Guessing or inventing owners not stated in the transcript

## Detection Logic
Triggers when the agent attempts to:
- Call `create_task` with an owner marked as "unassigned" or "needs confirmation"
- Call `send_teams_message` with a personalized action item reminder for an unassigned item
- Create any task where the owner was not explicitly named in the transcript

## Correct Handling
Action items without owners should:
1. ✅ Appear in the MOM document under "Action Items" with "⚠️ unassigned — needs confirmation"
2. ✅ Be mentioned in the general Teams channel summary ("1 action item needs owner assignment")
3. ❌ NOT trigger `create_task` calls
4. ❌ NOT trigger personalized `send_teams_message` DMs

## Examples

### ❌ BLOCKED (Correct)
```
Transcript: "Someone should update the documentation."
Agent attempts: create_task(title="Update docs", owner="unassigned")
Guard blocks: No explicit owner stated
```

### ✅ ALLOWED (Correct)
```
Transcript: "David, can you update the documentation?"
Agent attempts: create_task(title="Update docs", owner="David")
Guard allows: Owner explicitly stated
```

### ✅ ALLOWED (Correct)
```
Transcript: "Someone should update the documentation."
Agent: Adds to MOM as "[ ] Update documentation — Owner: ⚠️ unassigned — needs confirmation"
Guard allows: No task creation attempted, just MOM documentation
```

## Priority
Set to 95 (very high) to ensure it's checked before tool approval policies (100) but after the high action count guard (90).

## Override
This policy cannot be overridden. If an action item has no owner, it must be flagged in the MOM and brought up for assignment, not automatically created.