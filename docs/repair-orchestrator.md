# Phase 11C-A: Repair Orchestrator Foundation

`codeatlas.orchestrator.RepairOrchestrator` turns **one quality-approved finding**
into a bounded draft `PatchProposal` requiring human approval. The foundation is
an offline Python API, with Python, JavaScript, and TypeScript language adapters.

## Inputs and results

```python
from codeatlas.orchestrator import RepairOrchestrator, RepairRepositoryState, record_repair_result

# The caller captures the active run/revisions and holds an existing detached
# head Snapshot. These are trusted backend inputs, independent of provider data.
state = RepairRepositoryState(
    run_id=manifest.run_id,
    repository=manifest.repository,
    base_commit=manifest.base_commit,
    head_commit=manifest.head_commit,
)
result = RepairOrchestrator(offline_provider).plan(
    finding,
    packet=review_packet,
    manifest=manifest,
    state=state,
    snapshot=head_snapshot,
    evidence=evidence_logger,
)
if result.status == "proposed":
    proposal = result.proposal  # Existing PatchProposal; approval_required=True.
    updated_manifest = record_repair_result(manifest, result)
```

`plan` returns `proposed`, `rejected`, or `review_only` with stable reason codes.
`propose` (also `create_proposal`) returns just the existing `PatchProposal`, or
raises `RepairRejected`. Unsupported languages produce `review_only`.
`record_repair_result` returns a manifest copy using its existing patch fields;
it preserves previously recorded proposals and is idempotent by proposal ID.

The finding must match the active manifest record exactly. Run ID, repository,
packet ID, base/head revisions, and snapshot commit must agree. The original
workspace cannot be supplied as the head snapshot. Abstention, duplicate
suppression, weak/missing deterministic evidence, low confidence, ambiguous or
unsupported flow, invalid locations, stale findings, incompatible state, and
protected/generated targets fail closed before provider invocation.

A review-only finding needs the explicit backend `fix_eligible=True` argument
and must still satisfy the evidence, scope, and language gates. Eligibility does
not approve a patch. Finding confidence and evidence strength are not upgraded
by proposal generation or provider claims.

## Bounded context and provider contract

`RepairContext` is a frozen, strict data model; its serialized contract is
`schemas/repair-context.schema.json`. It contains run/finding/repository identity,
review revisions, language, target and finding line range, containing symbol,
bounded code, deterministic evidence, quality decision, confidence/evidence
strength, relevant imports, already-available related test paths, limits,
prohibited paths, policy version, and context truncation status.

Defaults: **one target file**, **60 added/deleted patch lines**, **32 KB diff**,
**64 KB target source**, **160 code-context lines / 16 KB code**, **24 KB complete
context**, ten evidence items, ten imports, and eight related test paths.
Limits have hard ceilings and may be tightened by the caller. Required symbol
context is rejected when it cannot fit; optional import truncation is explicit.

The existing `ReviewerProvider.review(ReviewPacket)` interface receives a fresh
one-finding projection. An optional `RepairProvider.propose(RepairContext)`
operation accepts the same bounded data and returns a structured suggestion,
JSON suggestion, or `ReviewerResult` with exactly one `patch_suggestions` item.
Providers must explicitly declare `network_access=False`. Live and undeclared
providers are rejected. Provider implementations are trusted in-process
components; their returned data has no authority over policy or execution.

The suggestion uses the existing contract: `suggestion_id`, `finding_id`,
`target_files`, `unified_diff`, `rationale`, `expected_behavior`, optional
`risk_level`, `limitations`, and `provider_provenance`. Unexpected fields,
approval/execution claims, additional findings, multiple suggestions, duplicate
JSON keys, oversized output, abstention, invalid structure, and secrets are
rejected. Secrets are scanned in string leaves before JSON escaping, including
metadata that will be discarded.

Raw transcripts, approval tokens, credentials, configuration secrets, unrelated
files, and unbounded logs are never projected into provider context. Source
redaction and secret scanning reuse the existing review, patch, execution-output,
and deterministic-secret scanners. Evidence events contain IDs, decisions, and
bounded metadata, not source, diffs, or provider responses.

## Shared patch and validation path

