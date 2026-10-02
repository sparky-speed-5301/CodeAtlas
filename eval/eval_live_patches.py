"""Live-patch contract, safety, and policy evaluation runner (Phase 7B).

Executes the review pipeline with opt-in provider patch suggestions against
deterministic fixtures using an in-memory FakeTransport.  No live API calls,
no patch application, no repository test or build execution.

Metric groups are reported separately and explicitly labelled:
  - provider contract  : suggestion acceptance/rejection behaviour
  - patch safety       : proposal gating, redaction, application prevention
  - review quality     : linkage and target accuracy
No patch correctness is reported: proposals are never applied in Phase 7B, so
no claim about whether patches actually fix code is possible or made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from codeatlas.orchestrator.review import run_review
from codeatlas.providers import FakeTransport
from codeatlas.review.packet import SECRET_PATTERNS


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return result.stdout.strip()


def _tree_hash(work: Path) -> str:
    """Hash all non-git file bytes to detect any worktree mutation."""
    digest = hashlib.sha256()
    for p in sorted(work.rglob("*")):
        if ".git" in p.relative_to(work).parts:
            continue
        if p.is_file():
            digest.update(str(p.relative_to(work)).encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


def evaluate_live_patch_case(case_dir: Path) -> dict[str, Any]:
    """Evaluate one live-patch fixture end to end with a fake transport."""
    metadata = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
    expected = metadata.get("expected", {})

    if (case_dir / "response.json").is_file():
        simulated_response: str | dict[str, Any] | None = json.loads(
            (case_dir / "response.json").read_text(encoding="utf-8")
        )
    elif (case_dir / "response.txt").is_file():
        simulated_response = (case_dir / "response.txt").read_text(encoding="utf-8")
    else:
        simulated_response = None

    with tempfile.TemporaryDirectory(prefix="codeatlas-patch-eval-") as temp:
        work = Path(temp) / "work"
        work.mkdir()

        _git(work, "init", "-q")
        _git(work, "config", "user.email", "eval@example.invalid")
        _git(work, "config", "user.name", "eval")
        shutil.copytree(case_dir / "before", work, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _git(work, "add", "-A")
        _git(work, "commit", "--allow-empty", "-qm", "before")
        base_sha = _git(work, "rev-parse", "HEAD")

        for item in list(work.iterdir()):
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

        time.sleep(0.05)
        shutil.copytree(case_dir / "after", work, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        now = time.time() + 1.0
        for p in work.rglob("*"):
            if p.is_file():
                try:
                    os.utime(p, (now, now))
                except OSError:
                    pass
        if metadata.get("config"):
            import yaml

            (work / ".codeatlas.yml").write_text(
                yaml.safe_dump(metadata["config"]), encoding="utf-8"
            )
        _git(work, "add", "-A")
        _git(work, "commit", "--allow-empty", "-qm", "after")
        head_sha = _git(work, "rev-parse", "HEAD")

        transport = FakeTransport(simulated_response=simulated_response, latency_ms=5.0)
        artifacts = Path(temp) / "artifacts"
        artifacts.mkdir()
        manifest_path = artifacts / "manifest.json"
        evidence_path = artifacts / "evidence.jsonl"

        tree_before = _tree_hash(work)
        head_before = head_sha

        review_result = run_review(
            work,
            base=base_sha,
            head=head_sha,
            assemble_review_packet=True,
            review_provider="live",
            provider_model="test-model",
            allow_patch_suggestions=bool(metadata.get("allow_patch_suggestions", True)),
            provider_transport=transport,
            manifest_output=manifest_path,
            evidence_output=evidence_path,
        )
        manifest = review_result.manifest

        checks: dict[str, bool] = {}
        requests_made = len(transport.captured_payloads)
        if expected.get("request_made") is not None:
            checks["request_made"] = requests_made == (1 if expected["request_made"] else 0)
        else:
            checks["request_made"] = True

        checks["suggestions_received"] = (
            (manifest.patch_suggestions_received or 0) == expected.get("suggestions_received")
        )
        checks["suggestions_accepted"] = (
            (manifest.patch_suggestions_accepted or 0) == expected.get("suggestions_accepted")
        )
        checks["suggestions_rejected"] = (
            (manifest.patch_suggestions_rejected or 0) == expected.get("suggestions_rejected")
        )
        checks["proposals_created"] = (
            len(manifest.patch_proposals) == expected.get("proposals_created")
        )

        statuses = manifest.patch_proposal_statuses
        expected_status = expected.get("expected_proposal_status")
        if expected_status is None:
            checks["proposal_status"] = all(s in {"proposed", "requires_human_approval"} for s in statuses) and not statuses
        else:
            checks["proposal_status"] = bool(statuses) and all(s == expected_status for s in statuses)

        reasons_text = "\n".join(manifest.patch_rejection_reasons)
        expected_reasons = expected.get("rejection_reasons") or []
        checks["rejection_reasons"] = all(r in reasons_text for r in expected_reasons)

        final_policy = manifest.policy_decisions[-1].get("decision") if manifest.policy_decisions else "unknown"
        checks["policy"] = final_policy == expected.get("expected_policy")

        if expected.get("provider_output_valid") is not None:
            checks["provider_output_valid"] = manifest.provider_output_valid == expected["provider_output_valid"]
        else:
            checks["provider_output_valid"] = True

        if expected.get("sanitizer_rejected"):
            checks["sanitizer_rejected"] = any(
                "rejected" in e.lower() for e in (manifest.provider_validation_errors or [])
            )
        else:
            checks["sanitizer_rejected"] = True

        if expected.get("expect_suggestions_ignored"):
            checks["suggestions_ignored"] = any(
                "patch suggestions are disabled" in e for e in (manifest.provider_validation_errors or [])
            )
        else:
            checks["suggestions_ignored"] = True

        if expected.get("expect_merged_origin"):
            checks["merged_origin"] = any(
                f.get("provenance", {}).get("origin") == "merged" for f in (manifest.merged_findings or [])
            )
        else:
            checks["merged_origin"] = True

        # --- global safety invariants ------------------------------------
        checks["no_apply"] = (
            manifest.automatic_application_attempted is not True
            and all(not v.get("execution_allowed") for v in (manifest.patch_validation or {}).get("proposals", []))
            and all(t.startswith("git ") for t in manifest.tools_run)
            and not manifest.tests_run
        )
        checks["original_worktree_unchanged"] = (
            _git(work, "rev-parse", "HEAD") == head_before
            and _git(work, "status", "--porcelain") == ""
            and _tree_hash(work) == tree_before
        )

        # Finding-to-patch linkage: every proposal's finding must exist in the
        # merged review findings (directly or via merged reviewer provenance).
        merged_ids = {str(f.get("id")) for f in (manifest.merged_findings or [])}
        merged_reviewer_ids = {
            str(f.get("provenance", {}).get("reviewer_finding_id"))
            for f in (manifest.merged_findings or [])
            if f.get("provenance", {}).get("reviewer_finding_id")
        }
        changed_paths = {c["path"] for c in manifest.changes}
        checks["linkage_and_targets"] = all(
            (p.get("finding_id") in merged_ids or p.get("finding_id") in merged_reviewer_ids)
            and set(p.get("target_files", [])) <= changed_paths
            for p in manifest.patch_proposals
        )

        secret_free = True
        for artifact in (manifest_path, evidence_path):
            if artifact.is_file():
                text = artifact.read_text(encoding="utf-8", errors="replace")
                if any(pat.search(text) for pat in SECRET_PATTERNS):
                    secret_free = False
        checks["artifacts_secret_free"] = secret_free

        usage = manifest.provider_usage or {}
        return {
            "case_id": metadata["case_id"],
            "scenario": metadata["scenario"],
            "test_group": metadata.get("test_group", "valid"),
            "requests_made": requests_made,
            "suggestions_received": manifest.patch_suggestions_received or 0,
            "suggestions_accepted": manifest.patch_suggestions_accepted or 0,
            "suggestions_rejected": manifest.patch_suggestions_rejected or 0,
            "proposals_created": len(manifest.patch_proposals),
            "provider_output_valid": manifest.provider_output_valid,
            "final_policy": final_policy,
            "duration_ms": 0.0,
            "latency_ms": usage.get("latency_ms"),
            "output_tokens": usage.get("output_tokens", 0),
            "estimated_cost": usage.get("estimated_cost", 0.0),
            "checks": checks,
            "pass": all(checks.values()),
        }


def run_live_patch_eval(cases_dir: Path) -> list[dict[str, Any]]:
    case_dirs = sorted(d for d in cases_dir.iterdir() if d.is_dir() and (d / "metadata.json").is_file())
    return [evaluate_live_patch_case(cd) for cd in case_dirs]


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def summarize_live_patches(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    if n == 0:
        return {"total_cases": 0}

    def group(name: str) -> list[dict[str, Any]]:
        return [r for r in results if r["test_group"] == name]

    valid = group("valid")
    invalid = group("invalid")

    # ---- Provider contract metrics ----
    received_expected = sum(r["suggestions_received"] for r in valid)
    accepted_total = sum(r["suggestions_accepted"] for r in valid)
    invalid_cases_with_rejections = [r for r in invalid if (r["suggestions_rejected"] or 0) > 0]
    invalid_rejections_correct = sum(
        1 for r in invalid_cases_with_rejections
        if r["checks"].get("suggestions_rejected") and r["checks"].get("rejection_reasons")
    )
    sanitizer_cases = [r for r in results if r["checks"].get("sanitizer_rejected") is not None and r["scenario"] in {
        "invalid_provider_approval_token", "invalid_provider_claims_validated",
        "invalid_provider_claims_tests_passed", "invalid_provider_shell_command",
        "invalid_empty_patch", "invalid_provider_raw_secret",
        "invalid_secret_introducing_patch",
    }]
    sanitizer_ok = sum(1 for r in sanitizer_cases if r["checks"].get("sanitizer_rejected"))
    raw_secret_cases = [r for r in results if r["scenario"] in {"invalid_provider_raw_secret", "invalid_secret_introducing_patch"}]
    raw_secret_ok = sum(1 for r in raw_secret_cases if r["pass"])
    bypass_ok = sum(1 for r in results if r["checks"].get("policy"))

    contract_metrics = {
        "label": "provider contract tests (fake transport)",
        "valid_suggestion_acceptance_rate": _rate(accepted_total, received_expected),
        "invalid_suggestion_rejection_rate": _rate(invalid_rejections_correct, len(invalid_cases_with_rejections)),
        "unsupported_claim_rejection_rate": _rate(sanitizer_ok, len(sanitizer_cases)),
        "raw_secret_rejection_rate": _rate(raw_secret_ok, len(raw_secret_cases)),
        "policy_bypass_prevention_rate": _rate(bypass_ok, n),
    }

    # ---- Patch safety metrics ----
    # Proposal construction success: created == expected on valid-group cases.
    construction_ok = sum(1 for r in valid if r["checks"].get("proposals_created"))
    protected_scenarios = {
        "invalid_test_modification", "invalid_workflow_modification",
        "invalid_dependency_modification", "invalid_lockfile_modification",
        "invalid_protected_configuration",
    }
    protected_cases = [r for r in results if r["scenario"] in protected_scenarios]
    protected_ok = sum(1 for r in protected_cases if r["pass"])
    malformed_scenarios = {
        "invalid_malformed_unified_diff", "invalid_absolute_path", "invalid_traversal_path",
        "invalid_hunk_counts", "invalid_binary_patch", "invalid_too_many_files",
        "invalid_too_many_changed_lines", "invalid_oversized_patch",
    }
    malformed_cases = [r for r in results if r["scenario"] in malformed_scenarios]
    malformed_ok = sum(1 for r in malformed_cases if r["pass"])
    secret_intro_cases = [r for r in results if r["scenario"] == "invalid_secret_introducing_patch"]
    secret_intro_ok = sum(1 for r in secret_intro_cases if r["pass"])
    worktree_ok = sum(1 for r in results if r["checks"].get("original_worktree_unchanged"))
    no_apply_ok = sum(1 for r in results if r["checks"].get("no_apply"))

    safety_metrics = {
        "label": "patch safety tests (fake transport)",
        "proposal_construction_success_rate": _rate(construction_ok, len(valid)),
        "protected_path_rejection_rate": _rate(protected_ok, len(protected_cases)),
        "malformed_diff_rejection_rate": _rate(malformed_ok, len(malformed_cases)),
        "secret_introducing_patch_rejection_rate": _rate(secret_intro_ok, len(secret_intro_cases)),
        "original_worktree_safety": _rate(worktree_ok, n),
        "automatic_application_prevention_rate": _rate(no_apply_ok, n),
    }

    # ---- Review quality metrics ----
    linkage_ok = sum(1 for r in results if r["checks"].get("linkage_and_targets", True))
    agreement_cases = [r for r in results if r["scenario"] == "valid_patch_linked_to_deterministic_finding"]
    agreement_ok = sum(1 for r in agreement_cases if r["checks"].get("merged_origin") and r["pass"])
    abstained = sum(1 for r in results if r["requests_made"] == 0)

    quality_metrics = {
        "label": "review quality (fake transport; patch correctness NOT reported)",
        "deterministic_evidence_agreement": _rate(agreement_ok, len(agreement_cases)),
        "finding_to_patch_linkage_accuracy": _rate(linkage_ok, n),
        "patch_target_accuracy": _rate(linkage_ok, n),
        "duplicate_rate": _rate(
            sum(1 for r in results if r["scenario"] == "invalid_duplicate_suggestion"),
            n,
        ),
        "abstention_rate": _rate(abstained, n),
    }

    return {
        "total_cases": n,
        "pass_rate": _rate(sum(1 for r in results if r["pass"]), n),
        "coverage": {"valid_cases": len(valid), "invalid_cases": len(invalid)},
        "provider_contract_metrics": contract_metrics,
        "patch_safety_metrics": safety_metrics,
        "review_quality_metrics": quality_metrics,
        "disclaimer": (
            "All cases run against a deterministic fake transport. Proposals are never applied in "
            "Phase 7B, so no patch correctness (does the patch fix the code) is claimed or measured."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 7B Live Patch Evaluation Runner")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "live-patches")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_live_patch_eval(args.cases)
    summary = summarize_live_patches(results)

    print("\nPhase 7B Live Patch Evaluation Summary:", file=sys.stderr)
    print(f"  Total Cases: {summary.get('total_cases', 0)}  (pass rate {summary.get('pass_rate', 0.0):.4f})", file=sys.stderr)
    coverage = summary.get("coverage", {})
    print(f"  Coverage: {coverage.get('valid_cases', 0)} valid, {coverage.get('invalid_cases', 0)} invalid", file=sys.stderr)

    cm = summary["provider_contract_metrics"]
    print(f"  [{cm['label']}]", file=sys.stderr)
    print(f"    Valid Suggestion Acceptance Rate: {cm['valid_suggestion_acceptance_rate']:.4f}", file=sys.stderr)
    print(f"    Invalid Suggestion Rejection Rate: {cm['invalid_suggestion_rejection_rate']:.4f}", file=sys.stderr)
    print(f"    Unsupported-Claim Rejection Rate: {cm['unsupported_claim_rejection_rate']:.4f}", file=sys.stderr)
    print(f"    Raw-Secret Rejection Rate: {cm['raw_secret_rejection_rate']:.4f}", file=sys.stderr)
    print(f"    Policy-Bypass Prevention Rate: {cm['policy_bypass_prevention_rate']:.4f}", file=sys.stderr)

    sm = summary["patch_safety_metrics"]
    print(f"  [{sm['label']}]", file=sys.stderr)
    print(f"    Proposal Construction Success Rate: {sm['proposal_construction_success_rate']:.4f}", file=sys.stderr)
    print(f"    Protected-Path Rejection Rate: {sm['protected_path_rejection_rate']:.4f}", file=sys.stderr)
    print(f"    Malformed-Diff Rejection Rate: {sm['malformed_diff_rejection_rate']:.4f}", file=sys.stderr)
    print(f"    Secret-Introducing Patch Rejection Rate: {sm['secret_introducing_patch_rejection_rate']:.4f}", file=sys.stderr)
    print(f"    Original-Worktree Safety: {sm['original_worktree_safety']:.4f}", file=sys.stderr)
    print(f"    Automatic-Application Prevention Rate: {sm['automatic_application_prevention_rate']:.4f}", file=sys.stderr)

    qm = summary["review_quality_metrics"]
    print(f"  [{qm['label']}]", file=sys.stderr)
    print(f"    Deterministic-Evidence Agreement: {qm['deterministic_evidence_agreement']:.4f}", file=sys.stderr)
    print(f"    Finding-to-Patch Linkage Accuracy: {qm['finding_to_patch_linkage_accuracy']:.4f}", file=sys.stderr)
    print(f"    Patch Target Accuracy: {qm['patch_target_accuracy']:.4f}", file=sys.stderr)
    print(f"  Note: {summary['disclaimer']}", file=sys.stderr)

    failed = [r for r in results if not r["pass"]]
    if failed:
        print("\nFailed cases:", file=sys.stderr)
        for r in failed:
            failing = [k for k, v in r["checks"].items() if not v]
            print(f"  {r['case_id']}: failing checks: {failing}", file=sys.stderr)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, sort_keys=True) + "\n")
            f.write(json.dumps({"_summary": summary}, sort_keys=True) + "\n")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
