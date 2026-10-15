# Controlled beta product validation

This plan defines Work Package 7 product evidence. Passing engineering tests is
not production-readiness evidence, and this small curated study cannot support
statistical generalization to arbitrary repositories, teams, providers, or
workflows.

The versioned metric catalog is
`eval/product-validation/metrics-v1.json`. Every metric independently records its
definition, numerator, denominator, population, exclusions, benchmark version,
unit, and confidence-interval method. Binomial rates use a 95% Wilson interval
when the denominator is nonzero. Timing means and paired time differences use a
95% percentile bootstrap interval when enough complete observations exist; the
paired comparison resamples complete pairs. Missing, unavailable, unsupported, blocked, and
`not_evaluated` outcomes remain explicit and are never converted to success.

## Product-level success metrics

Metrics are reported in four separate groups. No weighted average, composite
quality score, or single readiness score combines unrelated outcomes.

### Review usefulness

- Finding precision and recall.
- Actionable-, accepted-, rejected-, duplicate-, and false-positive rates.
- Severity, anchor, explanation, and evidence usefulness.
- Time to understand and resolve a finding.
- Time to the first finding later accepted as useful.

### Autofix usefulness

- Proposal eligibility and generation success.
- Patch parsing, isolated validation, and targeted-test success.
- User approval, rejection, and regeneration rates.
- Apply conflict, revert, and regression rates.
- Time to an accepted fix.
- Paired time saved compared with a predeclared manual correction of the same
  task under the same validation policy.

### Reliability

- Review completion, timeout, and cancellation rates.
- Live-provider failure rate.
- Recovery and stale-run correctness.
- Mocked and hosted GitHub delivery correctness, reported separately.
- Extension startup success, service crash recovery, and orphaned-process rate.

### Trust and safety

- Secret-redaction success and token-leakage rate.
- Unsafe-command and unauthorized-apply rejection.
- Cross-repository isolation and stale-proposal rejection.
- Unexpected workspace mutation rate for operations with zero mutation authority.

The metric catalog is authoritative for the exact populations and exclusions.
Reports must include raw numerators and denominators so a rate cannot conceal a
small sample or excluded population.

## Curated public-repository set

`eval/cases/product-validation/v1/suite.json` contains metadata and labels only.
Every case uses a public HTTPS URL and immutable 40-character base and head
commits. It records repository identity, language, changed files, labelled
findings and anchors, severity, abstention, fixability, patch scope, test policy,
SPDX license, provenance, and limitations.

Collection must follow the existing controlled public collector's fail-closed
temporary-fetch policy. A collection runner may fetch exact commits into a
temporary workspace, verify their identities, compute bounded metadata, and then
remove that workspace. It must not persist source, install dependencies, execute
repository code, contact a model provider, or mutate a remote. If network or a
commit is unavailable, the case is `unavailable`; an offline result is never
substituted. The checked-in manifest is a label input, not evidence that a later
collection or repository test run succeeded.

The set intentionally includes Python, JavaScript/TypeScript, Go, Java, and
Rust plus clean changes, real defects, security and sensitive-data issues,
authorization, nullability/type, performance, ambiguity, correct abstention, and
AI-generated-defect cases. Most upstream cases use real direct-parent fixes and
therefore require abstention from claiming that the historical defect remains in
HEAD. The positive Java case pins a defective change whose label is supported by
a later upstream correction; that later evidence stays hidden until the reviewer
submits an outcome. Sanitized public CodeAtlas fixtures supply additional
controlled positive, ambiguous, and clean oracles; they are identified as synthetic in each case. Category
coverage is a study-design property, not a claim that the set represents
real-world prevalence or unknown-defect discovery.

A case records `NOASSERTION` rather than inventing a license when the pinned
repository revision has no declared SPDX license. Prescribed test commands are
policy, not pass evidence; none of the curated upstream commands were run during
metadata collection.

The v1 set has 18 cases:

| Source | Language | Validation role |
| --- | --- | --- |
| Public CodeAtlas sanitized fixtures (6) | Python | controlled clean, sensitive-data, authorization, ambiguity, abstention, and labelled AI-defect oracles |
| urllib3 (2) | Python | fixed concurrency and performance changes; correct abstention |
| p-limit | TypeScript | fixed declaration contract; correct abstention |
| fsnotify | Go | fixed Windows nil dereference; correct abstention |
| bat | Rust | fixed command injection; correct abstention |
| Gson | Java | fixed repeated-byte-order-mark parsing; correct abstention |
| Apache Commons Lang | Java | positive overflow-bound defect supported by a later upstream correction |
| Intentional2_repo | JavaScript | positive synthetic hardcoded-credential change without retaining its value |
| report | Python | positive authentication and authorization bypass |
| itsdangerous (2) | Documentation and YAML | clean documentation correction and ambiguous release-attestation policy |
| foundry-hosted-agents-fsi | YAML | positive defect with explicit public Copilot provenance and an immediate corrective child commit |

Labels are hypotheses to be independently adjudicated. A commit message can
support provenance but cannot by itself prove severity, exploitability, or a
correct fix. Tests are never reported passed unless they are actually run under
the case's declared policy.

## Human-in-the-loop protocol

Use protocol `human-review-v1` and benchmark `wp7-controlled-beta-v1`.
Randomize case order per reviewer while preserving the immutable case and run
identities. Do not show expected labels until the reviewer has submitted the
outcome. Record no reviewer name, free-form comment, source excerpt, prompt,
provider transcript, credential, or approval token. `reviewer_id_hash` is a
study-scoped SHA-256 pseudonym and must not be reused as an account identifier.

For each case:

1. Start from the pinned base/head pair and record the run and context hashes.
2. Start timing when the first finding is displayed.
3. Ask whether the reviewer can understand the finding.
4. Ask whether the reviewer can identify the affected code from the anchor.
5. Ask whether the reviewer understands the evidence.
6. Ask whether the reviewer can decide the finding's validity.
7. If a proposal exists, ask whether the reviewer understands it and chooses to
   approve, reject, request regeneration, request more context, or abstain.
8. Run validation only after the existing scoped approval boundary permits it.
9. Ask whether the reviewer can understand the validation result.
10. For a controlled failure, follow the documented recovery path and ask
    whether the reviewer can recover safely without mutation authority.
11. Stop timing and submit one strict `HumanReviewEvaluation` record.

The eight protocol capabilities use `yes`, `no`, or `not_evaluated`:

- understand the finding;
- identify affected code;
- understand evidence;
- decide finding validity;
- understand the proposed fix;
- choose approve, reject, or regenerate;
- validate the result;
- recover safely.

Finding and fix outcomes use only:

```text
accepted
rejected
needs_more_context
needs_regeneration
abstained
not_evaluated
```

Records are validated by `codeatlas.benchmark.product_validation` and the schema
`schemas/human-review-evaluation.schema.json`. Timing is optional and bounded.
`not_evaluated` stays outside evaluated-outcome denominators. A manual time
comparison is valid only when declared before the tool-assisted attempt and
uses the same task, person or matched cohort, repository state, and validation
scope.

## Adjudication and reporting

Use two independent label reviewers for disputed expected findings where
possible. Resolve disagreements without exposing one reviewer's answer before
the other submits. Preserve the original outcomes and record only bounded reason
codes for the adjudication result.

Report results by language, category, provider mode, and reviewer-experience
band when the subgroup denominator is large enough to avoid identifying an
individual. Always publish counts, exclusions, unavailable cases, confidence
intervals, environment/tool versions, and limitations. Do not claim controlled
beta readiness until the predefined safety boundaries pass and the remaining
product metrics are reviewed individually.
