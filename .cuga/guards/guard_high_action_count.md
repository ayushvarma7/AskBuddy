---
description: Flags when extract_action_items produces more than 8 action items, likely indicating a parsing issue
enabled: true
id: guard_high_action_count
name: High Action Item Count Guard
priority: 90
triggers:
  natural_language:
  - more than 8 action items
  - unusually high number of tasks
  - many action items
  target: intent
  threshold: 0.7
type: intent_guard
intent_examples:
- The transcript has 10 action items to process
- I found 12 tasks that need to be created
- There are 15 action items from this meeting
- Processing 9 action items from the transcript
- Extracted 11 tasks from the meeting
response:
  response_type: natural_language
  content: |
    ⚠️  UNUSUALLY HIGH ACTION ITEM COUNT DETECTED
    
    The transcript appears to have generated more than 8 action items, which is 
    unusually high for a typical meeting. This may indicate:
    
    - **Parsing issue**: Multiple items incorrectly merged or split
    - **Unusually dense meeting**: A very long or packed agenda
    - **Misclassification**: Items that should be decisions or follow-ups, not tasks
    
    RECOMMENDED ACTIONS:
    1. Review the extracted items list carefully
    2. Check if any items are duplicates or should be combined
    3. Verify that decisions aren't being treated as action items
    4. Confirm that items without owners are properly flagged
    
    Would you like to:
    - Continue with task creation after review
    - Re-analyze the transcript with different parsing
    - Manually review and filter the action items first
allow_override: true
---

# High Action Item Count Guard

## Purpose
Prevents the agent from blindly creating an excessive number of tasks when something goes wrong with transcript parsing or classification. A typical meeting rarely produces more than 5-8 concrete action items.

## Detection Logic
Triggers when the agent attempts to process more than 8 action items from a single transcript.

## Common Causes
1. **Parser confusion**: Speaker turns incorrectly split, causing one item to become multiple
2. **Over-eager classification**: Suggestions or discussions treated as commitments
3. **Missing context**: Items that reference "we should" without actual assignment
4. **Long meetings**: Genuinely dense meetings (less common but valid)

## Response
The guard pauses execution and asks the user to review the extracted items before proceeding. The user can:
- Override and continue (if the count is legitimate)
- Request re-analysis
- Manually filter items before execution

## Priority
Set to 90 (high) to ensure it's checked before tool approval policies (100) but after most other guards.