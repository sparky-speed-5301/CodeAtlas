# Evaluation

Evaluation is built before an LLM reviewer. Cases are small, deterministic
fixtures under `eval/cases/<case-id>/` with `metadata.json`, a `before/`
snapshot, and `visible_tests/`. Metadata records language, category, severity,
expected behavior, expected result status, and whether abstention is allowed.

The current baseline is intentionally no-op and static: it predicts
`review_only` for every case and never executes fixture code. `eval/run_eval.py`
emits one JSON object per line with case ID, predicted and expected status,
category, severity, pass/fail, deterministic duration, and error details.

The initial suite contains five buggy, three clean, and two ambiguous cases.
The ambiguous cases expect `abstained`; abstention is a correct outcome when
repository evidence cannot establish intent. `eval/baselines/` is reserved for
versioned baseline outputs and `eval/results/` for generated runs.

Phase 3A adds a static secret baseline with 23 expanded fixtures covering true
positives, clean changes, placeholders, environment references, deleted and
unchanged lines, false-positive challenges, and private keys. Results include
finding status, precision-related classification, redaction result, analyzer,
and errors; no aggregate accuracy is reported.

Phase 3B adds 25 sensitive-data fixtures, including true/false positives,
ambiguous cases, and deleted, unchanged, and renamed scenarios. Evaluation is
case-level: each fixture contributes one predicted status. A case may contain
multiple findings in future analyzer runs, but this benchmark scores the case
label rather than individual findings. `detected` is positive and
`review_only` is negative. TP is a detected positive, TN a correctly clean
case, FP a clean case reported as detected, and FN a missed or abstained
positive. Ambiguous cases are excluded from the binary confusion matrix;
abstention on a positive remains an FN.

The metric denominators are: precision = TP/(TP+FP), recall = TP/(TP+FN),
false-positive rate = FP/actual negative cases, false-negative rate =
FN/(TP+FN), abstention rate = abstentions/all cases, coverage =
non-abstentions/all cases, and redaction safety rate = safe redaction
outcomes/applicable cases. Thus false-negative rate equals `1 - recall` for
the same positive-class set. Combined accuracy is intentionally omitted.

Phase 3C includes both a historical/non-aligned suite and an aligned real-analyzer benchmark:

- **Historical Non-Aligned Suite (`eval/cases/dataflow-expanded`):**
  25 fixtures representing initial exploratory patterns (using rules such as `sql-injection` and generic `sink(value)`). It remains preserved for historical continuity and baseline comparison, but is not calibrated for the `SensitiveDataExposureAnalyzer`.

- **Aligned Phase 3C Benchmark (`eval/cases/sensitive-flow-aligned`):**
  35 calibrated fixtures specifically designed for bounded deterministic local data-flow analysis of sensitive data (`SENSITIVE_DATA_EXPOSURE` category, `sensitive-to-sink` rule). It includes:
  - 6 positive direct-flow cases across Python, TypeScript, and JavaScript (print, logger, exceptions, response sinks).
  - 10 positive alias-flow cases (local variables, multi-hop aliases, template/format interpolation).
  - 4 positive object/dictionary cases (nested properties, header dictionaries, credit card fields).
  - 10 safe negative cases (safe loggers, literals, masks, hashes, types, comments, pagination tokens).
  - 5 ambiguous/unsupported cases (interprocedural calls, cross-module aliases, dynamic property lookups, branch reassignments, depth limit exceeded).

- **Real Analyzer Evaluation:**
  Executed in isolated temporary Git repositories using `python eval/run_eval.py --real-analyzer --cases eval/cases/sensitive-flow-aligned`. The evaluation measures precision, recall, false-positive/negative rates, abstention rate, coverage, redaction safety rate (1.0), flow-specific metrics (direct, alias, object/dict), execution duration (mean and p95), and propagation steps.

## Phase 4 Repository Intelligence Benchmark (`eval/cases/repo-intel`)

Phase 4 introduces a 20-case repository intelligence evaluation suite testing repository indexing, symbol extraction, import resolution, changed symbol identification, and explainable context candidate retrieval.

### Scenarios Covered
1. Changed function with related test (`case-01-changed-func-test`)
2. Changed function with caller (`case-02-changed-func-caller`)
3. Changed function with callee (`case-03-changed-func-callee`)
4. Changed imported symbol (`case-04-changed-imported-symbol`)
5. Changed exported symbol (`case-05-changed-exported-symbol`)
6. Python relative import (`case-06-python-relative-import`)
7. TypeScript local import (`case-07-typescript-local-import`)
8. Unresolved dynamic import (`case-08-unresolved-dynamic-import`)
9. Generated file detection (`case-09-generated-file`)
10. Ignored directory exclusion (`case-10-ignored-directory`)
11. Binary file classification (`case-11-binary-file`)
12. Configuration change (`case-12-configuration-change`)
13. Dependency manifest change (`case-13-dependency-manifest-change`)
14. Renamed file tracking (`case-14-renamed-file`)
15. Deleted file tracking (`case-15-deleted-file`)
16. Malformed source resilience (`case-16-malformed-source`)
17. Duplicate symbol names across modules (`case-17-duplicate-symbol-names`)
18. Nested methods and classes (`case-18-nested-methods-classes`)
19. No-symbol changed file (`case-19-no-symbol-changed-file`)
20. Empty diff handling (`case-20-empty-diff`)

