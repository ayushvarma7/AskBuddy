# 3. Verify citations after the fact instead of trusting the prompt

**Status:** accepted

## Context

Every agent prompt ends with a hard rule against fabricating a filename,
section, or date. Nothing checked whether the rule held. The product claim —
"always cites its source, never makes things up" — rested entirely on the model
complying with an instruction.

Options considered: stronger prompt wording; an LLM judge scoring groundedness;
a deterministic check against corpus metadata.

## Decision

A deterministic post-hoc check (`citations.py`). Parse the `Source(s):` block,
resolve every filename and section against the metadata actually in the corpus,
and record the verdict on the answer's feedback row.

An LLM judge was rejected for this job: it costs a call per answer, is itself
capable of being wrong, and the question here is not a judgement call. Either
the cited document exists or it does not.

## Consequences

- Zero marginal cost: stdlib only, one cached metadata read.
- The claim becomes a measurable number in the feedback report rather than an
  aspiration.
- It runs **after** the answer is composed and never blocks delivery. A wrong
  citation is worth knowing about; withholding an answer the user is waiting on
  would be worse. This means a bad citation can still reach a user — the check
  is detection, not prevention.
- Section matching tolerates a dropped or added heading number, deliberately
  trading a little strictness for far fewer false positives.

## Revisit when

Verdicts show a failure mode the structural check can't see — for example a
citation that resolves correctly but doesn't support the claim. That is the case
for an LLM judge, as a second layer rather than a replacement.
