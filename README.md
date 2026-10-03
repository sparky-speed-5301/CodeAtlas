# CodeAtlas

**Navigate complexity. Review intelligently. Fix safely.**

CodeAtlas is an evidence-first, repository-aware software engineering platform.
Phase 6 adds an isolated, approval-gated patch proposal and validation interface (`codeatlas.patching`)
featuring memory-safe unified diff parsing, protected path policies, cryptographic approval tokens,
ephemeral detached worktree sandboxes, and offline MockFixer test modes.
Phase 5 adds bounded review packet assembly, explicit policy gating, a reviewer provider abstraction
with an offline deterministic MockReviewer, strict provider output validation, and explainable finding merging.
Phase 4 adds a deterministic, inspectable repository intelligence and symbol context
layer. It builds an isolated repository index with safe exclusions, extracts Python
and TypeScript/JavaScript symbols and imports, tracks safe local references, maps diffs
to changed symbols, and deterministically retrieves explainable related context (test files,
callers, callees, configuration files).

Phase 3C adds bounded deterministic local data-flow tracking to the
sensitive-data exposure analyzer. Phase 3B adds deterministic, local,
read-only analyzers for hardcoded secrets and sensitive-data exposure. The
review core validates Git repositories, resolves commits, extracts diffs,
detects file languages, creates isolated snapshots, and writes manifests and
evidence logs. It still does not call live external LLMs, execute repository code,
or integrate with GitHub.

## Implementation plan

1. Establish the schemas, threat model, and benchmark cases (complete).
2. Add read-only repository snapshots, language detection, and deterministic evidence collection (complete).
3. Add deterministic repository intelligence, symbol extraction, and context retrieval (complete).
4. Add bounded context assembly, policy gating, and reviewer provider abstraction (complete).
5. Add isolated, approval-gated patch validation (complete).
6. Add live provider integrations only after the CLI, policy gates, and evaluation checks are reliable.

## Current commands

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m compileall -q eval src tests
python eval/run_eval.py --cases eval/cases --output eval/results/run.jsonl
python eval/run_eval.py --real-analyzer --cases eval/cases/sensitive-flow-aligned --output eval/results/sensitive-flow-aligned.jsonl
python eval/eval_repo_intel.py --cases eval/cases/repo-intel --output eval/results/repo-intel.jsonl
python eval/eval_review_packets.py --cases eval/cases/review-packets --output eval/results/review-packets.jsonl
python eval/eval_patching.py --cases eval/cases/patching --output eval/results/patching.jsonl
codeatlas patch inspect --proposal PATH
codeatlas patch validate --proposal PATH --repo PATH --base BASE
codeatlas patch apply-isolated --proposal PATH --repo PATH --base BASE --approval-token TOKEN
codeatlas review --repo PATH --base main --head HEAD \
  --index-repository --context-output out/context.json \
  --assemble-review-packet --review-provider mock \
  --packet-output out/packet.json --policy-output out/policy.json \
  --json-output out/review.json --markdown-output out/review.md \
  --manifest-output out/manifest.json --evidence-output out/events.jsonl \
  --analyzer secrets --analyzer sensitive-data-exposure
codeatlas --help
```

The review command runs only deterministic, diff-scoped analyzers, static repository
intelligence, and bounded packet assembly; it never executes files in the target repository, calls a remote LLM, or fabricates
findings. Use `--no-analyzers` to collect metadata and symbols only. Use `--no-index-repository`
to disable index construction. The evaluation runner is static and never executes files in a case.
See `docs/` for scope, architecture, non-goals, threats, limitations, and metrics.

## Phase 8C: Observed test evidence in human review

Both `patch validate` and `patch apply-isolated` include `review_packet` and
`human_approval_manifest` in the existing `--report-output` JSON. The CLI renders
scope, runner, targets, counts, bounded diagnostics, duration, redaction, network
isolation, and limitations. The existing evidence log records attachment or
exclusion using identifiers and status metadata.

Evidence attaches only from an execution-started `TestResult` whose proposal,
sandbox, patch hash, base commit, and `TestPlan` match the isolated validation
report. Approval, isolated application, syntax validation, output redaction,
runner policy, and bounds must pass. Blocked/not-run results remain visible
without attached observations. `approval_scope` records the approval token's run
scope; `approval_verified` authorizes isolated validation, not deployment or merge.

The existing `ObservedTestEvidence` model is bounded to 16,000 compact UTF-8 JSON
bytes, 4,000 diagnostic bytes, ten failed names (160 bytes each), a 500-byte
failure summary, six stack-summary lines (1,800 bytes), 64 command arguments and
targets (512 characters each), and 20 limitations (512 bytes each). Oversized
commands/target sets are excluded rather than shortened into a misleading scope.
If both scopes ran, the packet attaches the full-suite observation; the validation
report retains both original records and the packet displays both statuses.

Passing tests retain the human-approval gate. Existing finding evidence strengths
are preserved: a green run alone does not reproduce a finding. The existing
`validated` patch lifecycle state describes completed validation, not an approval
or a claim that the patch is safe. Network policy remains process/policy-only:
`network_isolation_verified=false` is explicit in packets and manifests.

> Observed test evidence covers only the executed scope and does not prove patch correctness or absence of other regressions.

Run `python eval/eval_observed_evidence.py` for the Phase 8C contract evaluation
and JSON Schema validation.

### Separate validation artifact files

Both patch-validation commands accept `--packet-output PATH`,
`--manifest-output PATH`, and `--markdown-output PATH`, independently or
together. They export the exact `review_packet`, `human_approval_manifest`,
and human-readable validation summary already generated for the report;
`--report-output` continues to contain both artifacts and the validation results.

```bash
codeatlas patch validate --proposal proposal.json --repo ./repo \
  --packet-output artifacts/packet.json --manifest-output artifacts/manifest.json \
  --markdown-output artifacts/summary.md

codeatlas patch apply-isolated --proposal proposal.json --repo ./repo \
  --approval-token "$APPROVAL_TOKEN" --run-tests \
  --report-output artifacts/report.json \
  --packet-output artifacts/packet.json --manifest-output artifacts/manifest.json \
  --markdown-output artifacts/summary.md
```

Missing parent directories are created after destination and redaction checks.
Destinations must be regular files, distinct from inputs and other outputs, and
must not traverse symbolic links or junctions. Git metadata and existing files
inside the target repository are protected from overwrite; use an external
artifact directory for repeatable exports. Existing external artifact files are
replaced atomically. Each file is atomic independently, so a failure writing the
second file may leave the first exported. Export failures identify the option
and exit nonzero; any existing report output is preserved. Failed validation
still exports its generated artifacts before returning the validation failure.
