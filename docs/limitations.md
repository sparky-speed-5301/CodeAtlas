# Limitations

Phase 3A still does not provide model calls, deep language analysis, sandbox
execution, test execution, GitHub integration, validated patch generation, or
automatic fixes/merge. The secret analyzer is pattern-based, may miss obfuscated
or non-literal credentials, and may flag unusual credential-like fixtures.
It cannot determine whether a detected value is active. It never emits the
full matched value and does not inspect unchanged or deleted lines.

Phase 3B sensitive-data exposure detection is also pattern-based. It may miss
aliases and indirect data flow, may flag generic names such as `token`, and
does not establish that data was actually transmitted or persisted. A future
data-flow analyzer is required for stronger guarantees.

Phase 3C tracks only deterministic bounded local assignments, object/dictionary access,
interpolation, and local alias chains within an intraprocedural scope. It does not prove
runtime data flow, cross functions or modules, resolve dynamic properties, or
evaluate imports. Ambiguous propagation and depth limits are reported as bounded unsupported-flow evidence or abstentions. Controlled benchmark fixtures in `eval/cases/sensitive-flow-aligned` test these explicit boundaries, verifying that unsupported interprocedural flows abstain rather than hallucinating detections.

## Phase 4 Repository Intelligence & Symbol Context Limitations

- **No Code Execution:** Repository indexing never compiles, runs, or imports target repository code or test suites. All symbol, import, and reference extraction is strictly static and read-only.
- **No Complete Call Graph:** Call relationships are tracked only for statically identifiable, unambiguous direct local references and resolved local imports. Dynamic dispatch, polymorphic calls, runtime monkeypatching, and reflection are not tracked.
- **No Interprocedural Semantic Analysis:** Analysis does not compute whole-program data-flow or interprocedural taint propagation.
- **Unsupported Dynamic Behavior:**
  - Dynamic imports (e.g. `import(variable)` or `require(runtimeVar)`) cannot be statically resolved and are explicitly flagged with `reason="dynamic_import"`.
  - External package imports (e.g. third-party dependencies from npm or PyPI) are marked `reason="external_package"` rather than guessed.
  - Names conflicting across scopes without local import clues are marked `reason="ambiguous_name"`.
- **Parsers & Fallback Boundaries:**
  - Python uses standard library `ast`, which guarantees exact syntactic correctness for valid syntax. Syntax errors result in `failed` status and `IndexDiagnostic` records rather than crashing.
  - TypeScript / JavaScript uses structured regex and brace matching. While highly accurate for clean function, class, interface, and arrow declarations, complex non-standard metaprogramming may result in lower parse confidence or fallback scopes.
- **Index Freshness Rules:**
  - The index is tied to the exact Git commit SHA. If the commit changes or is uncommitted, the index is marked stale and must be rebuilt.
- **Controlled Evaluation Limitations:**
  - Benchmark scores (100% precision/recall on Phase 4 scenarios) measure behavior on controlled, isolated fixtures. They do not constitute a guarantee of 100% coverage across arbitrary dynamic polyglot codebases.

## Phase 5 Review Packet & Provider Abstraction Limitations

- **No Live LLM Calls:** Phase 5 provides the typed provider abstraction (`ReviewerProvider`) and deterministic `MockReviewer`. No remote LLM APIs or network connections are made.
- **Strict Bounded Budgets:** Packets are subject to hard limits (`max_packet_bytes`, `max_lines_per_file`, `max_total_context_lines`, `max_findings`). Content exceeding limits is deterministically prioritized and truncated or excluded with explicit limitation entries.
- **Policy Abstention on Truncation:** When policy dictates `abstain_on_truncated_changed_code: true`, reviews where changed code is truncated will abstain rather than hallucinating review recommendations on partial diffs.
- **Precedence of Deterministic Evidence:** Provider outputs cannot override or demote deterministic analyzer findings without supporting evidence.
- **No Automatic Fixes or Merges:** Policy rules strictly prevent automatic fix execution, auto-merging, or modification of workflow or credential files.
## Phase 6 Patch Proposal & Validation Limitations

- **No Automatic Patch Application:** Phase 6 strictly prohibits applying patches to user repositories, working trees, or source directories automatically. All patch applications are gated behind explicit human approval tokens.
- **Detached Ephemeral Sandboxes Only:** Patch validation and test application occur exclusively within detached ephemeral Git worktrees (`codeatlas-sandbox-`). The original repository working tree is verified before and after execution to guarantee zero mutation.
- **No Arbitrary Code Execution:** Phase 6 validator does not run repository test suites, package manager lifecycle scripts, build systems, or arbitrary commands. Tests status remains explicitly `not_run`, and execution allowed remains `False`.
- **Protected Paths by Default:** Test files, CI/CD workflows, dependency manifests, and lockfiles cannot be modified by proposed patches unless specifically permitted by policy configuration.
- **Approval Scoping:** Approval tokens are cryptographically scoped to the proposal ID, base commit, patch hash, and target file paths. Any modification or base commit mismatch invalidates approval.
- **Controlled Evaluation Scope:** Phase 6 benchmark results reflect 29 controlled fixtures in `eval/cases/patching`. Real-world patches may encounter complex three-way merge conflicts requiring manual developer intervention.


