# 2. One chunk table for every corpus, discriminated by a column

**Status:** accepted

## Context

HR and IT documents must never bleed into each other's answers. Options: a table
per corpus, a schema per corpus, or one table with a discriminator column.

## Decision

One `hr_chunks` table with a `corpus` column. Every retrieval query filters on
it; `_vector_search` and `_keyword_search` each take the corpus and thread it
into their SQL.

## Consequences

- One HNSW index and one GIN index to maintain, not one pair per domain.
- Adding a corpus needs no DDL — only a registry entry and an ingest run.
- Isolation is a query-level guarantee, so **every** read path has to apply the
  filter. A new query that forgets it silently leaks across domains. This is the
  real cost of the decision and the reason corpus scoping is called out in
  `AGENTS.md`.
- The table name is now wrong: it holds IT documents too. Renaming needs a data
  migration, so it stays.

## Revisit when

Corpora need different embedding dimensions or different retention rules, either
of which breaks the shared-schema assumption.
