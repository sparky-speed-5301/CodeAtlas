# Persistent review operations (Work Package 5)

## Service startup

```bash
codeatlas serve --database /path/to/codeatlas-service.sqlite3
```

The long-running `serve` command defaults to `~/.codeatlas/service.sqlite3`.
Embedded `ReviewStateManager()` and `create_server()` instances retain the
existing in-memory defaults. Pass `persistence_path`, `PersistenceConfig`, or
`database_path` explicitly when embedding a persistent service.

The service owns its SQLite database exclusively. CLI, Python client, VS Code,
and CI consumers use the versioned HTTP API to share run identities; they do
not open the database themselves. A second owner fails with `database_in_use`.
Shutdown waits for review workers and snapshot cleanup before releasing the
database lock.

## Stored contracts

The schema has checksummed, transactional migrations (currently versions 1
and 2). Startup validates the migration ledger, actual schema definitions,
SQLite integrity, and foreign keys. A newer or altered schema fails closed.

Repositories and runs are separate tables. Entity projections are stored in a
normalized records table with scoped views for findings, feedback,
FixProposals, legacy PatchProposals, validation attempts and evidence metadata,
apply/revert events, lifecycle events, provider metadata, capability snapshots,
and safe error codes. Rows carry repository/run identity, applicable
finding/proposal identity, revisions, patch hash, schema/policy versions, and
timestamps. Canonical JSON and SHA-256 payload hashes detect accidental payload
changes. Payload hashes are integrity checks, not authorization credentials.

Only allowlisted, bounded metadata is stored. Exact redaction-audited proposal
diffs use the existing proposal artifact contract. Full repository source,
retrieved source context, arbitrary configuration, provider transcripts,
prompts, environment variables, credentials, approval tokens, unrestricted
diagnostics, and pre-apply worktree bytes are excluded. Diagnostics are
represented by safe outcome codes. Persisted validation evidence describes
only the executed scope and does not establish patch correctness.

`PersistenceConfig` bounds:

| Setting | Default |
| --- | --- |
| `max_runs` | 500 |
| `max_age_seconds` | 90 days |
| `max_payload_bytes` | 512,000 per serialized artifact |
| `max_records_per_run` | 5,000 current projections |

Findings are bounded to 200 with explicit truncation metadata. Apply/revert and
lifecycle/validation-attempt histories are bounded to 50 entries. Expired runs
lose their findings, proposals, evidence, and histories and retain a minimal
`expired` tombstone until capacity eviction. Capacity admission evicts terminal
runs rather than live work. SQLite additionally has a page-count bound.

## Recovery behavior

Recovery metadata uses `pending_recovery`, `recovered`, `recovery_failed`, and
`expired`; fresh records report `not_required`. Run lifecycle status remains
`completed`, `failed`, or `cancelled` when a terminal review is recovered.
An abandoned review reports `recovery_failed`. Interrupted fix operations are
marked failed independently of the parent review's completed status.

After restart, clients can retrieve completed finding summaries/details,
feedback/dismissal state, proposal diffs, validation outcome metadata, and
apply/revert history. Retrieved code context is intentionally session-local.
Recovered fixes are historical: they cannot approve, validate, apply, or
revert. Prior validation approvals are cleared. Revert availability is false
because exact original bytes are not stored. Recovery never executes a
provider, a test command, Git mutation, or workspace write.

The safe `operation_interrupted` diagnostic survives repeated service restarts.
An interrupted validation also retains a bounded limitation stating that sandbox
cleanup is unverified. A killed process may leave an isolated worktree behind;
recovery does not infer ownership from a saved path or automatically delete it.
This warning records uncertainty, not successful cleanup or permission to retry.

Deterministic proposal IDs may repeat. Durable keys include the repository and
run so proposals from different reviews are independently retrievable.

## Jobs and cross-client operations

Review execution has bounded admission and worker concurrency (defaults: two
workers, sixteen queued reviews). Queue exhaustion returns
`review_queue_full` with HTTP 429. Cancellation is cooperative: an already
running review completes its cleanup and cannot publish success after the
cancellation request. Mutating HTTP requests are never transparently retried.

```bash
codeatlas reviews status RUN_ID
codeatlas reviews events RUN_ID
codeatlas reviews cancel RUN_ID
codeatlas reviews retry RUN_ID
```

`POST /api/v1/reviews/{run_id}/retry` explicitly creates a new run for a failed,
cancelled, or interrupted review. Resolved revisions are retained where
available. It does not replay a fix or restore an approval. The new status
includes `retry_of`. `GET /api/v1/reviews/{run_id}/events` returns bounded,
repository-scoped lifecycle metadata. Python clients expose `retry_review`
and `get_review_events`. CLI polling and VS Code polling recognize recovery
failure and expiration as terminal outcomes. `/capabilities` reports
`state_lifetime: persistent` only for a configured persistent service.

Persistent fix operations journal identity before work begins. If that required
checkpoint fails, the operation is blocked before repository execution or
workspace mutation. A crash with an unfinished operation is never treated as
a successful automatic replay.

## GitHub Actions

`.github/workflows/codeatlas-review.yml` uses `pull_request` events, read-only
permissions, per-PR cancellation concurrency, a fixed timeout, and the existing
dry-run GitHub adapter. Trusted tooling is installed from the base revision;
the PR head checkout is review data. Checkout credentials are not persisted.
PR base/head SHAs are validated and refetched by the adapter so stale reviews
fail closed. Credentials are available only to the review step's GitHub
transport and are not stored in the review database.

The workflow produces a local dry-run report. It does not run PR repository
tests/build/install scripts or publish comments, approvals, commits, merges,
or pushes. GitHub-hosted execution and platform-specific runtime behavior need
their own execution evidence; local structural and fake-transport checks do
not demonstrate that a GitHub-hosted run passed.
