# Real repository benchmarks

CodeAtlas has an offline, versioned benchmark format for measuring review and
repair behavior on public repositories, local mirrors, and sanitized fixtures.
The scorer never clones a repository, contacts a provider, executes fixture
code, installs dependencies, or applies a patch. Collection of observations is
a separate, explicitly controlled step.

The checked-in `real-repositories-v1` corpus is a **contract fixture**. It
covers all twelve required categories and includes a local Git bundle with
reproducible base and head commits. Its example observations test the scorer;
they are not evidence of analyzer or live-provider quality.

[Controlled provider comparison](provider-comparison.md) adds safe operational metadata, request/context identity controls, and explicit `offline`, `live`, `fallback`, and `dry_run` modes without mixing provider outcomes into quality scores.

## Case contract

A suite contains `benchmark_version` and a list of strict case objects. Unknown
fields are rejected. Every case records:

- an HTTPS `repository_url`, a safe local `fixture_ref`, or both;
- `repository_identity`, full 40-character `base_sha` and `head_sha` values;
- one or more languages and changed repository-relative files;
- expected finding categories, anchors, severities, quality decisions, and
  evidence requirements;
- required, allowed, or forbidden abstention;
- expected fixability, proposal eligibility, and patch scope;
- the allowed test-command policy, including explicit network and dependency
  installation decisions;
- known limitations and the benchmark version.

The `population` field is explicit:

- `positive`: at least one labelled finding anchor is required;
- `negative`: no finding anchors are allowed;
- `ambiguous`: intent is not established strongly enough for binary scoring.

The benchmark categories are `clean_changes`, `real_bugs`, `security_issues`,
`sensitive_data_exposure`, `authorization_authentication`,
`type_nullability`, `test_regressions`, `performance_regressions`,
`ambiguous_changes`, `correct_abstention`, `ai_generated_defects`, and
`multi_language_changes`.

Local fixture references cannot be absolute or contain `..`. On load, every
changed file must exist in the fixture's `before/` or `after/` snapshot. The
checked-in fixture also has `mirror.bundle`, which contains the declared Git
commits and can be cloned without network access.

## Quality collection

`codeatlas.benchmark.quality_collection` runs installed deterministic analyzers against the immutable sanitized snapshots and records measured findings separately from labels. It can also collect explicitly selected live reviewer or repair-provider results through injected bounded provider interfaces. Live collection requires explicit selection, network permission, an approved credential environment-variable name, provider timeout and response limits, and exact repository/base/head identity.

Unavailable live collection records `provider_mode_used: unavailable` with a stable reason such as `missing_credentials`, `network_disabled`, `timeout`, `rate_limit`, `malformed_response`, or `secret_response`. It never substitutes deterministic output. Fallback is disabled unless the caller explicitly selects `fallback` and supplies a non-network provider; reports retain the trigger and effective-provider attribution. Collection artifacts retain hashes, safe provider/model names, timings, usage metadata, and scored observations—not prompts, transcripts, credential values, or raw provider responses.

Deterministic fixture metrics remain contract evidence, not real-world provider quality. Validation statuses preserve `passed`, `failed`, `not_run`, `unavailable`, `blocked`, and `unsupported`; only `passed` and `failed` enter pass-rate denominators.

## Review observations and metrics

Review observations identify their mode as `deterministic` or `live_provider`.
The scorer always emits separate reports for these modes. Each observation can
record findings, abstention, ranking, duplicate links, evidence, time to first
finding, total duration, provider attempts and failures, retries, token usage,
and cost.

Each metric has a numerator, denominator, population descriptions, exclusions,
benchmark version, unit, and a 95% Wilson confidence interval for binomial rates
when a nonzero denominator exists. Ranking quality and non-rate means do not use
a misleading binomial interval. A missing denominator produces `null`, not a
fabricated zero. No combined accuracy metric is emitted.

| Metric | Numerator | Denominator |
|---|---:|---:|
| Precision | uniquely matched expected findings | matched findings + unmatched reported findings |
| Recall | uniquely matched expected findings | labelled anchors in positive cases |
| False-positive rate | clean cases with one or more findings | negative cases |
| False-negative rate | unmatched labelled anchors | labelled anchors in positive cases |
| Abstention rate | abstained observations | all observations in the mode |
| Coverage | non-abstained observations | all observations in the mode |
| Duplicate rate | findings linked through `duplicate_of` | reported findings |
| Finding-anchor accuracy | matched findings overlapping the labelled file/range | matched findings |
| Severity accuracy | matched findings with the labelled severity | matched findings |
| Quality-decision accuracy | correct matched-finding decisions plus correct required/forbidden abstention behavior | matched findings plus cases with required/forbidden abstention |
| Abstention-behavior accuracy | cases obeying required or forbidden abstention | cases where abstention is not merely allowed |
| Evidence completeness | matched findings satisfying the evidence requirement | matched findings |
| Ranking quality | sum of reciprocal rank of the first relevant finding | cases with labelled findings |
| Time to first finding | mean observed milliseconds | observations reporting it |
| Total review duration | mean observed milliseconds | observations in the mode |
| Provider failure rate | failed provider attempts | provider attempts |
| Retry rate | retries | provider attempts |
| Token usage and cost | sum of reported values | observations reporting the value (the metric value is the mean) |

