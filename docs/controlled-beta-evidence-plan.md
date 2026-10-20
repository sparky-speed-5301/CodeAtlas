# Controlled beta evidence plan (Work Package 9)

This is the predeclared privacy-safe evidence plan for a limited, controlled
beta. It describes what may be observed and which evidence is sufficient to
consider a later release decision. It does **not** collect telemetry, authorize
a repository, grant provider access, approve a proposal, or establish release
readiness by itself.

The machine-readable contract is:

```text
config/controlled-beta-evidence-plan.json
```

The contract is validated by `codeatlas.beta_plan`. Loading the file requires an
explicit path to one unlinked regular file. The loader does not search for
plans, discover sessions, inspect repositories, start services, call a
provider, or send data anywhere. The checked-in plan remains
`production_ready=false` and its release assessment is `not_evaluated`.

This plan is used together with:

- [controlled beta operations](controlled-beta-operations.md) for the bounded
  local workflow and recovery rules;
- [controlled beta feedback](controlled-beta-feedback.md) for metadata-only
  session, feedback, incident, and execution evidence;
- [controlled beta product validation](controlled-beta-validation.md) for the
  `human-review-v1` reviewer protocol and the existing
  `wp7-controlled-beta-v1` benchmark.

## Plan identity and status

- Plan version: `controlled-beta-evidence-plan-v1`.
- Benchmark version: `wp7-controlled-beta-v1`.
- Protocol version: `human-review-v1`.
- The plan contains no source, prompt, provider transcript, credential,
  approval token, unrestricted environment value, repository path, or reviewer
  name.
- `production_ready` is permanently `false` in this contract.
- A plan status is not an execution result and is not a product-quality score.

The four statuses are explicit:

| Status | Meaning | Allowed action |
| --- | --- | --- |
| `configured` | The bounded plan is structurally valid and ready for separately authorized collection. | An operator may begin only an explicitly consented session. |
| `blocked` | A policy, authorization, consent, or ground-truth gate prevents collection. | Stop; do not bypass the gate or relabel the session. |
| `invalid` | The plan payload failed schema, bounds, privacy, or consistency validation. | Repair the plan outside the collection flow and validate it again. |
| `not_evaluated` | No observation has been supplied for a declared assessment or release decision. | Keep the result unclaimed; do not convert it to pass, fail, or zero. |

`validate_evidence_plan` and `validate_evidence_plan_file` return a bounded
status and the stable error code `schema_validation_failed`; invalid payload
details are not reflected. A valid plan with `status=blocked` or
`status=not_evaluated` is still non-collectable for the corresponding reason.

## Cohort and repository selection

The plan is intentionally small:

- minimum participants: **5**;
- target participants: **8**;
- maximum participants: **12**;
- maximum sessions: **36**;
- minimum repositories: **3**;
- target repositories: **6**;
- maximum repositories: **12**.

Every repository must satisfy all of these requirements before a session is
started:

1. The owner or maintainer has explicitly authorized the controlled beta.
2. The operator has an explicit local checkout and immutable base and head
   revisions. A URL, repository name, or account relationship is not a
   substitute for a local scope check.
3. The worktree is clean under the existing onboarding rules.
4. The repository is in the controlled repository set and uses one of the
   supported languages in this plan.
5. The work is metadata-only at export time. Source may be read by the already
   authorized local operation as required by the existing review flow, but raw
   source is not evidence-plan data and is not persisted by this plan.
6. The repository owner can withdraw authorization and request deletion of
   plan artifacts.

Allowed repository classes are explicitly authorized private repositories,
explicitly authorized public repositories, and sanitized controlled fixtures.
Unowned, unapproved, production-live, mutable, unpinned, or unsupported
repositories are excluded. A checked-in public benchmark manifest is metadata
and labels, not proof that a public repository was collected or reviewed.

Sessions are selected separately. An operator must opt in to each session and
explicitly export its bounded metadata artifact. There is no directory scan,
background collector, implicit session discovery, or unsolicited collection.

## Language distribution

The target is 18 review cases, distributed as follows. Minimum and maximum
values are planning bounds, not permission to exceed the repository, consent,
or session limits.

