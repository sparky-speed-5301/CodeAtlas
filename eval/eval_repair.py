"""Offline Phase 11C-A repair foundation evaluation (no repository execution)."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from codeatlas.findings.models import Finding
from codeatlas.git.models import ChangeStatus, Diff, FileChange, LineRange
from codeatlas.git.snapshot import Snapshot
from codeatlas.orchestrator import RepairOrchestrator, RepairRepositoryState, RunManifest
from codeatlas.review.packet import assemble_review_packet
from codeatlas.review.provider import ReviewerResult
from codeatlas.review.quality import assess_finding

SOURCES = {
    "python": "def compute(value):\n    return value // 0\n",
    "javascript": "export function compute(value) {\n    return value / 0;\n}\n",
    "typescript": "export function compute(value: number): number {\n    return value / 0;\n}\n",
}


def repair_request(root: Path, path: str = "src/calc.py", language: str = "python") -> tuple[Finding, dict]:
    """Small deterministic fixture also used by the repair contract tests."""
    snapshot = root / "snapshot"
    target = snapshot / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SOURCES[language], encoding="utf-8", newline="\n")
    state = RepairRepositoryState(run_id="run-repair", repository=str(root / "repository"),
                                  base_commit="b" * 40, head_commit="a" * 40)
    diff = Diff("base", "head", files=(FileChange(
        path=path, status=ChangeStatus.MODIFIED, old_ranges=(LineRange(2, 1),), new_ranges=(LineRange(2, 1),),
    ),))
    packet = assemble_review_packet(snapshot, diff, repository_name=state.repository,
                                    base_commit=state.base_commit, head_commit=state.head_commit)
    finding = assess_finding(Finding(
        id="CA-REPAIR-1", file=path, start_line=2, end_line=2, severity="high", category="LOGIC_BUG",
        claim="The changed expression divides by literal zero.", impact="The expression cannot compute the intended ratio.",
        evidence_strength="strong", confidence=0.95, evidence=["Changed divisor is the literal 0."],
        tools_consulted=["deterministic-analyzer"], fixability="suggested", status="detected",
        provenance={"origin": "deterministic", "run_id": state.run_id},
    ), packet=packet)
    packet.deterministic_findings = [finding.model_dump(mode="json")]
    manifest = RunManifest(run_id=state.run_id, repository=state.repository, base_ref="base", head_ref="head",
                           base_commit=state.base_commit, head_commit=state.head_commit,
                           findings=[finding.model_dump(mode="json")], review_packet_id=packet.packet_id)
    return finding, {"packet": packet, "manifest": manifest, "state": state, "snapshot": Snapshot(snapshot, state.head_commit)}


def repair_suggestion(path: str = "src/calc.py", language: str = "python") -> dict:
    old = SOURCES[language].splitlines()[1]
    return {
        "suggestion_id": "repair-draft-1", "finding_id": "CA-REPAIR-1", "target_files": [path],
        "unified_diff": f"--- a/{path}\n+++ b/{path}\n@@ -2 +2 @@\n-{old}\n+{old.replace(' / 0', ' / 2').replace('// 0', '// 2')}\n",
        "rationale": "Replace the zero divisor with the intended constant, subject to human review.",
        "expected_behavior": "Compute the ratio without a zero divisor.", "risk_level": "medium",
        "limitations": ["The intended divisor must be confirmed by the author."],
    }


class OfflineRepairReviewer:
    name = "offline-repair-fixture"
    version = "1.0"
    network_access = False

    def __init__(self, suggestion: dict | None = None) -> None:
        self.suggestion = suggestion if suggestion is not None else repair_suggestion()
        self.calls = 0
        self.received = None

    def review(self, packet) -> ReviewerResult:
        self.calls += 1
        self.received = packet
        return ReviewerResult(provider_name=self.name, patch_suggestions=[self.suggestion])


CASES = (
    "python", "javascript", "typescript", "abstained", "duplicate", "review_only", "low_evidence",
    "ambiguous", "unsupported_flow", "invalid_location", "stale_run", "incompatible_state",
    "unsupported_language", "network_provider", "malformed_patch", "provider_command", "provider_secret",
    "syntax_error", "conflict", "patch_budget", "generated_file", "unbounded_context",
)


def evaluate_case(kind: str) -> dict:
    from codeatlas.orchestrator import RepairLimits

    with tempfile.TemporaryDirectory(prefix="codeatlas-repair-eval-") as directory:
        root = Path(directory)
        language = kind if kind in SOURCES else "python"
        path = {"python": "src/calc.py", "javascript": "src/calc.js", "typescript": "src/calc.ts"}[language]
        if kind == "unsupported_language":
            path = "src/calc.rs"
        finding, request = repair_request(root, path, language)
        suggestion = repair_suggestion(path, language)
        provider = OfflineRepairReviewer(suggestion)
        limits = RepairLimits()
        updates = {
            "abstained": {"quality_decision": "abstain"}, "duplicate": {"quality_decision": "suppress_duplicate"},
            "review_only": {"quality_decision": "review_only"}, "low_evidence": {"evidence_strength": "weak"},
            "ambiguous": {"ambiguity_score": 0.5}, "unsupported_flow": {"unsupported_flow": True},
            "invalid_location": {"start_line": 40, "end_line": 40},
        }
        if kind in updates:
            finding = finding.model_copy(update=updates[kind])
            request["manifest"].findings = [finding.model_dump(mode="json")]
        if kind == "stale_run":
            request["state"] = request["state"].model_copy(update={"run_id": "new-run"})
        elif kind == "incompatible_state":
            request["state"] = request["state"].model_copy(update={"head_commit": "c" * 40})
        elif kind == "network_provider":
            provider.network_access = True
        elif kind == "malformed_patch":
            suggestion["unified_diff"] = "invalid diff"
        elif kind == "provider_command":
            suggestion["commands"] = ["pip install dependency"]
        elif kind == "provider_secret":
            suggestion["rationale"] = "password = 'private-value-123'"
        elif kind == "syntax_error":
            suggestion["unified_diff"] = suggestion["unified_diff"].replace("+    return value // 2", "+    return (")
        elif kind == "conflict":
            suggestion["unified_diff"] = suggestion["unified_diff"].replace("-    return value // 0", "-    return value // 9")
        elif kind == "patch_budget":
            limits = RepairLimits(max_patch_lines=1)
        elif kind == "generated_file":
            target = request["snapshot"].path / path
            target.write_text("# @generated\n" + SOURCES[language], encoding="utf-8")
        elif kind == "unbounded_context":
            limits = RepairLimits(max_context_lines=1)
        target = request["snapshot"].path / path
        before = target.read_bytes()
        result = RepairOrchestrator(provider, limits=limits).plan(finding, **request)
        expected = "proposed" if kind in SOURCES else "review_only" if kind == "unsupported_language" else "rejected"
        checks = {"expected_status": result.status == expected, "snapshot_unchanged": target.read_bytes() == before,
                  "approval_required": result.approval_required,
                  "no_execution": result.validation is None or (not result.validation.execution_allowed and not result.validation.commands_run),
                  "no_automatic_approval": result.proposal is None or result.proposal.status == "requires_human_approval"}
        if result.proposal:
            checks["bounded_scope"] = result.proposal.target_files == [path]
            checks["existing_policy"] = result.proposal.policy_decision["decision"] == "requires_human_approval"
        return {"case_id": f"repair-{kind}", "status": result.status, "reason": result.reason,
                "checks": checks, "pass": all(checks.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    results = [evaluate_case(kind) for kind in CASES]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("".join(json.dumps(result, sort_keys=True) + "\n" for result in results), encoding="utf-8")
    for result in results:
        print(json.dumps(result, sort_keys=True))
    passed = sum(result["pass"] for result in results)
    print(f"Phase 11C-A: {passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