Ambiguous cases are described in every report but excluded from precision,
recall, and binary error rates. Abstention and coverage still include them.
Finding matching uses unique category labels first; anchor accuracy is measured
separately so a right category at the wrong location cannot masquerade as an
accurately anchored finding.

## Repair observations and metrics

Repair observations report every gate independently. `passed`, `failed`,
`not_run`, and `unavailable` are distinct check outcomes. The scorer reports:

- proposal eligibility correctness and structured-output acceptance;
- patch parse success, target-file scope accuracy, and changed-line scope;
- secret and policy safety;
- syntax, formatter, linter, type-check, targeted-test, and full-suite pass
  rates, excluding `not_run` and `unavailable` from their pass denominators;
- regression, accepted-patch, rejected-patch, revert, and apply-conflict rates;
- average repair attempts, latency, and cost per proposal.

A generated proposal, an applied patch, a syntax pass, one passing test, or a
provider confidence value is never treated as proof that a defect is fixed.
Lifecycle records use precise statuses:

```text
proposal_generated
patch_policy_accepted
syntax_passed
type_check_unavailable
targeted_tests_passed
full_suite_not_run
validated_in_sandbox
applied_after_confirmation
reverted_by_user
```

The observation model rejects contradictory claims, including `syntax_passed`
with a failed syntax check, `type_check_unavailable` with an available result,
an accepted and rejected patch, or a revert without prior acceptance.

## Collecting benchmark repositories

Collection is separate from scoring and has two explicit modes. It returns
metadata and status only; it never returns source contents, applies patches,
runs tests, approves validation, commits, merges, pushes, or contacts a model
provider.

### Offline sanitized mode

Use the checked-in fixture with no network, credentials, live provider, or
external service:

```bash
codeatlas benchmark-collect --mode offline \
  --suite eval/cases/real-repositories/v1/suite.json \
  --case-id real-bug
```

The fixture must contain `before/`, `after/`, `revisions.json`, and
`metadata.json`. The metadata records an SPDX license identifier and provenance;
the declared base and head commits, repository identity, labels, anchors,
severity, abstention, fixability, patch scope, test policy, limitations, and
benchmark version remain in the versioned suite. The result includes a digest
of labelled snapshot files, never their contents.

### Controlled public-repository mode

Public collection must be explicitly invoked with an HTTPS URL, case ID, exact
40-character base and head SHAs, license metadata, and provenance:

```bash
codeatlas benchmark-collect --mode public \
  --suite path/to/pinned-suite.json --case-id case-001 \
  --repository-url https://github.com/example/project.git \
  --base-sha <40-character-base-sha> --head-sha <40-character-head-sha> \
  --license-spdx MIT --provenance "upstream repository; license file reviewed"
```

The collector uses a bounded, non-interactive Git fetch in a temporary
workspace, verifies both exact commits, and removes the workspace. It does not
modify branches, push, mutate pull requests or issues, install dependencies,
or place credentials in repair context. Moving branches are never accepted as
benchmark pins. A failure is machine-readable and stable, for example:

```text
status: unavailable
reason: sha_verification_failed
```

Supported reason codes include `case_not_found`, `fixture_unavailable`,
`metadata_unavailable`, `invalid_url`, `missing_pin`, `license_missing`,
`fetch_failed`, `sha_verification_failed`, `timeout`, and `cleanup_failed`.

## Running the benchmark

Run both example observation sets without network access:

```bash
codeatlas benchmark \
  --suite eval/cases/real-repositories/v1/suite.json \
  --review-observations eval/cases/real-repositories/v1/example-review-observations.json \
  --repair-observations eval/cases/real-repositories/v1/example-repair-observations.json \
  --output eval/results/real-repositories-contract.json
```

Report writes are atomic and reject destinations that resolve to the suite or observation inputs, including symbolic-link destinations.

The package module offers the same interface:

```bash
python -m codeatlas.benchmark.runner --suite ... --review-observations ...
```

Regenerate the sanitized snapshots, Git bundle, labels, and example
observations with:

```bash
python eval/generate_real_repository_benchmark.py
```

Real benchmark publication should preserve the suite version, source license,
repository identity, immutable SHAs, observation mode, tool/provider versions,
and known limitations. Credentials, provider tokens, private source, secrets,
and raw user data must never be placed in labels, fixtures, observations, or
reports.
