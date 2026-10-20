# Controlled beta feedback and report interpretation

This guide covers Work Package 8 operational evidence. It is not a claim that
external users, public repositories, live providers, hosted GitHub CI, or an
interactive VS Code beta were exercised. Engineering fixtures and actual
operator observations must remain distinguishable. Start with the
[controlled beta operations guide](controlled-beta-operations.md).

For Work Package 9 collection boundaries, use the [controlled beta evidence
plan](controlled-beta-evidence-plan.md) and validate the explicit machine-readable
contract at `config/controlled-beta-evidence-plan.json` before collecting any
session. The plan is not telemetry and does not turn contract-only evidence into
measured beta evidence.

## Collect a bounded session

Use a controlled repository and explicit immutable base/head revisions. Export
onboarding evidence to a new operator-controlled file outside the repository,
then create and run a session through the existing `beta-session` stages. Each
stage is separately invoked; inspect it before proceeding:

```bash
python -m codeatlas --json beta-session inspect --session beta-session.json
python -m codeatlas --json beta-session complete --session beta-session.json
python -m codeatlas --json beta-session export --session beta-session.json --output session-export.json
```

Mark completion only when the intended operator workflow is finished. Completion
is a lifecycle marker, not proof that review, proposal, validation, tests, or
apply succeeded. Session creation alone can select `live` without any provider
request having occurred. Preserve the individual stage result and failure code.
A beta-session retry records retry intent; it does not automatically rerun a
stage. Cancellation does not authorize cleanup or roll back a previously applied
patch. Follow the underlying service cancellation/recovery contract separately.

Record only safe, bounded identifiers and metadata: session/run/case IDs,
repository identity hash, immutable SHAs, language, provider/model identifier,
context/patch hashes where available, stage outcomes, durations, evaluated
finding counts, approval/validation/test/apply/revert states, and safe failure
codes. Record missing observations explicitly. Do not supply guessed durations,
counts, correctness labels, token/cost estimates, or successful cleanup merely
because an operation returned.

## Finding and fix feedback

The local API accepts enum-only finding and FixProposal feedback. Finding feedback is available through `findings feedback`; FixProposal feedback is available through the existing scoped `fix feedback` command. `findings export-feedback <RUN> --repository-identity <HASH> --output <NEW_FILE>` creates a bounded safe export for reporting. The live service verifies the run, finding, proposal, and repository scope; do not hand-edit session JSON to fabricate feedback.

For each finding, decide whether the anchor and explanation are understandable,
whether the evidence supports the claim, and whether it is actionable in the
pinned change. Preserve acceptance, rejection, duplicate, false-positive,
needs-more-context, and abstention distinctions when the collection interface
supports them. A local dismissal is not independently adjudicated false-positive
evidence. A provider abstention is not necessarily a human rejection.

For each proposal, record whether it was inspected, accepted, rejected, or needs
regeneration; separately record scoped validation approval, actual validation,
requested and executed test scope, apply approval, apply conflict, and revert.
Approving validation does not mean accepting the proposed fix or authorizing
apply. A proposal may parse and pass syntax checks while being logically wrong.
A passing targeted test establishes only that executed scope; it does not prove
correctness, security, or no regressions. Never invent acceptance or false-positive
rates when the available feedback does not distinguish their populations.

The separate [human-review protocol](controlled-beta-validation.md) uses
`HumanReviewEvaluation` and `schemas/human-review-evaluation.schema.json` for
product-study observations. It is not interchangeable with beta session metadata.
Follow its predeclared populations, timing, adjudication, and pseudonym rules
rather than treating operational completion as precision, recall, or time saved.

## Incidents: safe collection boundary

