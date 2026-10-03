# Architecture

## Planned review flow

`CLI -> immutable snapshot/worktree -> language detection -> repository intelligence & symbol indexing -> deterministic tools -> retrieval -> bounded review packet assembly -> policy gate -> reviewer provider -> output validation -> finding merge & ranking -> patch proposal & isolated validation -> evidence manifest`

Phase 6 implements safe, approval-gated patch proposal modeling, path protection policies, and isolated temporary worktree validation (`codeatlas.patching`).
Phase 5 implements deterministic review packet assembly, explicit policy gating, reviewer provider abstraction, and explainable finding merging (`codeatlas.review`).
Phase 4 implements a deterministic repository intelligence and symbol context layer (`codeatlas.repository`).
Phase 3B/3C implements deterministic evidence analysis for `HARD_CODED_SECRET`
and `SENSITIVE_DATA_EXPOSURE`.
No live LLM calls, external network dependencies, or arbitrary code execution exist.

## Boundaries

- **CLI:** validates configuration, selects a repository, configures optional repository indexing (`--index-repository`), review packet assembly (`--assemble-review-packet`), reviewer provider (`--review-provider mock`), and patch subcommands (`codeatlas patch inspect`, `codeatlas patch validate`, `codeatlas patch apply-isolated`); it does not hide failures.
- **Snapshot:** review uses the resolved head commit in a temporary detached
  Git worktree. The author's branch is read-only from CodeAtlas' perspective.
- **Patch Proposal & Validation Layer (`codeatlas.patching`):**
  - **Patch Proposal Modeling (`models.py`):** Typed `PatchProposal`, `PatchFile`, and `PatchHunk` models tracking rationale, expected behavior, risk level, status, patch hash, and redaction audit.
  - **State Machine & Lifecycle:** Governs explicit transitions (`proposed` -> `requires_human_approval` -> `approved` -> `applied_in_isolated_worktree` -> `validated`). Invalid transitions raise clear errors. Prohibits self-approval by providers or mock reviewers.
  - **Scoped Approval Tokens (`proposal.py`):** Deterministic, cryptographically scoped tokens bound to proposal ID, base commit, patch hash, and allowed paths (`CAT-APP-...`). Prohibits application without valid matching token.
  - **Safe Unified Diff Parsing (`parser.py`):** Parses unified diffs in memory without filesystem writes. Enforces strict hunk line counts, operation classification (`modify`, `add`, `delete`, `rename`), path normalization, absolute path rejection, path traversal (`..`) blocking, null byte rejection, and size bounds (`max_files`, `max_changed_lines`, `max_patch_bytes`).
  - **Protected Path Policy Engine (`policy.py`):** Rejects unauthorized modifications to test files, CI/CD workflows, package manager lockfiles, dependency manifests, and repository configuration unless explicitly allowed.
  - **Isolated Sandbox Execution (`sandbox.py` & `apply.py`):** Applies patches exclusively within ephemeral detached temporary worktrees (`codeatlas-sandbox-`). Verifies zero mutation of the user's original repository working tree before and after sandbox execution. Performs memory-safe AST syntax validation on modified Python files. Cleans up and prunes all sandbox worktrees even upon failure.
  - **Mock Fixer (`mock.py`):** Offline, deterministic `MockFixer` supporting test modes: `valid_patch`, `malformed_patch`, `path_traversal`, `outside_snapshot_path`, `conflict_patch`, `syntax_breaking_patch`, `test_modifying_patch`, `workflow_modifying_patch`, `dependency_modifying_patch`, `secret_introducing_patch`, `oversized_patch`, and `empty_patch`.
- **Bounded Review Packet (`codeatlas.review.packet`):**
  - **Assembly & Budgeting:** Enforces strict deterministic bounds on `max_context_files`, `max_lines_per_file`, `max_total_context_lines`, `max_packet_bytes`, and `max_findings`.
  - **Prioritized Retention:** Changed code first, deterministic findings second, directly related symbols next, tests/configuration after that. Excluded candidates and line truncations are explicitly tracked with reasons.
  - **Packet-Level Redaction Audit:** Scrubs raw secret values using verified regex patterns and secret markers before serialization; verifies `raw_value_matches == 0`.
- **Policy Engine (`codeatlas.review.policy`):**
  - Evaluates explicit gating rules: redaction failure blocks review; blocker and high-severity security findings require human review; truncated changed code triggers policy abstention; low-confidence findings are downgraded to review-only; sensitive workflow/key changes require human approval.
- **Reviewer Provider Abstraction (`codeatlas.review.provider` & `codeatlas.review.mock`):**
  - Defines typed `ReviewerProvider` protocol and `ReviewerResult`.
  - Provides deterministic, offline `MockReviewer` with configurable modes for testing and evaluation (`clean`, `echo_changed`, `invalid_json`, `invalid_path`, `invalid_line_range`, `duplicate_finding`, `unsupported_validated`, `fabricated_test`, `low_confidence`, `secret_leak`).