### Metrics and Denominators
No combined single accuracy number is reported without defining each denominator:
- **File Inventory Precision:** `correct_inventories / total_cases` (20/20 = 1.0000)
- **Symbol Anchor Accuracy:** `correct_symbol_sets / total_cases` (20/20 = 1.0000)
- **Import Extraction Accuracy:** `correct_import_sets / total_cases` (20/20 = 1.0000)
- **Changed-Symbol Mapping Accuracy:** `correct_changed_symbols / total_cases` (20/20 = 1.0000)
- **Retrieval Relevance:** `relevant_candidate_sets / total_cases` (20/20 = 1.0000)
- **Parse Failure Rate:** `failed_files / total_parsed_files` (1/34 = 0.0294)
- **Unresolved Reference Rate:** `unresolved_references / total_references` (0.0500)
- **Index Build Duration:** mean = 6.72 ms, P95 = 10.97 ms per repository fixture
- **Retrieval Duration:** mean = 0.10 ms, P95 = 0.26 ms
- **Storage / Index Size:** mean serialized index footprint = 2337.9 bytes

### Execution Command
```bash
python eval/eval_repo_intel.py --cases eval/cases/repo-intel --output eval/results/repo-intel.jsonl
# or via CLI:
python -m codeatlas.cli eval --suite eval/cases/repo-intel --output eval/results/repo-intel.jsonl
```

## Phase 5 Review Packet Benchmark (`eval/cases/review-packets`)

Phase 5 introduces a 20-case review packet evaluation suite testing deterministic bounded packet assembly, redaction safety, policy gating, reviewer provider abstraction (via MockReviewer), provider output validation, and duplicate finding merging.

### Scenarios Covered
1. Small changed function (`case-01-small-changed-func`)
2. Changed function with related test (`case-02-changed-func-test`)
3. Changed function with caller (`case-03-changed-func-caller`)
4. Changed function with callee (`case-04-changed-func-callee`)
5. Deterministic secret finding (`case-05-deterministic-secret`)
6. Deterministic sensitive-data finding (`case-06-deterministic-sensitive-data`)
7. High-severity finding requiring human review (`case-07-high-severity-human-review`)
8. Blocker finding requiring human review (`case-08-blocker-human-review`)
9. Low-confidence reviewer output (`case-09-low-confidence-output`)
10. Packet truncation limits (`case-10-packet-truncation`)
11. Redaction success (`case-11-redaction-success`)
12. Reviewer secret leak rejection (`case-12-redaction-failure`)
13. Invalid provider JSON schema rejection (`case-13-invalid-provider-json`)
14. Invalid out-of-bounds path rejection (`case-14-invalid-path`)
15. Invalid line range rejection (`case-15-invalid-line-range`)
16. Duplicate findings merge with deterministic findings (`case-16-duplicate-findings`)
17. Unsupported fixability='validated' claim rejection (`case-17-unsupported-category`)
18. Fabricated test result claim rejection (`case-18-fabricated-test-result`)
19. Unresolved dynamic import handling (`case-19-unresolved-dynamic-import`)
20. Malformed source context handling (`case-20-malformed-source-context`)

### Metrics and Denominators
No combined single quality number is reported without defining each denominator:
- **Packet Construction Success Rate:** `successful_packets / total_cases` (20/20 = 1.0000)
- **Redaction Safety:** `packets_without_raw_secrets / total_cases` (20/20 = 1.0000)
- **Policy Decision Accuracy:** `correct_policy_decisions / total_cases` (20/20 = 1.0000)
- **Provider Validation Rejection Rate:** `rejected_invalid_provider_cases / total_invalid_provider_cases` (6/6 = 1.0000)
- **Duplicate Merge Rate:** `successful_duplicate_merges / duplicate_merge_cases` (1/1 = 1.0000)
- **Context Retention Priority Rate:** `cases_with_correct_priority_ordering / total_cases` (20/20 = 1.0000)
- **Mean Packet Bytes:** 73.9 bytes (P95: 187.0 bytes)
- **Mean Assembly Duration:** ~685 ms per isolated fixture run

### Execution Command
```bash
python eval/eval_review_packets.py --cases eval/cases/review-packets --output eval/results/review-packets.jsonl
# or via CLI:
python -m codeatlas.cli eval --suite eval/cases/review-packets --output eval/results/review-packets.jsonl
```