Incident storage is a local library contract in `codeatlas.beta_incidents`; it does not introduce a second database owner or an unrestricted free-text CLI. `BetaIncidentStore` creates, appends, loads, retains, and exports repository-scoped records. `create_beta_incident` accepts a closed reason code and projects the corresponding component, severity, safe message, and remediation. Records contain optional session/run identity, bounded timestamp, recovery status, redaction status, and workspace observation. Service/provider failures,
stale reviews, interruption, test cancellation, patch conflict, GitHub failures,
extension disconnects, unavailable tools/database, and possible orphan worktrees
are distinct conditions.

Do not attach exception text, provider responses, source/diffs, shell output,
filesystem paths, credentials, approval tokens, prompts, transcripts, unrestricted
environment values, or identifying free-form comments. `workspace_unchanged`
may be unknown: record true only when actually observed. A worktree warning
never authorizes automatic deletion or implies recovery succeeded. Use the
[operations troubleshooting table](controlled-beta-operations.md#troubleshooting-incidents-and-recovery)
for explicit operator remediation.

Incident storage is repository-scoped and bounded. Retention/truncation means a
retained incident count is not an all-time failure count, and absence of records
is not proof that no incidents occurred. Separate retained records, occurrence
counts, and recovery events if the final report exposes them; no recovery event
means automatic replay or renewed approval authority. Incident reporting must
not open or repair the service database, inspect saved worktree paths, execute
tests, contact a provider/GitHub, or mutate the workspace.

## Build an execution report

Use explicit evidence paths; the report does not search for sessions or scan
repositories. `--session` may be repeated at most 50 times, and duplicate session
identities are rejected rather than counted twice. `--onboarding` accepts a
bounded onboarding report, not a repository path:

```bash
python -m codeatlas --json beta-report --config config/controlled-beta.json --session session-export.json --onboarding onboarding.json --output execution-report.json
python -m codeatlas --json beta-report --config config/controlled-beta.json --session session-one.json --session session-two.json --output execution-report-many.json
```

A single onboarding report must match every supplied session's repository
identity. For multiple repositories, keep onboarding-scoped reports separate;
do not attach one repository's readiness result to another repository's sessions.
The identity hash describes the canonical local repository root, not a public
URL, account identity, authorization credential, or proof of GitHub ownership.
Moving a checkout may change that identity.

Incident input `--incidents <PATH>` requires `--repository <LOCAL_CHECKOUT>` solely to recompute and verify the repository identity; the path is never exported. Use a metadata-only incident file and keep incidents, onboarding, sessions, and feedback inputs in the same repository scope. Feedback input uses repeatable `--feedback <SAFE_EXPORT>`. Do not substitute `OWNER/REPO` for the local identity check.
Mismatched or ambiguous evidence must fail closed, not silently be mixed or
relabelled. Incident totals remain unverified without the supported input path.

Without `--service`, reporting reads only explicitly supplied local evidence
and configuration. Adding `--service` explicitly permits bounded read-only
loopback diagnostics; it does not start a service or run a workflow. Reports
and each evidence file are bounded to 32 KiB. Export needs a new regular file
in an existing directory and rejects linked or existing destinations. Reporting
never executes a provider, review, test, approval, apply, commit, merge, or push.
A successful report export is not a successful beta execution.

## Interpret evidence, not readiness

Read the top-level check, onboarding status, execution evidence basis, each mode,
each numerator/denominator, missing outcomes, truncation flags, and limitations
independently. `beta_ready` is a bounded configuration/preflight result, not a
product-quality verdict. `production_ready=false` and
`correctness_established=false` remain explicit. A passed onboarding extension
compatibility check does not establish an interactive extension health result.

### Evidence basis

| Basis/status | Interpretation |
| --- | --- |
| `measured` | Counts derived from the explicitly supplied execution records. This is not independent attestation, statistical generalization, or proof the records came from external users. Preserve provenance. |
| `contract_only` | Synthetic/local fixtures exercise persistence, bounds, modes, or safety contracts; they do not measure live-provider quality or real user outcomes. |
| `unavailable` | Required evidence or an integration/tool is unavailable; do not convert it to success or zero failures. |
| `not_run` | The operation or rate population was not observed/executed. A zero denominator has no rate value; it is not 0% or 100% success. |

Do not promote a fixture to measured external-beta evidence merely by loading it
through the report CLI. Retain the evaluation artifact's `contract_only` basis
and its limitations when sharing results. Recorded stage metadata and a declared
basis alone are not cryptographic proof of provenance.

### All six execution modes

Reports retain six separate, ordered buckets even when some are empty:

| Mode | Meaning |
| --- | --- |
| `offline` | Explicit deterministic/non-network provider mode. Local review is not live-provider evidence; a GitHub dry-run can still use network transport. |
| `live` | Selected live-provider mode; only a separately authorized executed request is live-request evidence. Check stage outcomes, not the bucket alone. |
| `fallback` | A distinct explicit fallback outcome; the current beta workflow does not automatically fall back and never infers it from failure. |
| `unavailable` | An allowed dependency or resource could not be used. |
| `blocked` | A policy, approval, identity, or safety gate rejected execution. |
| `not_run` | No execution for the recorded mode/stage. Missing mode coverage stays explicit. |

Mode is not lifecycle status. A selected offline/live session can contain
blocked, unavailable, or not-run stages. Do not merge the six buckets into one
provider accuracy or readiness score. Do not confuse these execution modes with
the older provider-comparison contract's separate `dry_run` mode.

### Current operational counters and rates

| Field | Population and caveat |
| --- | --- |
| `session_completion` | Completed lifecycle records / supplied sessions in that mode; does not establish successful review or apply. |
| `review_completion` | Successful recorded reviews / records with review timing or an explicit review/provider failure; an unrun review is not counted as passed. |
| `finding_acceptance` | Accepted findings / accepted plus rejected findings; unevaluated findings are outside the denominator. Rejection categories do not establish a false-positive rate. |
| `abstention` | Recorded abstentions / recorded findings; inspect whether the underlying observation was provider abstention or human feedback. |
| `false_positive_rate`, `fix_proposal_acceptance` | Unavailable until sufficiently specific, compatible feedback is supplied; proposal generation is not acceptance. |
| `validation_success`, `test_success` | Passed / passed plus failed outcomes in their respective recorded scope; blocked, unavailable, and not-run cases are excluded, not counted as passed. Show those exclusions. |
| `apply_count`, `revert_count` | Recorded guarded applies and reverts; an applied-then-reverted patch counts as an apply too. Neither counter proves correctness. |
| `provider_failures`, `timeouts`, `cancellations` | Session-level metadata counts, not complete request-attempt or incident histories. |
| `github_dry_run` | Separate passed/failed/blocked/unavailable/not-run counts; metadata preflight alone is not a completed review dry-run. |

Bounded event history can be truncated. Always retain
`session_history_truncated` and incident truncation/retention information where
available. No denominator supports an inference about unobserved languages,
platforms, repositories, users, or providers. Historical repository-wide Ruff
and mypy baseline debt remains separate from focused beta-contract checks.

## Offline execution-contract evaluation

```bash
python -m eval.eval_beta_execution
python -m eval.eval_beta_execution --output beta-execution-evaluation.json
```

The evaluator uses newly owned temporary fixtures to check session restart,
bounded export, no overwrite, six-mode separation, and terminal-session scope.
Its fixture rates are `contract_only`. Live providers, public repositories,
GitHub, an actual extension host, platform matrix, and toolchain matrix are
explicitly `not_run` with unavailable evidence. `passed_with_limitations` means
those local contracts passed, not that beta acceptance is complete:
`acceptance_complete=false` and `production_ready=false` remain explicit.

Run this only with the already installed project dependencies. It does not
install tools or require network access, external credentials, repository tests,
or a target worktree. Windows/Linux, Python-version, native compiler/runner,
hosted CI, and interactive extension evidence must be collected separately.
Policy-level network restrictions are not independently verified OS isolation.
Do not report generated patches, parsing/syntax checks, or these fixture results
as semantic correctness or actual external beta outcomes.
