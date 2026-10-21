# Limited-release operations and rollback

This document is part of the Work Package 9 release gate. It describes the
operator actions required for a controlled limited-release decision. It does
not authorize public hosting, billing, automatic rollout, automatic apply,
automatic approval, automatic commit, automatic merge, or automatic push.

## Release evidence inputs

The release gate consumes only explicitly named artifacts. It does not scan
repositories, discover evidence files, inspect arbitrary directories, contact a
provider, contact GitHub, execute repository tests, or install dependencies.

Validate the plan and run the gate with explicit paths:

```bash
python -m codeatlas --json release-check \
  --plan config/controlled-beta-evidence-plan.json \
  --evidence <LIMITED_RELEASE_EVIDENCE_JSON> \
  --artifact-evidence <ARTIFACT_EVIDENCE_JSON> \
  --artifact-directory <ARTIFACT_DIRECTORY> \
  --source-root <IMMUTABLE_SOURCE_CHECKOUT> \
  --kpi-catalog eval/product-validation/metrics-v1.json
```

The command returns exactly one decision:

- `release_candidate`: all required evidence and safety gates passed without
  limitations;
- `release_candidate_with_limitations`: no blocking safety failure exists, but
  one or more declared populations remain contract-only, unavailable, or
  not-run;
- `blocked`: mandatory evidence is missing or invalid, a safety gate failed,
  artifact identity/checksum verification failed, or rollback/cleanup was not
  verified.

`production_ready` remains `false` for every result. A release candidate is a
limited controlled decision, not a general-availability claim.

## Pre-release checklist

Before considering a candidate:

1. Validate `config/controlled-beta-evidence-plan.json` against its runtime
   model and checked-in schema.
2. Confirm the benchmark identity is `wp7-controlled-beta-v1` and that the
   evidence source revision is the exact intended source revision.
3. Use only explicitly authorized repositories and sessions. Confirm owner or
   maintainer authorization and per-session operator/reviewer opt-in.
4. Confirm that session, feedback, incident, and KPI exports contain metadata
   only. Do not retain source, prompts, provider transcripts, credentials,
   approval tokens, reviewer names, or unrestricted environment values.
5. Independently verify artifact checksums, package boundaries, secret scans,
   source revision identity, and reproducibility identity.
6. Inspect every KPI group independently. Preserve numerator, denominator,
   exclusions, limitations, status, and evidence basis.
7. Verify that no critical product issue is unresolved:
   unauthorized mutation, broken approval binding, cross-repository scope
   failure, credential/network boundary failure, artifact verification
   failure, unsafe patch rejection, rollback failure, cleanup failure, or
   missing required evidence.
8. Confirm rollback and cleanup evidence is present and explicitly passed.
9. Read every limitation. Do not convert unavailable, blocked, or not-run
   evidence to a pass.

## Rollback procedure

Rollback is an explicit operator action:

1. Stop starting new beta sessions and mark the release decision `blocked`.
2. Do not retry automatically and do not reuse a stale approval, proposal,
   session, or artifact identity.
3. If a patch was explicitly applied, use the existing scoped revert workflow
   with the current repository and patch identity. A failed or unavailable
   revert remains a release-blocking observation.
4. Preserve only the bounded incident reason code and safe recovery status.
   Never export exception text, paths, diffs, source, command output, tokens,
   or provider responses.
5. Re-run the release gate only with a new explicitly supplied evidence package
   whose source, plan, artifact, and recovery identities match.

Rollback evidence must distinguish:

- passed rollback;
- failed rollback;
- blocked rollback;
- unavailable rollback;
- not-run rollback.

Only `passed` rollback observations can satisfy the release gate. A missing or
not-run rollback observation blocks the release decision.

## Cleanup procedure

Cleanup is ownership-safe and explicit:

1. Remove only temporary resources created by the current authorized
   operation.
2. Do not delete persisted session, incident, or evidence paths implicitly.
3. Do not terminate foreign processes.
4. If a worktree or process may remain after interruption, record
   `orphan_worktree` or the appropriate stable incident code and require manual
   inspection by the owner.
5. Verify that the original repository bytes and Git state are unchanged for
   read-only, rejected, dry-run, cancelled, and recovery operations.
6. At retention expiry or authorized withdrawal, explicitly delete exported
   metadata artifacts, temporary checkouts, and pseudonymous reviewer records,
   then record a bounded deletion verification status.

Cleanup evidence must not claim success merely because a process returned. An
unknown cleanup state is not a passed cleanup state.

## Stop conditions

Stop collection and hold the release if any of the following occurs:

- missing or withdrawn consent;
- unauthorized repository or session;
- benchmark or ground-truth drift;
- secret or sensitive material in an artifact;
- unauthorized mutation or approval bypass;
- cross-repository access;
- credential or network boundary failure;
- corrupt or tampered artifact;
- failed checksum or reproducibility verification;
- failed rollback or cleanup;
- unresolved critical incident;
- missing mandatory evidence;
- unbounded collection or retention.

Do not widen permissions, use a different repository, replace a missing
denominator, or relabel an unavailable state to pass. Fix the cause, collect a
new explicitly authorized observation, and rerun the gate.

## Current limitations

The release gate does not establish live-provider quality, public repository
coverage, hosted CI behavior, interactive VS Code behavior, native sandbox
isolation, or full platform/toolchain coverage unless those observations are
explicitly supplied and independently verified. The existing contract-only
offline evaluator remains contract-only, and `production_ready=false` remains
mandatory.
