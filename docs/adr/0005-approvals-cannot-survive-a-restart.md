# 5. Accept that pending approvals cannot survive a restart

**Status:** accepted

## Context

A gated action pauses the `CugaSupervisor`'s graph, which uses an in-memory
checkpointer. Resuming needs the exact same Python object, held in
`_pending_approvals`. If the process restarts while a confirmation is pending,
that object is gone and the buttons in Slack cannot resume anything.

Options: serialise the paused graph (not supported by CUGA's in-memory
checkpointer); re-derive and re-execute the intended action on confirm; or
accept the limitation and handle it honestly.

Re-deriving was rejected because the intended tool call is not observable to us.
The graph pauses *before* the tool runs, so our tool function is never entered
and never sees its arguments. Reconstructing the action would mean re-parsing
the user's message and guessing — for `merge_pull_request`, guessing wrong is
unacceptable.

## Decision

Accept that a pending approval does not survive a restart, and make the failure
loud instead of silent.

`ask_buddy_pending_approvals` records every proposed high-risk action. At
startup, unresolved rows are marked `orphaned` and each affected conversation is
told explicitly that the request was cancelled and nothing changed on GitHub.
Clicks on stale buttons get a specific reason rather than a vague one.

## Consequences

- No approval is ever silently lost; the user is told to ask again.
- A complete audit trail of every high-risk action proposed, who resolved it,
  and how — valuable in its own right for a bot that can merge PRs.
- The safe direction is preserved: losing an approval means *nothing happens*.
- Deploys during a pending confirmation are mildly disruptive by design.

## Revisit when

CUGA gains a serialisable checkpointer, or exposes the pending tool call on the
paused graph — either would make real resumption possible.