| Language | Minimum | Target | Maximum |
| --- | ---: | ---: | ---: |
| Python | 2 | 6 | 8 |
| JavaScript/TypeScript | 2 | 3 | 5 |
| Go | 2 | 2 | 4 |
| Java | 2 | 3 | 5 |
| Rust | 2 | 4 | 8 |
| **Total** | **10** | **18** | **30** |

The distribution must cover every language exactly once. Missing toolchains are
recorded as `unavailable`; they are not replaced by a different language and
are not counted as successful observations.

## Review and FixProposal case distribution

The review target has 18 cases. Each category is present so that a clean
change, real bug, security-sensitive change, ambiguity, and correct abstention
remain separate populations.

| Review category | Minimum | Target | Maximum |
| --- | ---: | ---: | ---: |
| Clean changes | 1 | 2 | 3 |
| Real bugs | 1 | 3 | 5 |
| Security issues | 1 | 2 | 4 |
| Sensitive-data exposure | 1 | 1 | 2 |
| Authorization/authentication | 1 | 2 | 3 |
| Type/nullability | 1 | 1 | 2 |
| Performance regressions | 1 | 1 | 2 |
| Ambiguous changes | 1 | 2 | 3 |
| Correct abstention | 1 | 2 | 3 |
| AI-generated defects | 1 | 2 | 3 |
| **Total** | **10** | **18** | **30** |

The FixProposal target is eight cases, always a bounded subset of the review
cases:

- real bugs: 2;
- security issues: 1;
- authorization/authentication: 1;
- type/nullability: 1;
- performance regressions: 1;
- AI-generated defects: 2.

Proposal eligibility, proposal generation, inspection, acceptance, validation,
testing, apply, conflict, correction, regression, and revert remain separate
observations. A generated proposal is not an accepted fix. A validation
approval is not apply approval. No plan threshold authorizes automatic apply,
approval, commit, merge, or push.

## Observation period and session policy

The declared observation period is **2026-10-26 through 2027-01-22**, inclusive
(89 days). Operators must not silently extend it. A revised period requires a
new versioned plan and renewed authorization.

Session policy:

- target duration: 45 minutes;
- maximum duration: 120 minutes;
- idle timeout: 15 minutes;
- maximum individual stage: 30 minutes;
- timeout stops the session stage;
- automatic retry is disabled.

The existing session event history, output, and incident bounds remain in
force. A timeout, cancellation, blocked stage, or unavailable tool is retained
as that outcome. It is never inferred to be a successful review or a failed
product result without the relevant denominator.

## Inclusion and exclusion

### Include only when

- repository authorization and per-session opt-in are recorded;
- base and head are immutable and locally scope-checked;
- the worktree is controlled and clean;
- the language and required tool are supported or their unavailability is
  explicitly recorded;
- the corresponding benchmark label or ground truth is available;
- reviewer consent is present before any reviewer identity hash is created;
- the operator chooses an explicit metadata-only artifact export path.

### Exclude or stop when

- authorization or consent is absent or withdrawn;
- revisions are mutable, stale, or not pinned;
- the operation needs raw source, prompts, transcripts, credentials, tokens, or
  unrestricted environment data in a retained artifact;
- a repository, run, finding, proposal, or session scope check fails;
- a language, provider, service, extension, platform, or toolchain is
  unavailable. Preserve `unavailable` or `not_evaluated` rather than filling
  the gap with a different observation;
- ground truth is unavailable, exposed before submission, or drifts from the
  declared benchmark;
- collection, retention, or session bounds would be exceeded.

## Ground truth and reviewer protocol

Ground truth uses the immutable `wp7-controlled-beta-v1` case labels and pinned
base/head metadata. At least two independent adjudicators are required for
the declared ground-truth set of 18 cases. Expected labels remain hidden until
the reviewer submits an outcome. Prescribed tests are policy inputs, not
ground truth and not proof of semantic correctness.

Use `human-review-v1`:

1. Obtain repository-owner, operator, and reviewer consent before collection.
2. Randomize case order per reviewer while preserving case and run scope.
3. Record only the existing bounded pseudonymous reviewer hash; never store a
   reviewer name or account identifier.
4. Record the eight protocol capabilities as `yes`, `no`, or
   `not_evaluated`.
5. Record finding and FixProposal outcomes using the existing enum-only
   outcomes and bounded reason codes.