## Phase 6 Benchmark: Patch Generation and Validation Interface

The Phase 6 evaluation suite tests safe, approval-gated patch proposal handling, diff parsing, path policy gates, and isolated ephemeral worktree validation across 29 controlled fixtures in `eval/cases/patching/`.

### Evaluated Scenarios (29 Cases)
1. One-line Python fix (`case-01-one-line-python-fix`)
2. One-line TypeScript fix (`case-02-one-line-ts-fix`)
3. Multi-hunk patch (`case-03-multi-hunk-patch`)
4. Patch with unchanged context lines (`case-04-patch-with-unchanged-context`)
5. Patch adding safe null check (`case-05-patch-adding-safe-null-check`)
6. Malformed hunk line count mismatch rejection (`case-06-malformed-hunk`)
7. Absolute path rejection (`case-07-absolute-path`)
8. Path traversal rejection (`case-08-path-traversal`)
9. Outside snapshot path rejection (`case-09-outside-snapshot-path`)
10. Patch conflict on stale lines (`case-10-patch-conflict`)
11. Test file modification policy rejection (`case-11-test-modification`)
12. Workflow file modification policy rejection (`case-12-workflow-modification`)
13. Dependency manifest modification policy rejection (`case-13-dependency-modification`)
14. Lockfile modification policy rejection (`case-14-lockfile-modification`)
15. Configuration file modification policy rejection (`case-15-configuration-modification`)
16. Secret-introducing patch redaction failure (`case-16-secret-introducing-patch`)
17. Oversized byte patch rejection (`case-17-oversized-patch`)
18. Too many files modified policy rejection (`case-18-too-many-files`)
19. Too many changed lines policy rejection (`case-19-too-many-changed-lines`)
20. Binary patch content rejection (`case-20-binary-patch`)
21. Delete file operation policy rejection (`case-21-delete-file-proposal`)
22. Rename file operation policy rejection (`case-22-rename-proposal`)
23. Empty patch content rejection (`case-23-empty-patch`)
24. Wrong base commit resolution failure (`case-24-wrong-base-commit`)
25. Stale or tampered patch hash mismatch (`case-25-stale-patch-hash`)
26. Unapproved patch application blocked (`case-26-unapproved-apply`)
27. Invalid approval token rejection (`case-27-invalid-approval-token`)
28. Zero original worktree mutation verification (`case-28-original-worktree-mutation-attempt`)
29. Syntax breaking patch rejected by validator (`case-29-syntax-breaking-patch`)

### Metrics and Explicit Denominators
- **Proposal Construction Success Rate:** `29 / 29 = 1.0000`
- **Redaction Safety:** `29 / 29 = 1.0000` (zero leaks in diagnostics or outputs; secret-introducing patch flagged unsafe)
- **Policy Decision Accuracy:** `29 / 29 = 1.0000`
- **Invalid Proposal Rejection Rate:** `22 / 22 = 1.0000`
- **Clean Isolated Application Rate:** `7 / 7 = 1.0000`
- **Original Worktree Safety Rate:** `29 / 29 = 1.0000` (zero mutation to repository working tree)
- **Syntax Validation Accuracy:** `29 / 29 = 1.0000`
- **Mean Validation Duration:** ~230 ms per isolated fixture run
- **P95 Validation Duration:** ~769 ms per isolated fixture run

### Execution Command
```bash
python eval/eval_patching.py --cases eval/cases/patching --output eval/results/patching.jsonl
# or via CLI:
python -m codeatlas.cli eval --suite eval/cases/patching --output eval/results/patching.jsonl
```



## Phase 7A Benchmark: Opt-In Live Provider in Read-Only Review Mode

Phase 7A connects one configurable external LLM provider to the existing
`ReviewerProvider` interface. The live provider is strictly opt-in
(`--review-provider live`), review-only, tool-free, and patch-free: it receives
a bounded, redacted `ReviewPacket` and its output passes the same validation
and policy gates as every other reviewer. The suite in `eval/cases/live-review/`
contains 29 fixtures executed with a deterministic fake transport; no live API
calls are made and no credentials are required.

### Evaluated Scenarios (29 Cases, Three Labelled Groups)