- **Provider Output Validator (`codeatlas.review.validator`):**
  - Strictly validates provider findings against Pydantic schema, repository snapshot path bounds, valid line ranges, diff-scoping (unless `context_only`), unexecuted test claims, unsupported `validated` fixability claims, and raw secret absence.
- **Finding Merge & Ranking (`codeatlas.review.ranking`):**
  - Merges overlapping deterministic analyzer and reviewer findings giving deterministic evidence precedence.
  - Ranks findings using explainable weights: severity (40%), evidence strength (30%), provider confidence, diff-scoping boost, and limitations penalty.
- **Repository Intelligence (`codeatlas.repository`):**
  - **File Inventory & Scanner:** scans repository files excluding default paths (`.git/`, `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `dist/`, `build/`, `coverage/`, `vendor/`) and custom paths configured in `.codeatlas.yml`. Classifies files into source, test, configuration, documentation, build, asset, and other.
  - **Parsers & Fallbacks:**
    - Python: Uses the standard library `ast` module (safe syntax-tree parsing, 1.0 confidence, never executes code).
    - JavaScript / TypeScript: Uses a structured regex and brace-matched block parser (0.95 confidence for clean declarations, 0.60 for partial fallbacks).
    - Malformed files are recorded as `IndexDiagnostic` with `level="error"` and `reason="syntax_error"`; parse failures do not crash the review.
  - **Symbols:** Extracts functions, async functions, classes, methods, module variables, interfaces, and type aliases. Generates deterministic, stable, secret-free symbol IDs (`sym::{file}::{kind}::{scope}::{name}`).
  - **Imports:** Extracts static imports, aliases, and relative imports. Resolves local import paths safely against inventoried files. Dynamic imports (`import(...)`, `require(variable)`) are explicitly tagged as dynamic and recorded as unresolved diagnostics.
  - **References & Dependency Graph:** Builds a safe intra-file reference graph and resolves references to locally defined symbols or imported local targets. Unresolved references record explicit reasons (`external_package`, `dynamic_import`, `ambiguous_name`, `parser_limitation`, `unsupported_syntax`).
  - **Index Freshness & Serialization:** Indexes are bound to the commit SHA (`commit_sha`) and can be serialized to JSON without losing fidelity.
  - **Context Retrieval:** `retrieve_context(index, changed_files, changed_symbols, max_candidates)` ranks candidates using explainable signals: `changed_symbol` (1.00), `changed_file` (0.95), `test_reference` (0.88), `caller` (0.82), `callee` (0.78), `imported_by` (0.70), `imports` (0.68), `configuration` (0.55-0.90), and `path_proximity` (0.45).
- **Analyzers:** `HardcodedSecretAnalyzer` and `SensitiveDataExposureAnalyzer` read only the detached head
  snapshot and only added/new-side lines from the Git diff model. Python, JavaScript, and TypeScript are supported.
- **Evidence:** findings and index diagnostics reference file/line ranges, evidence, consulted tools
  and tests, limitations, and provenance.
- **Validation:** a patch transitions to `validated` only after configured
  checks pass in an isolated runner. A failed or unavailable check is visible.
- **Evaluation:** `eval/cases` stores small fixtures and metadata; the baseline
  runner is static and never imports or executes fixture code.

## Data flow and failure handling

Inputs are untrusted data, including source, comments, dependency metadata,
workflows, and tool output. Each stage reports analyzed, skipped, unresolved,
and failed work into a run manifest. Insufficient evidence produces
`abstained`, not a guessed blocker.

## Verification & Test Execution Layer (Phase 8A, `codeatlas.verification`)

Phase 8A adds isolated, policy-gated test discovery and execution for validated patch proposals (`codeatlas patch validate --run-tests`).

### Architecture and Components
- **Test Discovery (`discovery.py`):** Automatically infers targeted test files from changed files, symbol relationships, and naming conventions. Formulates typed `TestPlan` instances with bounded timeouts and execution constraints.
- **Command Policy & Strict Allowlist (`command_policy.py`):** Validates test commands against an explicit allowlist (`python -m pytest`, `python -m unittest`, `pytest`, `vitest`, `jest`, and local sandbox `node_modules/.bin/` binaries). Strictly rejects shell operators, command substitutions, path traversals, package installers, and unauthorized executables.
- **Sandboxed Execution (`executor.py`):** Executes approved test commands directly inside detached ephemeral worktrees (`codeatlas-sandbox-`) without shell invocation (`shell=False`). Enforces runtime timeouts, output byte limits, environment sanitization, secret pattern redaction, and post-execution worktree immutability checks.

### Platform-Aware Runner Resolution (`runner_selection.py`)
Runner resolution detects and resolves JavaScript and TypeScript test runners (`vitest`, `jest`) in a platform-safe, deterministic manner:
1. **POSIX Runner Selection:**
   - Selects only executable POSIX binaries from `node_modules/.bin/<runner>` or system `PATH`.
   - Windows launcher scripts (`.cmd`, `.bat`) are strictly ignored and rejected on POSIX.
   - Executes binaries directly without shell invocation (`shell=False`); no shell fallback is permitted.
2. **Windows Runner Selection:**
   - Approved `.cmd`, `.bat`, or `.exe` launchers in `node_modules/.bin/` or system `PATH` may be used.
   - All invocations remain strictly subject to the command allowlist and argument validation rules.
3. **Deterministic Fallback:**
   - When a runner preference is provided (e.g. `vitest`), the resolver checks the preferred runner first. If unavailable, it deterministically falls back to the other approved runner (e.g. `jest`) if present and executable.
   - When no preference is specified, deterministic discovery order is `vitest` followed by `jest`.
4. **Unsupported Runner Rejection:**
   - Any runner preference outside approved runners (`vitest`, `jest` for JS/TS; `pytest`, `unittest` for Python) is rejected during discovery with an `unknown_runner` limitation.
5. **Missing Executable Permission Behavior:**
   - On POSIX, candidate runner files lacking executable permissions (`st_mode & 0o111`, `os.access(..., os.X_OK)`) are disqualified from local selection.
   - If no valid system runner is found, discovery reports `missing_js_runner` and blocks test execution. Direct execution attempts fail closed with process launch errors rather than falling back to unvetted shells.
6. **Fixture Executable-Mode Preservation:**
   - Evaluation fixtures track POSIX executable bits (`100755`) directly in the Git repository index.
   - Evaluation generators (`generate_test_execution_cases.py`) and execution harnesses (`eval_test_execution.py`) preserve `0o755` file modes when populating sandbox repositories.

### Security Invariants
- **No Shell Metacharacters:** Shell control characters (`&`, `|`, `;`, `>`, `<`, `$`, `(`, `)`, `` ` ``, `\n`) are rejected at command validation.
- **No Arbitrary Commands:** Commands must strictly match allowlisted executable prefixes.
- **No Package Installation or Downloads:** Dependency managers (`pip`, `npm`, `pnpm`, `yarn`) and network retrieval tools (`curl`, `wget`) are strictly blocked.
- **Bounded Output & Secret Redaction:** Captures stdout and stderr up to `max_output_bytes` and scrubs all output against credential, API key, token, and secret patterns prior to report or evidence generation.
- **Zero Worktree Mutation:** Guarantees zero byte changes to the original working tree before and after sandbox execution.