## Phase 7A Live Provider Limitations

- **Read-Only Review Mode Only:** The live provider generates structured review findings, summaries, limitations, and abstentions. It cannot generate patches, apply fixes, merge, execute repository code or tests, run builds, install packages, or call arbitrary tools. Live patch application remains unimplemented until Phase 7B.
- **No Provider Tools:** The provider is a data-in/data-out transport component. It has no shell, Git, filesystem, network (beyond the provider transport itself), GitHub, secret, or approval-token access.
- **Credential Boundary:** API keys are read only from the environment variable named by `api_key_env_var`. Keys are never accepted in configuration files, packets, manifests, logs, or terminal output, and inline credential keys in provider config files fail closed.
- **Prompt-Injection Resistance Is Layered, Not Absolute:** Repository content is fenced in `<UNTRUSTED_REPOSITORY_CONTEXT>` markers and the system instruction forbids instruction-following from data, but a sufficiently persuasive injection of the underlying model cannot be ruled out deterministically. The guarantee is downstream: provider output passes schema, path, line-range, diff-anchoring, fabricated-test, duplicate, and raw-secret validation, and can never change policy, upgrade evidence strength, claim `validated`, or approve anything.
- **No Cancellation:** The HTTP transport uses blocking `urllib` requests with a timeout. In-flight requests cannot be cancelled mid-flight; cancellation lands at the next gate.
- **Single Backend:** One OpenAI-compatible HTTP backend is implemented. No automatic provider fallback exists; transport failures abstain the run and are recorded.
- **Budgets:** `request_budget` caps requests per reviewer instance, `max_output_tokens` caps generation, `max_response_bytes` caps responses, and per-run cost is estimated from configured pricing. There is no cross-run daily budget enforcement yet.
- **Evaluation Scope:** All Phase 7A evaluation cases use a deterministic fake transport. They verify pipeline contract and safety behaviour only; no live-provider model precision or recall is measured.

## Phase 7B Provider Patch Suggestion Limitations

- **Drafts Only:** Provider patch suggestions materialize as `PatchProposal` records with status `proposed` or `requires_human_approval`. They are never applied, never merged, and never turned into approval tokens by the review command.
- **Approval Boundaries:** Provider output cannot approve its own suggestion, generate approval tokens, or upgrade a proposal past `requires_human_approval`. Provider patches always require human approval regardless of configuration, and the existing scoped approval-token mechanism remains the only route to isolated application (Phase 6 command).
- **Structural Validation Scope:** Phase 7B validates suggestions structurally (parse, paths on both header sides, operations, protected paths, budgets, redaction, changed-line anchoring, read-only content match against the head snapshot, hash integrity, policy). It does not run tests, builds, or sandbox application, so "applies cleanly" is not asserted at proposal time.
- **Content Match Is Literal:** Hunk old-side lines must exactly match the head snapshot. Whitespace-only divergence (trailing spaces, line-ending variants beyond CRLF normalization) rejects valid-intent suggestions as conflicts.
- **Changed-Line Anchoring:** Provider patches must overlap the review diff's changed lines by default; a fix touching only unchanged context lines is rejected unless policy explicitly allows it.
- **Same Redaction Patterns:** Suggestion, diff, and rationale redaction uses the same credential pattern set as the rest of the pipeline; novel secret formats may pass pattern checks.
- **Evaluation Scope:** All Phase 7B evaluation cases use a deterministic fake transport. No patch correctness is measured because no proposal is applied.

## Phase 7C Isolated Validation Limitations

- **One Command, One Path:** Isolated validation is reachable only through `codeatlas patch apply-isolated` → `validate_patch_proposal(..., allow_isolated_apply=True)` with a valid scoped token. The review command can never apply; its manifests record `isolated_validation_attempted=false` unconditionally.
- **No Correctness Claim:** Clean application plus valid syntax says nothing about whether the patch is right. `tests_status` and `build_status` are always `not_run` in Phase 7C, and `execution_allowed` is always false.
- **Terminal-State Blocking:** Rejected, applied, and validated proposals refuse re-validation unless policy sets `patch.allow_revalidation`. A `failed_validation` proposal requires a fresh approved attempt (revalidation policy) before another sandbox run.
- **Syntax Check Is Python-Only and Vacuous Elsewhere:** Non-Python files report `syntax_valid=true` without any parsing; a "valid" TypeScript patch has no syntax gate.
- **Sandbox Retention Is Debug-Only:** `--retain-sandbox-on-failure` leaves a sandbox directory on disk for local debugging. It is opt-in, off by default, reported as `cleanup_status="retained"`, and never enabled by the review command.
- **Cleanup Failure Surfacing:** If worktree removal fails (e.g. file locks on Windows), the report records `cleanup_status="failed"` and the command exits nonzero; a leftover directory may remain and must be removed manually.
- **Token Scope Is Deterministic, Not Time-Bound:** Approval tokens bind proposal ID, base commit, patch hash, target files, and run ID. Token expiration is not implemented; the "wrong-scope" dimension is covered by the run-ID binding.
- **git Apply Context Search:** `git apply` can relocate hunks whose stated position is wrong but whose context matches elsewhere in the file. Stale-position patches with matching context may therefore apply cleanly; content-absent hunks fail as conflicts.