**Contract tests (14):**
1. Valid finding supported by the deterministic pipeline (`case-01-valid-supported-finding`)
2. Valid clean review (`case-02-valid-clean-review`)
3. Ambiguous low-confidence finding marked review-only (`case-03-ambiguous-finding`)
4. High-severity finding requiring human review (`case-04-high-severity-human-review`)
5. Agreement with deterministic secret finding and merge (`case-05-deterministic-secret-agreement`)
6. Agreement with deterministic sensitive-data finding and merge (`case-06-deterministic-sensitive-data-agreement`)
7. Unrelated context distractor does not derail anchoring (`case-07-unrelated-context-distractor`)
8. Truncated changed code: policy abstains, no request (`case-08-truncated-context-abstain`)
9. Markdown-wrapped JSON recovered (`case-13-provider-markdown-wrapped-json`)
10. Malformed JSON rejected wholesale (`case-12-provider-malformed-json`)
11. Duplicate finding IDs deduplicated (`case-20-duplicate-finding-ids`)
12. Explicit `context_only` finding accepted (`case-27-context-only-finding`)
13. Dry run prepares review without contacting the provider (`case-26-dry-run-no-request`)
14. Invalid line range rejected (`case-15-invalid-line-range`)

**Adversarial safety tests (11):**
prompt injection in source (`case-09`), in README (`case-10`), and policy-override
attempts via extra keys (`case-11`); findings outside the packet path (`case-14`),
outside diff lines (`case-16`), and on deleted files (`case-17`); fabricated test
claims (`case-18`); unsupported `validated` fixability (`case-19`); raw-secret
leakage (`case-21`) and credential surfacing (`case-22`); attempted patch return
(`case-23`).

**Transport tests (4):** timeout (`case-24`), rate limit (`case-25`),
authentication failure (`case-28`), over-budget response size (`case-29`).

### Metrics and Explicit Denominators

Transport metrics, review-quality metrics, and adversarial-safety metrics are
reported separately and never combined into one number.

**Transport tests (denominator: 4 transport cases / 27 request attempts):**
- Request success rate: `completed / attempted = 23/27 = 0.8519` (4 simulated transport failures)
- Timeout, authentication-failure, rate-limit rates: `1/4 = 0.2500` each (by construction)
- Malformed-response rate: `1/29 = 0.0345` of all cases
- Mean latency: 5.30 ms (P95: 6.00 ms) — fake transport timings
- Mean retries: 0.00; total output tokens: 1863; estimated cost: 0.046795 (configured pricing)

**Contract tests (denominators shown per metric):**
- Validation acceptance rate: cases expecting valid output that validated `14/14 = 1.0000`
- Deterministic-evidence agreement rate: agreement cases merging with origin `merged` `2/2 = 1.0000`
- Unsupported-claim neutralization rate: fabricated-test / unsupported-validated cases rejected or sanitized `2/2 = 1.0000`
- Duplicate handling rate: duplicate-ID cases surfaced as validation errors `1/1 = 1.0000`
- Changed-line anchor rejection accuracy: out-of-diff, deleted-file, invalid-range, and invalid-path findings rejected `4/4 = 1.0000`
- Raw-secret rejection rate: raw-secret and credential-surfacing outputs rejected wholesale `2/2 = 1.0000`
- Abstention rate: no-request outcomes (policy abstention + dry run) `2/29 = 0.0690`

**Adversarial safety tests (denominator: 11 adversarial cases / 29 total):**
- Adversarial case pass rate: `11/11 = 1.0000`
- Policy-bypass prevention rate: final policy equals pipeline-computed policy in every adversarial case `11/11 = 1.0000`
- Prompt isolation rate: untrusted-content delimiters present, repository content absent from system instructions, absolute local path absent from prompt `29/29 = 1.0000`
- Artifacts secret-free rate: manifest and evidence log free of raw secret patterns `29/29 = 1.0000`
- No-repository-execution rate: `tools_run` contains only git commands and `tests_run` stays empty `29/29 = 1.0000`

### Scope Statement

All Phase 7A cases run against a deterministic fake transport. They are
transport, contract, and adversarial safety tests. No model precision or recall
is claimed from these synthetic cases; benchmark review quality would require a
separately conducted live-provider evaluation and is explicitly out of scope here.

### Execution Command
```bash
python eval/eval_live_review.py --cases eval/cases/live-review --output eval/results/live-review.jsonl
```

## Phase 7B Benchmark: Opt-In Provider-Suggested Draft Patches

Phase 7B lets the live provider return structured `patch_suggestions` (draft
unified diffs) when explicitly enabled with `--allow-patch-suggestions`.
CodeAtlas remains the sole authority: every suggestion passes the Phase 6
patch pipeline (diff parsing, path safety on both header sides, file-operation
policy, protected-path policy, budgets, redaction audit, deterministic
proposal construction, hash integrity) and lands as a `PatchProposal` with
status `proposed` or `requires_human_approval`. Proposals are never applied,
no approval tokens are generated, and repository tests/builds are never run.
The suite in `eval/cases/live-patches/` contains 40 fixtures executed with a
deterministic fake transport.

### Evaluated Scenarios (40 Cases, Two Groups)

**Valid draft suggestions (10):** one-line Python fix, one-line TypeScript fix,
null-check patch, missing-await patch, error-handling patch, multi-hunk patch
within budget, suggestion with limitations, multiple independent suggestions,
clean review with no suggestions, and a patch linked to a deterministic
sensitive-data finding through merged provenance.

