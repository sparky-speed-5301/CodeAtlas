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

