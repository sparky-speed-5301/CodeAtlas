"""Phase 6 Patch Generation and Validation Interface Evaluation Runner.

Evaluates patch proposals, policies, sandboxed application, and redaction guarantees
on isolated Git fixtures without executing fixture code, tests, or external LLMs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from codeatlas.patching import (
    PatchProposal,
    PatchStatus,
    PatchValidationResult,
    generate_approval_token,
    validate_patch_proposal,
)
from codeatlas.review.packet import SECRET_PATTERNS


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def evaluate_patching_case(case_dir: Path) -> dict[str, Any]:
    """Evaluate a single patch fixture in an isolated temporary Git repository."""
    meta_path = case_dir / "metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))

    prop_path = case_dir / "proposal.json"
    prop_data = json.loads(prop_path.read_text(encoding="utf-8"))

    base_dir = case_dir / "base"

    with tempfile.TemporaryDirectory(prefix="codeatlas-patch-eval-") as temp_dir:
        work_dir = Path(temp_dir) / "repo"
        work_dir.mkdir()

        _git(work_dir, "init", "-q")
        _git(work_dir, "config", "user.email", "eval@example.invalid")
        _git(work_dir, "config", "user.name", "eval")

        if base_dir.is_dir() and any(base_dir.rglob("*")):
            shutil.copytree(
                base_dir,
                work_dir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            _git(work_dir, "add", "-A")
            _git(work_dir, "commit", "--allow-empty", "-qm", "initial base commit")
        else:
            _git(work_dir, "commit", "--allow-empty", "-qm", "initial base commit")

        base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=work_dir, text=True
        ).strip()

        # Build PatchProposal
        if not metadata.get("wrong_base_commit"):
            prop_data["base_commit"] = base_sha
        proposal = PatchProposal.model_validate(prop_data)

        if metadata.get("tamper_hash"):
            proposal.patch_hash = "0000000000000000000000000000000000000000000000000000000000000000"

        # Approval token setup
        if metadata.get("no_token"):
            token = None
        elif metadata.get("invalid_token"):
            token = "CAT-APP-invalidtoken1234567890abcdef"
        else:
            token = generate_approval_token(
                proposal.proposal_id,
                base_sha,
                proposal.patch_hash,
                proposal.target_files,
            )

        # Pre-check initial worktree status
        pre_status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=work_dir, text=True
        ).strip()

        t0 = time.perf_counter()
        validation_result = validate_patch_proposal(
            proposal,
            repository_root=work_dir,
            approval_token=token,
            allow_isolated_apply=True,
        )
        duration_ms = (time.perf_counter() - t0) * 1000.0

        # Post-check original worktree status
        post_status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=work_dir, text=True
        ).strip()
        worktree_unchanged = (pre_status == post_status) and (post_status == "")

        # Check redaction: confirm no secret leaked in validation results, diagnostics, or status
        has_secret_leak = False
        val_dump = json.dumps(validation_result.model_dump())
        for pat in SECRET_PATTERNS:
            if pat.search(val_dump):
                has_secret_leak = True
                break

        # If this was a secret-introducing test case, confirm it was flagged unsafe
        if metadata.get("case_id") == "case-16-secret-introducing-patch":
            redaction_safe = (not has_secret_leak) and (not proposal.redaction_audit.safe)
        else:
            redaction_safe = (not has_secret_leak) and proposal.redaction_audit.safe

        # Check policy accuracy
        policy_dec = proposal.policy_decision.get("decision") if proposal.policy_decision else None
        expected_policy = metadata.get("expected_policy_decision")
        policy_correct = (policy_dec == expected_policy)

        # Check validation validity match
        valid_match = (validation_result.valid == metadata.get("expected_valid"))

        # Check applies cleanly match
        applies_match = (validation_result.applies_cleanly == metadata.get("expected_applies_cleanly"))

        # Check syntax valid match if specified
        expected_syntax = metadata.get("expected_syntax_valid")
        syntax_match = True
        if expected_syntax is not None:
            syntax_match = (validation_result.syntax_valid == expected_syntax)

        # Invalid proposal rejection check: if expected_valid is False, was it rejected/failed?
        is_expected_invalid = not metadata.get("expected_valid", True)
        rejection_correct = True
        if is_expected_invalid:
            rejection_correct = (not validation_result.valid)

        return {
            "case_id": metadata["case_id"],
            "title": metadata.get("title", ""),
            "duration_ms": duration_ms,
            "worktree_unchanged": worktree_unchanged,
            "redaction_safe": redaction_safe,
            "policy_correct": policy_correct,
            "valid_match": valid_match,
            "applies_match": applies_match,
            "syntax_match": syntax_match,
            "rejection_correct": rejection_correct,
            "is_expected_invalid": is_expected_invalid,
            "validation_result": validation_result.model_dump(),
            "proposal_status": proposal.status,
            "commands_run": validation_result.commands_run,
        }


def run_patching_eval(cases_dir: Path) -> list[dict[str, Any]]:
    """Run all patch validation evaluation cases."""
    results: list[dict[str, Any]] = []
    case_dirs = sorted([d for d in cases_dir.iterdir() if d.is_dir() and (d / "metadata.json").is_file()])
    for cdir in case_dirs:
        res = evaluate_patching_case(cdir)
        results.append(res)
    return results


def summarize_patching(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute benchmark summary metrics with explicit numerators and denominators."""
    n = len(results)
    if n == 0:
        return {"total_cases": 0}

    # Proposal construction success rate
    prop_success = sum(1 for r in results if r["valid_match"] or r["validation_result"]["valid"] or not r["is_expected_invalid"])
    # Redaction safety
    redaction_safe_count = sum(1 for r in results if r["redaction_safe"])
    # Policy accuracy
    policy_correct_count = sum(1 for r in results if r["policy_correct"])
    # Original worktree safety rate
    worktree_safe_count = sum(1 for r in results if r["worktree_unchanged"])
    # Syntax accuracy
    syntax_correct_count = sum(1 for r in results if r["syntax_match"])

    # Invalid proposal rejection rate
    invalid_cases = [r for r in results if r["is_expected_invalid"]]
    n_invalid = len(invalid_cases)
    rejected_correct_count = sum(1 for r in invalid_cases if r["rejection_correct"])

    # Clean isolated application rate (on valid cases)
    valid_cases = [r for r in results if not r["is_expected_invalid"]]
    n_valid = len(valid_cases)
    applied_cleanly_count = sum(1 for r in valid_cases if r["applies_match"])

    durations = sorted(r["duration_ms"] for r in results)
    mean_duration = sum(durations) / n
    p95_idx = int(0.95 * n) - 1
    p95_duration = durations[max(0, p95_idx)]

    return {
        "total_cases": n,
        "proposal_construction_success_rate": prop_success / n,
        "proposal_construction_numerator": prop_success,
        "proposal_construction_denominator": n,
        "redaction_safety": redaction_safe_count / n,
        "redaction_safety_numerator": redaction_safe_count,
        "redaction_safety_denominator": n,
        "policy_decision_accuracy": policy_correct_count / n,
        "policy_decision_numerator": policy_correct_count,
        "policy_decision_denominator": n,
        "invalid_proposal_rejection_rate": (rejected_correct_count / n_invalid) if n_invalid else 1.0,
        "invalid_proposal_rejection_numerator": rejected_correct_count,
        "invalid_proposal_rejection_denominator": n_invalid,
        "clean_isolated_application_rate": (applied_cleanly_count / n_valid) if n_valid else 1.0,
        "clean_isolated_application_numerator": applied_cleanly_count,
        "clean_isolated_application_denominator": n_valid,
        "original_worktree_safety_rate": worktree_safe_count / n,
        "original_worktree_safety_numerator": worktree_safe_count,
        "original_worktree_safety_denominator": n,
        "syntax_validation_accuracy": syntax_correct_count / n,
        "syntax_validation_numerator": syntax_correct_count,
        "syntax_validation_denominator": n,
        "mean_validation_duration_ms": mean_duration,
        "p95_validation_duration_ms": p95_duration,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 6 Patch Validation Evaluation Runner")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "patching")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_patching_eval(args.cases)
    summary = summarize_patching(results)

    print("\nPhase 6 Patching Evaluation Summary:", file=sys.stderr)
    print(f"  Total Cases: {summary.get('total_cases', 0)}", file=sys.stderr)
    print(
        f"  Proposal Construction Success Rate: {summary.get('proposal_construction_success_rate', 0.0):.4f} "
        f"({summary.get('proposal_construction_numerator', 0)}/{summary.get('proposal_construction_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Redaction Safety: {summary.get('redaction_safety', 0.0):.4f} "
        f"({summary.get('redaction_safety_numerator', 0)}/{summary.get('redaction_safety_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Policy Decision Accuracy: {summary.get('policy_decision_accuracy', 0.0):.4f} "
        f"({summary.get('policy_decision_numerator', 0)}/{summary.get('policy_decision_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Invalid Proposal Rejection Rate: {summary.get('invalid_proposal_rejection_rate', 0.0):.4f} "
        f"({summary.get('invalid_proposal_rejection_numerator', 0)}/{summary.get('invalid_proposal_rejection_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Clean Isolated Application Rate: {summary.get('clean_isolated_application_rate', 0.0):.4f} "
        f"({summary.get('clean_isolated_application_numerator', 0)}/{summary.get('clean_isolated_application_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Original Worktree Safety Rate: {summary.get('original_worktree_safety_rate', 0.0):.4f} "
        f"({summary.get('original_worktree_safety_numerator', 0)}/{summary.get('original_worktree_safety_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Syntax Validation Accuracy: {summary.get('syntax_validation_accuracy', 0.0):.4f} "
        f"({summary.get('syntax_validation_numerator', 0)}/{summary.get('syntax_validation_denominator', 0)})",
        file=sys.stderr,
    )
    print(f"  Mean Validation Duration: {summary.get('mean_validation_duration_ms', 0.0):.2f} ms", file=sys.stderr)
    print(f"  P95 Validation Duration: {summary.get('p95_validation_duration_ms', 0.0):.2f} ms", file=sys.stderr)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
            f.write(json.dumps({"summary": summary}) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