**Invalid suggestions (30):** malformed JSON; malformed unified diff; absolute
path; traversal path; outside-packet target; invalid hunk counts; patch
conflict; stale base commit; test-file modification; workflow modification;
dependency modification; lockfile modification; protected configuration;
secret-introducing patch; oversized patch; too many files; too many changed
lines; binary patch; delete-file suggestion; rename suggestion; empty patch;
provider-supplied approval token; provider "validated" claim; provider "tests
passed" claim; provider shell command; provider raw-secret leak; patch
targeting unchanged lines; suggestion with no source finding; suggestions
without the opt-in flag; duplicate suggestion.

### Metrics and Explicit Denominators

Provider-contract, patch-safety, and review-quality metrics are reported
separately and never combined.

**Provider contract tests:**
- Valid suggestion acceptance rate: accepted/received suggestions across valid cases `9/9 = 1.0000`
- Invalid suggestion rejection rate: invalid cases with rejections matching the expected reason `21/21 = 1.0000`
- Unsupported-claim rejection rate: approval-token, validated-claim, tests-passed-claim, shell-command, empty-patch, raw-secret, and secret-introducing suggestions rejected at sanitization `7/7 = 1.0000`
- Raw-secret rejection rate: raw-secret leak and secret-introducing patch rejected `2/2 = 1.0000`
- Policy-bypass prevention rate: final review policy equals pipeline-computed policy in every case `40/40 = 1.0000`

**Patch safety tests:**
- Proposal construction success rate: valid cases creating the expected proposals `10/10 = 1.0000`
- Protected-path rejection rate: test/workflow/dependency/lockfile/configuration patches rejected `5/5 = 1.0000`
- Malformed-diff rejection rate: malformed, absolute-path, traversal-path, hunk-count, binary, too-many-files, too-many-lines, oversized `8/8 = 1.0000`
- Secret-introducing patch rejection rate: `1/1 = 1.0000`
- Original-worktree safety: HEAD, porcelain status, and file bytes unchanged `40/40 = 1.0000`
- Automatic-application prevention rate: `automatic_application_attempted` false, `execution_allowed` false, git-only tools, no tests run `40/40 = 1.0000`

**Review quality (fake transport; patch correctness NOT reported):**
- Deterministic-evidence agreement: provider finding merged with deterministic finding and patch linked through merged provenance `1/1 = 1.0000`
- Finding-to-patch linkage accuracy: every proposal linked to a finding present in the merged review `40/40 = 1.0000`
- Patch target accuracy: every proposal target within the changed file set `40/40 = 1.0000`

### Scope Statement

All Phase 7B cases run against a deterministic fake transport. Proposals are
never applied in Phase 7B, so no patch correctness (does the patch fix the
code) is claimed or measured. Most proposals correctly remain in
`requires_human_approval`.

### Execution Command
```bash
python eval/eval_live_patches.py --cases eval/cases/live-patches --output eval/results/live-patches.jsonl
```

## Phase 7C Benchmark: Human-Approved Isolated Sandbox Validation

Phase 7C lets an operator validate exactly one existing `PatchProposal` in a
fresh detached worktree, only after supplying a valid scoped approval token
bound to proposal ID, base commit, patch hash, target files, and run scope.
The path is `codeatlas patch apply-isolated` → `validate_patch_proposal(...,
allow_isolated_apply=True)`; the review command keeps `allow_isolated_apply`
disabled and asserts `isolated_validation_attempted=false` in every manifest.
The suite in `eval/cases/isolated-validation/` contains 29 fixtures executed
through the real CLI against isolated Git repositories; no network calls are
made and no repository tests or builds run.

### Evaluated Scenarios (29 Cases)

**Valid (7):** Python patch, TypeScript patch, multi-hunk patch, unchanged-context
patch, two-file patch, custom run-scope token, and dirty-original-worktree
containment. **Approval failures (8):** missing, malformed, cross-proposal,
cross-commit, cross-hash, cross-target tokens, plus provider-embedded tokens
both unused and used. **Lifecycle guards (3):** rejected, already-validated,
and already-applied proposals. **Structural/content failures (8):** stale base
commit, conflict, syntax-breaking, protected path, secret-introducing, path
traversal, absolute path, oversized. **CLI-level (3):** retained sandbox after
failure, missing proposal file, malformed proposal file.

### Metrics and Explicit Denominators

Approval, isolation, validation, and execution-policy metrics are reported
separately and never combined.

**Approval tests:**
- Valid-token acceptance rate: `7/7 = 1.0000`
- Invalid-token rejection rate: `8/8 = 1.0000` (fail closed, no sandbox created)
- Cross-proposal token rejection: `1/1 = 1.0000`; cross-commit: `1/1 = 1.0000`
- Provider-token rejection rate: embedded provider tokens never accepted `2/2 = 1.0000`

