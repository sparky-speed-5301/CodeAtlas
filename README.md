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