## GitHub PR Review Integration (`codeatlas.github`)

Phase 9 integrates CodeAtlas with GitHub pull requests using a read-only by default, explicit-write only architecture (`codeatlas github review`).

### Modes and Artifacts
- **Issue Comments (Phase 9A):** Default mode posts anchored findings as individual PR issue comments.
- **Inline Comments (Phase 9B):** `--inline` posts comments anchored strictly to added (`+`) diff lines via GitHub's pull request comments API. Unchanged context lines fall back to issue comments; deleted or out-of-diff lines are suppressed.
- **Consolidated Summary Comment (Phase 9C):** `--summary-comment` renders a single bounded Markdown summary table containing finding counts by severity/category, test execution status, packet truncation, evidence limitations, and finding rows.
- **Check Run Integration (Phase 9D):** `--check-run` creates or updates a single GitHub check run (`CodeAtlas review`) reporting status and conclusion (`success`, `neutral`, `action_required`, `failure`).

### Write Ordering & Atomicity
When multiple write destinations are enabled (e.g. `--summary-comment --inline --check-run --post`), CodeAtlas enforces a deterministic, gated write sequence:
1. Fetch PR metadata and files read-only.
2. Validate local repository commits match PR base and head SHAs.
3. Run local CodeAtlas review (deterministic analyzers, repository index, review packet, policy engine, finding merge).
4. Cross-check local changed files with GitHub PR file list.
5. Render and budget all comment and check-run bodies.
6. Audit and redact all bodies against raw secret patterns.
7. Re-check head SHA before summary comment write; create or update in place.
8. Re-check head SHA before comment write batch (inline and fallback issue comments).
9. Re-check head SHA before check-run write; create or update check run.
10. Record complete review report and emit structured evidence.

If the remote PR head SHA changes at any gate:
- Prior completed writes remain intact (never rolled back automatically).
- All remaining write groups are immediately halted.
- The review report records `partial_write = true` and details which operations succeeded.

### Idempotency
- **Summary Comments:** Tracked via HTML comment marker `<!-- codeatlas:summary pr=<pr> head=<head> mode=summary -->`. Reruns update existing matching comments without duplicates. New head commits generate a separate current-head summary.
- **Individual Comments:** Tracked via finding marker `<!-- codeatlas:finding id=<finding_id> head=<head> mode=<issue|inline> -->`. Reruns suppress already posted findings.
- **Check Runs:** Bound by external ID `codeatlas:pr:<pr>:head:<head>`. Reruns update the existing check run.

### Minimum Required Permissions
- Read PR metadata, files, and diff: `pull-requests: read`
- Post PR comments and summary: `pull-requests: write` or `issues: write`
- Create and update check runs: `checks: write`