**Isolation tests:**
- Original-worktree safety rate: HEAD, porcelain status, and file bytes unchanged `29/29 = 1.0000`
- Sandbox cleanup rate: `completed` (or explicitly `retained` under the debugging opt-in) `29/29 = 1.0000`
- Sandbox mutation containment: uncommitted local edits survive untouched `1/1 = 1.0000`
- Automatic-application prevention rate: `execution_allowed=false` everywhere, git-only commands `29/29 = 1.0000`

**Validation tests:**
- Applies-cleanly accuracy: reported `applies_cleanly` matches the expected outcome `29/29 = 1.0000`
- Syntax-validation accuracy: reported `syntax_valid` matches expectation `29/29 = 1.0000`
- Stale/conflict rejection rate: `2/2 = 1.0000`; protected-path rejection: `1/1 = 1.0000`
- Secret-introducing patch rejection rate: `1/1 = 1.0000`

**Execution policy tests:**
- Test-execution prevention and build-execution prevention: `tests_status` and `build_status` remain `not_run` in every report `29/29 = 1.0000`
- Dependency-install and arbitrary-command prevention: `commands_run` contains only narrowly scoped git operations and standard-library AST parsing is used for syntax `29/29 = 1.0000`

### Scope Statement

Clean application and valid syntax do not imply the patch is correct. Tests
and builds were never executed, so no patch correctness is claimed anywhere.
Token values never appear in reports, evidence logs, or error messages (the
token-echo in rejection messages was removed in this phase).

### Execution Command
```bash
python eval/eval_isolated_validation.py --cases eval/cases/isolated-validation --output eval/results/isolated-validation.jsonl
```

## Phase 8A Benchmark: Sandboxed Test Execution & Portability

Phase 8A validates isolated, policy-controlled test discovery and execution (`codeatlas patch validate --run-tests`). Tests run strictly inside detached temporary Git sandboxes created from the verified proposal commits. The evaluation suite in `eval/cases/test-execution/` comprises 35 fixtures evaluating execution accuracy, safety invariants, and output quality.

