---
description: Require approval for sensitive operations that create tasks, send messages, or schedule meetings
enabled: true
id: approval_task_execution
name: Task Execution Approval Policy
priority: 100
triggers:
  tool_match:
  - create_task
  - send_message
  - schedule_followup
  target: tools
type: tool_approval
required_tools:
- create_task
- send_message
- schedule_followup
approval_message: |
  ⚠️  APPROVAL REQUIRED FOR TASK EXECUTION
  
  This action will execute a real operation that affects external systems:
  - create_task: Creates a task in your task tracker (Asana/Linear/Jira)
  - send_message: Sends a Slack message to a team member
  - schedule_followup: Creates a calendar event
  
  Please review the details above carefully before approving.
  
  SAFETY NOTE: All tools run in dry_run=True mode by default. You must explicitly
  set dry_run=False to execute real actions.
show_code_preview: true
auto_approve_after: null
---

# Task Execution Approval Policy

## Purpose
This policy ensures that MeetingScribe never creates tasks, sends messages, or schedules meetings without explicit human approval. This prevents:
- Accidentally creating duplicate tasks
- Sending messages to the wrong people
- Scheduling meetings at inappropriate times
- Acting on misinterpreted transcript content

## Scope
Applies to all execution tools:
- `create_task`: Task tracker operations
- `send_message`: Slack notifications
- `schedule_followup`: Calendar scheduling

## Workflow
1. Agent analyzes transcript and identifies actions
2. Agent shows dry-run preview of what would be executed
3. Human reviews and approves/rejects each action
4. Only approved actions execute with dry_run=False

## Override
This policy cannot be overridden. All execution actions require approval.