The orchestrator composes `materialize_patch_suggestions`, which already uses
the unified-diff parser, proposal constructor/hash, path and protected-file
policy, redaction audit, and `validate_patch_proposal(allow_isolated_apply=False)`.
The existing patching module provides an in-memory preview for adapter parsing.
Hunks must match exact, ordered, non-overlapping source coordinates, stay within
the supplied code context, and actually edit the finding location. Context lines
alone cannot anchor unrelated edits. No-op, multi-file, new/delete/rename,
out-of-scope, stale, secret-bearing, and syntax-breaking patches are rejected.

The returned validation is **static only**: `valid=True` means the draft passed
the structural/policy/available parser gates. `applies_cleanly=False` records that
no actual isolated application was observed. `approval_verified=False`, no
sandbox, no commands run, and tests/builds/type checks `not_run` are explicit.
The proposal remains `requires_human_approval`, with `approval_required=True`.

Subsequent human-approved validation uses the existing run-scoped approval token,
isolated validator, test discovery, safe command policy, and test executor. The
repair API never generates tokens or enters that execution path.

## Language adapters and limitations

The common `LanguageAdapter` protocol covers detection, installed-tool reporting,
static parsing, fixability classification, protected/generated-file detection,
allowlisted check plans, bounded targeted-test selection, and patch constraints.

- **Python:** existing standard-library AST/symbol parser; syntax can be checked
  in memory. Available ruff/black checks and pytest targets are planned only.
- **JavaScript / TypeScript:** existing partial symbol parser plus conservative
  delimiter checks. Successful partial parsing leaves `syntax_valid=None` and an
  explicit limitation. Compiler/type-check/build success is never inferred.
  Available ESLint/Prettier/TypeScript checks and vitest/jest targets are data only.
- Check templates are validated in the existing command-policy module. The test
  executor's execution allowlist is unchanged; repair generation executes none
  of the planned commands.

No source file is written, patch automatically applied or approved, dependency
installed, network request issued, arbitrary command executed, or commit/merge
performed during repair generation. Snapshot creation and human-approved
isolated validation remain explicit caller-owned operations.

## Verification

```bash
python -m pytest -q tests/unit/test_repair.py tests/integration/test_repair_orchestrator.py
python eval/eval_repair.py
```

The 22-case offline evaluation covers successful proposals in all three languages,
quality/freshness rejection, unsupported languages, network-capable providers,
malformed/unsafe provider output, secrets, conflicts, syntax errors, generated
files, and bounds. Contract tests also block process/network/approval/application
calls and verify the existing manifest/schema/token contracts. The integration
test preserves actual staged, unstaged, and untracked Git work while handing the
proposal to the existing approval-gated isolated validator.

## Phase 11C-B: FixProposal integration and safe VS Code diff preview

The local service exposes bounded fix-proposal operations that compose
`RepairOrchestrator` with a deterministic offline provider
(`MockRepairReviewer`, `network_access=False`); no second proposal model,
patch parser, or validation pipeline exists. The user's explicit Generate/Regenerate
Fix request is the backend `fix_eligible=True` decision for review-only findings;
it is never an approval, and every proposal stays `approval_required=True`.

Operations (all run-scoped, finding-scoped, bounded, schema-validated, redacted,
policy-checked):

- `GET /reviews/{run}/findings/{finding}/fix-eligibility` — mirrors the
  orchestrator's fail-closed gates (abstained, suppressed duplicate, weak
  evidence, ambiguous location, unsupported flow, fixability, unsupported
  language, protected targets) plus repository staleness (HEAD must still match
  the reviewed head commit) and returns stable reason codes with fixed
  user-safe explanations.
- `POST /reviews/{run}/findings/{finding}/fix-proposal` — generates one draft
  via a temporary detached head snapshot (read-only; never the workspace) and
  returns the typed FixProposal.
- `GET /reviews/{run}/findings/{finding}/fix-proposal` and
  `GET /fix-proposals/{proposal_id}?run_id=&finding_id=` — retrieval with
  cross-run/cross-finding scope checks.
- `POST .../fix-proposal/reject` — explicit rejection of a draft-ready proposal
  through the existing `PatchStatus` transition.
- `POST .../fix-proposal/regenerate` — re-runs bounded generation; a changed
  draft supersedes the previous one, an identical draft keeps its ID.