## Phase 8 Test Execution & Runner Portability Limitations

- **Approved Runner Scope:** Test execution is strictly limited to approved runners on the explicit allowlist (`pytest`, `unittest` for Python; `vitest`, `jest` for TypeScript/JavaScript). Non-standard runners (e.g. `mocha`, `karma`, `ava`, `tox`) require explicit policy extensions before approval.
- **Python Virtual-Environment Discovery:** Python test discovery uses the active environment's interpreter (`sys.executable`). It does not automatically discover, create, or activate virtual environments outside the current supported environment.
- **POSIX Executable Permissions:** On Linux and macOS, local runners in `node_modules/.bin/` must have POSIX executable bits (`+x`). Files lacking execute permissions fail closed without falling back to arbitrary shells or unvetted scripts.
- **Windows Launcher Constraints:** On Windows, approved `.cmd`, `.bat`, or `.exe` launchers may be used, but remain strictly constrained by allowlist prefix matching, argument validation, and shell metacharacter blocking.
- **Offline / No-Install Guarantee:** Test execution is strictly offline and local. Package manager installation (`npm`, `pip`, `yarn`, `pnpm`) and network tools (`curl`, `wget`) are unconditionally blocked by policy.
- **Bounded Diagnostic Capture:** Diagnostic output parsing is regex- and heuristic-based. Unrecognized failure output formats fall back to generic truncated summaries up to configured output byte budgets.

## Phase 9B Inline Comment Limitations

- **Comments Only:** Inline comments are added via the pull-request comments endpoint with `commit_id`, `path`, and `line`. No review verdict, approval, check, label, or merge path exists.
- **Added Lines Only:** Inline comments anchor exclusively to added (`+`) diff lines. Unchanged context lines and mixed/ambiguous ranges fall back to issue comments with a visible reason; deleted or out-of-diff lines are suppressed with a visible reason. GitHub's wider anchoring rules (e.g. context-line comments) are deliberately not exercised.
- **Diff Source:** The line map comes from the local `git diff` between the verified base/head SHAs. GitHub-side diff rendering differences (rename detection, whitespace flags) are not consulted; the changed-file set is cross-checked, but per-hunk divergence between the GitHub diff and the local diff is not detected.
- **Multi-Line Ranges:** Multi-line inline comments use `start_line`/`line` only when every line in the finding range is an added line; otherwise the finding falls back.
- **Marker Compatibility:** Idempotency markers now carry `mode=issue|inline`. Legacy 9A markers without the mode segment still parse and suppress duplicates; deduplication itself remains mode-agnostic (one comment per finding per head SHA across both modes).

## Phase 9C PR Summary Comment Limitations

- **Single Consolidated Comment:** Summary comment mode (`--summary-comment`) renders a single consolidated markdown issue comment with overview metrics, findings table, validation status, and limitations.
- **Strict Size Bounds:** Summary comments are capped at 50,000 UTF-8 bytes, maximum 50 findings, maximum 20 table rows, and maximum 20 limitations. Excessive content is deterministically prioritized and truncated with clear notice.
- **Idempotency via Markers:** Summary comments use `<!-- codeatlas:summary pr=<pr> head=<head> mode=summary -->`. Reruns on the same head update existing comments in place without duplicates. New head SHAs create fresh summaries and never overwrite previous head records.
- **Fail-Closed Redaction:** If any raw credential or secret pattern is detected in the rendered summary, the summary write is completely blocked and recorded as a failure. It is never posted partially redacted or unredacted.
- **Local Checkout Required:** CodeAtlas reviews against a verified local repository containing the exact PR commits. It never fetches, resets, or checks out branches automatically.

## Phase 9D GitHub Check-Run Limitations

- **Informational Check Runs Only:** Check runs communicate review status and findings via conclusions (`success`, `neutral`, `action_required`, `failure`). They never submit a pull request review verdict (APPROVE / REQUEST_CHANGES), merge code, or alter branch protection rules.
- **No Claims of Correctness:** A `success` conclusion indicates zero findings and passing tests; it does not claim mathematical patch correctness or guarantee absence of regressions.
- **External ID Binding:** Check runs are bound to `codeatlas:pr:{pr}:head:{head}`. Re-runs on the same head update the existing check run rather than producing duplicate checks.
- **Pre-Write SHA Validation:** Every write group (summary -> comments -> check run) is preceded by an immediate re-check of the remote PR head SHA. Any change aborts subsequent writes immediately to prevent writing against stale heads.
- **Network Isolation Unverified:** `network_isolation_verified=false` remains explicit in all check-run summaries and annotations. Process sandboxing does not provide hardware or OS-level network isolation.