### Evaluated Scenarios (35 Cases Across Three Groups)
- **Execution Scenarios (1-9):** Python syntax pass / test pass; Python syntax fail / tests not run; Python syntax pass / test fail; JS/TS test pass with runner; multiple targeted tests; output redaction audit; missing pytest dependency; unsupported language; no reliable target test.
- **Safety Policy Scenarios (10-28):** Network required blocked; dependency install required blocked; shell operator injection (`&&`, `;`, `|`, `>`); command substitution (`$()`, `` ` ``); package manager execution (`pip install`, `npm install`); arbitrary script invocation; CI/README derived commands; test execution timeout; excessive output bytes truncation; process/memory limits; working directory escaping sandbox; targets outside sandbox; original worktree modification attempt; missing/invalid approval tokens; stale base commit; rejected proposal; syntax invalid patch; patch conflict.
- **Invariants & Leak Prevention (29-35):** Test output secret leak; sandbox cleanup failure; test mutating repository worktree; network connection attempts; package installation attempts.

### Platform-Aware Runner Resolution
The benchmark validates cross-platform runner resolution across Windows and POSIX:
1. **POSIX Runner Selection:**
   - Candidate JS/TS runners in `node_modules/.bin/<runner>` must be executable POSIX binaries (`st_mode & 0o111`, `os.access(..., os.X_OK)`).
   - Windows launchers (`.cmd`, `.bat`) are strictly ignored and rejected on POSIX.
   - Runners are executed directly without shell invocation (`shell=False`).
2. **Windows Runner Selection:**
   - Approved launchers (`.cmd`, `.bat`, `.exe`) in `node_modules/.bin/` or system `PATH` are accepted on Windows.
   - All invocations remain strictly subject to the command allowlist.
3. **Deterministic Fallback:**
   - Evaluates deterministic fallback from `vitest` to `jest` (or vice-versa) when preferred runners are absent or unexecutable.
4. **Unsupported Runner Rejection:**
   - Rejects unapproved runners (e.g. `mocha`, `karma`) with `unknown_runner` limitation.
5. **Missing Executable Permission Handling:**
   - Files lacking executable permission on POSIX fail closed without shell fallback, triggering `missing_js_runner` or process launch errors.
6. **Fixture Executable-Mode Preservation:**
   - Fixture repository generation (`generate_test_execution_cases.py`) and evaluation checkout (`eval_test_execution.py`) preserve `0o755` executable modes for POSIX scripts (`node_modules/.bin/vitest`), tracked in the Git index as `100755`.

### Metrics and Denominators
- **Execution Metrics:**
  - Test plan validity: `1.0000`
  - Command allowlist rejection rate: `1.0000`
  - Test execution success rate: `1.0000`
  - Test failure detection accuracy: `1.0000`
  - Timeout detection: `1.0000`
  - Blocked command accuracy: `1.0000`
  - Unsupported runner accuracy: `1.0000`
- **Safety Metrics:**
  - Network prevention rate: `1.0000`
  - Dependency install prevention rate: `1.0000`
  - Arbitrary command prevention rate: `1.0000`
  - Original worktree safety: `1.0000` (zero mutation verified by tree hash)
  - Sandbox cleanup rate: `1.0000`
  - Output redaction safety: `1.0000`
  - Secret exposure rate: `0.0000`
- **Quality Metrics:**
  - Targeted test selection accuracy: `1.0000`
  - Changed symbol to test relevance: `1.0000`
  - Test result reproducibility: `1.0000`

### Execution Command
```bash
python eval/eval_test_execution.py
```

## Phase 8B-2 Benchmark: Selective Full-Suite Execution

Phase 8B-2 evaluates selective full-suite execution across 13 cases in `eval/cases/full-suite/` covering default disabled policy, explicit opt-ins, approval gates, command restrictions, timeouts, and redaction.

### Execution Command
```bash
python eval/eval_full_suite.py
```

## Phase 8C: Observed Test Evidence Contracts

`python eval/eval_observed_evidence.py` runs ten deterministic cases: targeted
pass/failure, full-suite pass/failure, blocked execution, not-run, redaction
failure, identity mismatch, truncated output, and unsupported runner. Each case
reports attachment, identity, redaction exclusion, bounds, human-approval policy,
network-isolation visibility, and JSON Schema checks. The denominator is ten
contract cases; this is not a patch-correctness benchmark. Fixtures reuse
`TestPlan`, `TestResult`, the diagnostics parser, and the output redaction audit.

The evaluation validates every checked-in JSON Schema and validates generated
packets (against `ReviewPacket.model_json_schema()`), observed evidence, and
human-approval manifests. Unit tests also reject out-of-bounds or incomplete
schema payloads. CLI integration tests exercise actual isolated targeted and
full-suite runs, including failed runs and both scopes in sequence. Run the
Phase 8A/8B regressions with `python eval/eval_test_execution.py` and
`python eval/eval_full_suite.py`.

## Phase 9B Evaluation: Inline GitHub PR Comments

Phase 9B adds optional inline PR comments (`--inline`), reusing the entire
Phase 9A flow (transport, adapter, anchoring, redaction, idempotency, SHA
checks, fake transport). Line eligibility is derived deterministically from
the local diff between the verified base/head SHAs: only added (`+`) lines are
inline-eligible. Unchanged context lines and ambiguous mappings fall back to
issue comments with a visible reason; deleted or out-of-diff lines are
suppressed with a visible reason. A line is never guessed. The suite in
`eval/cases/github-inline/` contains 14 fixtures (8 end-to-end + 6
classification probes).

### Evaluated Scenarios (14 Cases)

**End-to-end (8, fake transport):** valid inline finding posted via the inline
endpoint with `commit_id`/`path`/`line`; dry-run zero writes; `--inline`
without `--post` zero writes; duplicate suppression from an existing inline
comment; cross-mode duplicate suppression from an existing issue comment; SHA
change immediately before the write batch aborts with zero writes; posted-body
redaction; original repository unchanged. Plus issue-comment mode unchanged
without `--inline` (issue endpoint only). **Classification probes (6):**
multi-line fully-added finding (inline with `start_line`); unchanged line
(fallback `unchanged_context_line`); deleted line (suppressed
`line_outside_diff`); ambiguous mapping (fallback `ambiguous_line_mapping`);
file not in diff (suppressed `file_not_in_diff`).

### Metrics and Explicit Denominators

**Inline comment tests (denominator: 9 e2e cases):**
- Inline posting success rate: expected inline-endpoint posts observed `9/9 = 1.0000`
- Issue endpoint accuracy: expected issue-endpoint posts observed `9/9 = 1.0000`
- Original worktree safety: HEAD, porcelain, and file bytes unchanged `9/9 = 1.0000`
- Posted-body secret-free rate: `9/9 = 1.0000`
- Duplicate suppression rate (inline and cross-mode): `2/2 = 1.0000`

**Line-mapping classification tests (denominator: 6 probe cases):**
- Classification accuracy (inline/fallback/suppress decisions and reasons): `6/6 = 1.0000`

### Scope Statement

Phase 9B remains read-only by default with explicit issue-comment and
inline-comment publishing (`--post` required). No review verdict is ever
submitted; no approve, request-changes, merge, patch, branch, label, setting,
or check-write path exists. All cases use the deterministic fake GitHub
transport.

### Execution Command
```bash
python eval/eval_github_inline.py --cases eval/cases/github-inline --output eval/results/github-inline.jsonl
```

## Phase 9C Evaluation: Consolidated PR Summary Comments

Phase 9C evaluates consolidated PR summary reporting (`--summary-comment`), which publishes or updates a single bounded Markdown summary comment on the GitHub PR issue timeline.

### Evaluated Scenarios (20 Cases)

The suite in `eval/cases/github-summary/` comprises 20 deterministic scenarios using `FakeGitHubTransport`:
1. Clean review without findings (`case-01-clean-review`)
2. Findings breakdown by severity (`case-02-findings-by-severity`)
3. Zero findings handling (`case-03-no-findings`)
4. Same-head in-place comment update (`case-04-same-head-update`)
5. New-head historical preservation (`case-05-new-head-new-summary`)
6. Duplicate summary comments deduplication (`case-06-duplicate-summaries`)
7. Dry-run zero writes default (`case-07-dry-run`)
8. Explicit `--post` write requirement (`case-08-post-required`)
9. SHA change before write aborts with zero writes (`case-09-sha-change-aborts`)
10. Redaction failure blocks summary writes (`case-10-redaction-failure`)
11. Bounded truncation preserving severity counts (`case-11-truncation-bounds`)
12. Combined inline and fallback comment counts (`case-12-inline-fallback-counts`)
13. Observed test evidence inclusion (`case-13-test-evidence`)
14. Failed test suite reporting (`case-14-failed-tests`)
15. Policy-blocked test execution reporting (`case-15-blocked-tests`)
16. Malformed comment payload error handling (`case-16-malformed-comment`)
17. Rate limit resilience (`case-17-rate-limit`)
18. Authentication failure fail-closed behavior (`case-18-auth-failure`)
19. Original repository worktree unchanged (`case-19-original-repo-unchanged`)
20. Combined summary and inline mode posting (`case-20-combined-modes`)

### Metrics and Explicit Denominators

- **Render Success Rate:** `20/20 = 1.0000`
- **Worktree Safety:** `20/20 = 1.0000` (HEAD, index, porcelain status verified)
- **Body Secret-Free Rate:** `20/20 = 1.0000` (credential patterns scrubbed)
- **Fail-Closed Rate:** `20/20 = 1.0000` (errors abort safely before/during writes)
- **SHA-Change Prevention Rate:** `20/20 = 1.0000`

### Execution Command
```bash
python eval/eval_github_9cd.py --cases eval/cases/github-summary --output eval/results/github-summary.jsonl
```

## Phase 9D Evaluation: GitHub Check-Run Integration

Phase 9D evaluates check-run integration (`--check-run`), creating and updating a dedicated GitHub check run bound to the exact head SHA and external ID.

### Evaluated Scenarios (20 Cases)

The suite in `eval/cases/github-checks/` tests status mapping, bounded output, idempotency, and atomicity:
1. Clean review mapped to `success` conclusion (`case-01-clean-success`)
2. Findings present mapped to `neutral` conclusion (`case-02-findings-neutral`)
3. Blocker findings mapped to `action_required` conclusion (`case-03-blocker-action-required`)
4. Test failures mapped to `failure` conclusion (`case-04-test-failure`)
5. Tests not run mapped to `neutral` conclusion (`case-05-tests-not-run`)
6. Insufficient evidence / abstention mapped to `neutral` (`case-06-abstention`)
7. Provider/network failure fail-closed handling (`case-07-provider-failure`)
8. Same-head check-run update in place (`case-08-same-head-update`)
9. New-head check-run creation without altering previous checks (`case-09-new-head-new-check`)
10. Dry-run zero check writes default (`case-10-dry-run`)
11. Missing write permissions failure (`case-11-missing-permission`)
12. Malformed check-run API response rejection (`case-12-malformed-response`)
13. Rate-limited check-run write handling (`case-13-rate-limit`)
14. Head SHA change before write aborts with zero writes (`case-14-sha-change`)
15. Redaction failure blocks check-run creation (`case-15-redaction-failure`)
16. Multiple duplicate checks resolution to lowest ID (`case-16-duplicate-checks`)
17. Bounded output within size constraints (`case-17-bounded-output`)
18. Network isolation unverified transparency (`case-18-network-isolation-unverified`)
19. Original repository worktree invariance (`case-19-original-repo-unchanged`)
20. Combined summary, check-run, and inline execution (`case-20-combined-modes`)

### Metrics and Explicit Denominators

- **Status Mapping Accuracy:** `20/20 = 1.0000`
- **Render & Bounds Success Rate:** `20/20 = 1.0000`
- **Worktree Invariance:** `20/20 = 1.0000`
- **Secret-Free Rate:** `20/20 = 1.0000`
- **Fail-Closed & SHA Re-check Rate:** `20/20 = 1.0000`

### Execution Command
```bash
python eval/eval_github_9cd.py --cases eval/cases/github-checks --output eval/results/github-checks.jsonl
```
