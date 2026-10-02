# Product scope

CodeAtlas is an evidence-first CLI for reviewing repository changes. It should
identify high-impact defects, explain the stable code location and evidence it
used, and only call a proposed patch validated after configured checks pass.

## Phase 0 and Phase 1 contract

The current deliverable is documentation, schemas, and a deterministic
evaluation foundation. The baseline runner reads case metadata and does not
execute repository code. It is not an LLM reviewer.

## Initial product boundary

- Python 3.11+ CLI with `review`, `eval`, and a future approval-gated `fix`
  command.
- Immutable base/head inputs and isolated snapshots for the future review flow.
- Typed findings, patches, run manifests, JSONL logs, and evaluation results.
- TypeScript and Python are the first language-adapter targets.
- Review categories: authentication, authorization, input validation, async and
  promise errors, sensitive-data exposure, error handling,
  dependency/configuration risks, and API contract/type errors.
- Results may be `detected`, `review_only`, `suggested`, `validated`,
  `rejected`, or `abstained`.

## Acceptance principles

Every blocker must have a stable location and supporting evidence. Missing or
conflicting evidence must result in abstention rather than invented certainty.
The author's branch is never silently changed, and all analyzed, skipped,
failed, and validated work is recorded.
