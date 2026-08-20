# 1. Two flat git agents rather than a nested git supervisor

**Status:** accepted

## Context

GitHub support needed both issue and PR capability. PRs alone want eight tools
(list, get, reviews, checks, files, merge status, plus writes), and issues
another eight. Putting all sixteen on one agent makes tool selection unreliable;
the obvious alternative was a `git_supervisor` owning two sub-agents, nested
under the top-level `CugaSupervisor`.

## Decision

Two peer agents — `git_issue_agent` and `git_pr_agent` — registered directly on
the top-level supervisor.

## Consequences

- Routing stays one hop. The supervisor prompt names both and says which kinds
  of question go where; a question needing both is delegated to both and the
  answers combined.
- No nested pause semantics. A tool approval already pauses the supervisor's
  graph; a second level of supervision would have meant reasoning about a pause
  propagating up through two graphs, which is where the approval-resume path is
  already at its most subtle.
- The top-level supervisor prompt carries more routing detail than it otherwise
  would.

## Revisit when

A third git surface (Actions, releases, projects) makes the top-level prompt's
routing section unwieldy, *and* nested pause behaviour is understood well enough
to be tested.
