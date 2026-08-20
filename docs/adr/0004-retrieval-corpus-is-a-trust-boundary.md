# 4. Treat the retrieval corpus as untrusted input

**Status:** accepted

## Context

Ingested document text is fed to an LLM. That LLM runs under a supervisor that
also owns `merge_pull_request` and `set_issue_state`. Anyone who can add a
markdown file to `data/` can therefore put text in front of a model that has
write access to GitHub — a prompt-injection path from a document to a merge.

## Decision

Treat `data/` as untrusted input and rely on defence in depth rather than on
sanitising documents:

1. **Tool isolation.** A domain agent's tool list is exactly
   `[<corpus>_retrieve, post_slack_message]`. Retrieved text is only ever read
   by an agent that has no GitHub tools at all.
2. **Human confirmation.** Every irreversible GitHub action is approval-gated
   and needs an explicit Slack click. This is the backstop, and it is why the
   confirmation step must not be removed for convenience.
3. **Least privilege.** `GITHUB_TOKEN` is a fine-grained PAT; write scope is
   opt-in.

## Consequences

- Injected instructions in a document can influence what a *domain* agent says,
  but cannot reach a tool that changes anything.
- The supervisor does see sub-agent output, so the isolation is one level deep,
  not absolute. Confirmation is what covers the remaining gap.
- Whoever can write to `data/` is effectively trusted to influence answers.
  Corpus contents deserve the same review as code.

## Revisit when

Document ingestion becomes self-service, or an agent is given both retrieval and
write tools — either change invalidates the tool-isolation argument and needs a
different control.