The FixProposal contract (`FixProposalResponse`,
`schemas/fix-proposal.schema.json`, version `11C-B.1`) carries proposal/finding/
run identity, repository identity (never an absolute path), base/head commits,
single target file, the redaction-audited `patch_text`, `patch_hash`, diagnosis,
explanation, expected behavior change, assumptions, risk level, confidence,
evidence strength and sources, quality and policy decisions, lifecycle state,
limitations, and fixed rejection explanations. `approval_required` is a schema
constant `true`; approval tokens, credentials, provider transcripts, and
commands can never appear. Lifecycle states are `not_eligible`, `generating`,
`generation_failed`, `rejected_by_policy`, `draft_ready`, `rejected`, and
`regeneration_requested`.

The VS Code extension adds `CodeAtlas: Generate Fix`, `Preview Fix`,
`Reject Fix`, and `Regenerate Fix` commands. Preview uses VS Code's diff editor
against a virtual, read-only document provider: the patch is applied strictly
in memory (exact coordinates, context must match the current file, otherwise
the preview fails closed), no temporary files are written, and the repository,
working tree, and Git index are never touched. The preview reports the proposal
ID, patch hash, risk, confidence, evidence strength, quality and policy
decisions, assumptions, limitations, and approval-required status. There is
deliberately no Apply Fix command in this phase.

Verification: `python -m pytest -q tests/unit/test_fix_proposals.py` covers the
contract, schema, idempotency, scope checks, rejection/regeneration lifecycle,
stale-repository and secret-line fail-closed behavior, worktree preservation,
and the HTTP endpoints.

## Phase 11C-C: approval-gated FixProposal sandbox validation

Phase 11C-C adds two explicit, run- and finding-scoped service operations for a
`FixProposal`:

- `POST /reviews/{run}/findings/{finding}/fix-proposal/approve-validation`
  verifies an operator-supplied token and moves the proposal to
  `approved_for_validation`.
- `POST /reviews/{run}/findings/{finding}/fix-proposal/validate` (or the
  equivalent `/fix-proposals/{proposal}/validate`) runs only an already
  approved proposal through the existing isolated validator.

The validation token binds the proposal ID, finding ID, run ID, repository
identity, review base commit, reviewed head commit, patch hash, exact target
files, and the fixed operation name `validate`. It is checked before detached
worktree creation. Tokens are not generated by providers, persisted in the
FixProposal, logged, returned by either operation, or displayed by the
extension. A generation request and a read-only preview never count as
approval.

The FixProposal records explicit validation history:
`approval_required` → `approved_for_validation` → `validating` →
`applied_in_isolated_worktree` → `tests_running` → `validated`, with bounded
failure and cleanup states. Validation responses reuse the existing patch
parser/policy, detached sandbox, approved test-command policy, redaction,
observed evidence, review packet, human-approval manifest, and cleanup
behavior. Targeted tests and the opt-in full suite run only in the detached
worktree; no source, index, ref, or registration in the original workspace is
changed. There remains no Apply Fix command.

## Phase 11C-D: explicit apply of a validated FixProposal

Phase 11C-D adds exactly one way to write a validated fix into the original
workspace: `POST /reviews/{run}/findings/{finding}/fix-proposal/apply`, behind
the VS Code command `CodeAtlas: Apply Validated Fix`. Nothing is ever applied
automatically — not after generation, preview, sandbox approval, validation,
test success, a service response, or provider output. Application requires all
of the following, re-checked immediately before the mutation:

1. The proposal is `draft_ready` and `validated` (validation failed, cleanup
   failed, rejected, superseded, already applied, or reverted proposals are
   refused with stable reason codes such as `apply_not_validated`,
   `apply_validation_failed`, `already_applied`, `apply_not_available`).
2. The full proposal scope still matches the run: repository identity, base
   and head commits, provenance, target files, and the recomputed patch hash
   (`apply_scope_invalid`, `patch_hash_mismatch`).
3. The validation evidence exists, is identity-matched (proposal ID, patch
   hash, head commit, resulting diff hash), and belongs to a validated result
   (`apply_evidence_missing`, `apply_evidence_identity_mismatch`).
4. The stored repair policy version is still current
   (`apply_policy_version_incompatible`).