6. Run validation or tests only after their existing scoped approval boundary.
7. Preserve blocked, unavailable, timeout, cancellation, and not-run states.
8. Submit one record per case and run. Independently adjudicate disagreements
   without exposing the other answer first.

Free-form reviewer comments, prompts, source excerpts, provider transcripts,
credentials, approval tokens, and raw command output are not collected as
evidence. The pseudonymous hash is retained only because the reviewer has
explicitly consented to that bounded study identifier.

## Retention and deletion

The retention period is **30 days** from the end of the declared observation
period, or until an authorized withdrawal request, whichever requires earlier
action. Only metadata-only evidence is retained. Raw repository source, raw
prompts, provider transcripts, credentials, approval tokens, unrestricted
environment values, and source paths are never retained by this plan.

Deletion is an explicit operator action. At expiry or withdrawal, the operator
deletes exported artifacts, temporary checkouts, and pseudonymous reviewer
records, then verifies deletion using a bounded status-only record. Automatic
deletion of persisted paths and termination of foreign processes remain
disabled. Existing recovery rules apply if a process exits unexpectedly; the
plan never authorizes cleanup of a path it does not own.

## Consent and collection boundary

Consent mode is `explicit_opt_in_before_collection`:

- repository-owner authorization is required;
- the operator opts in to each session;
- each reviewer consents before review and before a reviewer hash is retained;
- withdrawal is allowed;
- collection before consent is false;
- silent collection is false;
- consent is represented only by a bounded status artifact.

The plan is not telemetry. No event is collected merely because CodeAtlas is
installed, a service is running, a repository is opened, or a provider is
configured. A session artifact exists only after the operator explicitly
invokes the existing stage/export flow. Do not add a global collector, an
analytics SDK, a hidden upload, or a second persistence owner.

The following are fixed off: automatic apply, automatic approval, automatic
commit, automatic merge, automatic push, arbitrary commands, dependency
installation, and unrestricted network access.

## Success thresholds

Thresholds are evaluated independently, with raw numerator and denominator
preserved by the existing evidence contracts. A zero denominator is
`not_evaluated`, not zero percent. `contract_only`, `unavailable`, and
`not_run` evidence cannot satisfy a measured success threshold.

| Metric | Direction | Target | Minimum observations |
| --- | --- | ---: | ---: |
| Review completion rate | minimum | 0.80 | 15 |
| Finding usefulness rate | minimum | 0.70 | 10 |
| FixProposal acceptance rate | minimum | 0.60 | 6 |
| Validation success rate | minimum | 0.80 | 6 |
| Secret-redaction success rate | minimum | 1.00 | 18 |
| Unsafe-action rejection rate | minimum | 1.00 | 10 |
| Unauthorized-apply rejection rate | minimum | 1.00 | 10 |
| Cross-repository isolation rate | minimum | 1.00 | 6 |
| Consent compliance rate | minimum | 1.00 | 18 |
| Unexpected workspace mutation rate | maximum | 0.00 | 18 |

These are release gates, not a composite score. They do not establish broad
quality, correctness, public-repository coverage, provider generalization, or
production readiness. Existing independent metrics, language/category splits,
limitations, and unavailable/not-run statuses must remain visible.

## Release-stop conditions

Stop collection and hold any limited-release decision if any of these occurs:

- missing or withdrawn consent;
- unauthorized repository or session;
- ground-truth or benchmark drift;
- secret or sensitive material in an artifact;
- an unsafe-command, approval, validation, apply, commit, merge, push, or
  network boundary failure;
- a cross-repository scope failure;
- unexpected workspace mutation;
- a required threshold is not met;
- required evidence is not evaluated;
- collection or retention would be unbounded.

Do not recover by deleting evidence, retrying automatically, relabelling a
failure, or widening permissions. Record a bounded incident through the
existing incident contract, preserve the stable failure status, and follow the
operations troubleshooting table.

## Current limitations

The plan does not claim that a live provider, public repository collection,
hosted GitHub workflow, interactive VS Code session, full platform matrix, or
native toolchain matrix has run. The existing contract-only evaluation remains
`contract_only`; external observations remain `not_run` or `unavailable` until
an explicitly authorized operator performs them. No threshold can convert
those limitations into a pass.