5. The exact patch text re-parses, re-passes patch policy, and re-passes the
   redaction/secret scan (`patch_parse_failed`, `patch_validation_failed`,
   `apply_redaction_failed`).
6. The workspace is still exactly where validation left it: HEAD equals the
   reviewed head commit (`repository_state_stale`) and the working tree is
   clean (`workspace_dirty`). The service never fetches, rebases, merges,
   resets, stashes, or checks out anything.
7. The user supplied an approval token minted for `operation="apply"` (a
   validate token is rejected; token scopes are operation-specific) and the
   complete current patch hash as the explicit final confirmation
   (`apply_confirmation_invalid`).

The apply itself goes through a single guarded primitive
(`apply_patch_to_worktree` in `codeatlas.patching.apply`) that: re-verifies the
clean tree, captures exact pre-apply bytes of the declared target files only,
runs `git apply --check` first, applies the patch, verifies the touched-file
set, and requires the resulting workspace diff hash to byte-match the
validated sandbox diff. On any mismatch every touched file is restored to its
captured pre-apply bytes immediately, and the response carries only fixed
error strings plus the stable reason codes `apply_failed` /
`resulting_diff_mismatch`. Raw git stderr, provider text, tokens, and secrets
never appear in any response.

On success the proposal moves to the new `applied` terminal chain
(`PatchStatus` `validated` → `applied`; validation lifecycle `validated` →
`applied`), the apply event is appended to bounded in-memory history, and the
response reports the applied files, HEAD/branch at apply time, and both the
validated and observed resulting diff hashes. The FixProposal schema version
moves to `11C-D.1`; `generation_status` gains `applied`, `validation_status`
gains `applied` and `reverted`.

Verification: `python -m pytest -q tests/unit/test_worktree_apply.py
tests/unit/test_fix_apply.py tests/integration/test_autofix_workflow.py`.

## Phase 11C-E: safe revert and apply history

- `POST .../fix-proposal/revert` restores the exact pre-apply bytes captured at
  apply time. It only touches the declared target files, refuses if any target
  file's current bytes no longer hash-match the recorded post-apply state
  (`revert_state_changed`), requires the same confirmed patch hash
  (`revert_confirmation_invalid`), verifies the restoration byte-for-byte, and
  never runs git mutations (no checkout/reset/stash). A user edit made after
  the apply is preserved, not clobbered.
- `GET .../fix-proposal/apply-history` (and
  `/fix-proposals/{proposal}/apply-history?run_id=&finding_id=`) returns the
  bounded event history (`applied`, `reverted`, `revert_failed`,
  `apply_rejected`) with stable metadata only, plus the current apply status
  (`not_applied`, `applied`, `reverted`) and revert availability.
- The VS Code extension adds `CodeAtlas: Revert Applied Fix` and
  `CodeAtlas: Show Fix Apply History`. Both are explicit; revert shows a modal
  confirmation naming the restored files. Captured pre-apply bytes live only
  in service memory, so revert availability is scoped to the service session.

## Phase 11C-F: end-to-end verification

The full autofix chain — real review run, generation via
`RepairOrchestrator`, explicit approval, isolated sandbox validation, explicit
apply, and revert — is verified end to end at three levels:

- `tests/integration/test_autofix_workflow.py`: one real Git repository and
  the real loopback HTTP service; exercises the complete chain plus the
  dirty-workspace, validate-token, and untyped-endpoint rejection paths, and
  asserts HEAD, refs, and the index are never mutated.
- `eval/eval_autofix_workflow.py` (12 offline cases, `Phase 11C-F: N/12`):
  apply requires validation, validate tokens never apply, confirmation-hash
  enforcement, dirty-workspace and stale-HEAD rejection, double-apply
  rejection, byte-exact revert, revert blocking after user edits, ordered
  bounded history, zero git metadata mutation, and token-free responses.
- `extension/test_autofix_workflow.js`: the compiled VS Code extension driven
  against the real production service through the test host, covering the
  actual user flow: Generate Fix → (modal confirmations) → Approve → Validate
  → Apply Validated Fix → Revert, including refusal before validation,
  validate-token rejection at apply time, no-token-prompt-without-confirmation,
  and token-free UI surfaces.
