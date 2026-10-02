"""Phase 7C isolated-validation evaluation runner.

Executes the real CLI (``codeatlas patch apply-isolated``) against isolated Git
fixtures with computed scoped approval tokens.  No network calls are made, no
repository tests or builds run, and the original repository is never modified.

Metric groups are reported separately and explicitly labelled: approval,
isolation, validation, and execution policy.  A clean application with valid
syntax never implies the patch is correct.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from codeatlas.patching.proposal import generate_approval_token
from codeatlas.review.packet import SECRET_PATTERNS


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return result.stdout.strip()


def _tree_hash(work: Path) -> str:
    digest = hashlib.sha256()
    for p in sorted(work.rglob("*")):
        if ".git" in p.relative_to(work).parts:
            continue
        if p.is_file():
            digest.update(str(p.relative_to(work)).encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


def _build_token(scenario: str, prop_data: dict, base_sha: str, run_id: str) -> str | None:
    pid = prop_data["proposal_id"]
    p_hash = prop_data["patch_hash"]
    files = prop_data["target_files"]
    if scenario == "valid":
        return generate_approval_token(pid, prop_data["base_commit"], p_hash, files, run_id=run_id)
    if scenario == "missing":
        return None
    if scenario == "malformed":
        return "CAT-APP-notarealtoken0000000000000000"
    if scenario == "other_proposal":
        return generate_approval_token("prop-other", prop_data["base_commit"], p_hash, files, run_id=run_id)
    if scenario == "other_commit":
        return generate_approval_token(pid, "1" * 40, p_hash, files, run_id=run_id)
    if scenario == "other_hash":
        return generate_approval_token(pid, prop_data["base_commit"], "0" * 64, files, run_id=run_id)
    if scenario == "other_files":
        return generate_approval_token(pid, prop_data["base_commit"], p_hash, ["src/other.py"], run_id=run_id)
    if scenario == "provider_embedded_used":
        return prop_data.get("provenance", {}).get("provider_approval_token")
    raise ValueError(f"unknown token scenario: {scenario}")


def evaluate_isolated_validation_case(case_dir: Path) -> dict[str, Any]:
    metadata = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
    expected = metadata["expected"]
    run_id = metadata.get("run_id", "default")

    with tempfile.TemporaryDirectory(prefix="codeatlas-isoval-") as temp:
        work = Path(temp) / "repo"
        work.mkdir()
        _git(work, "init", "-q")
        _git(work, "config", "user.email", "eval@example.invalid")
        _git(work, "config", "user.name", "eval")
        for rel_path in sorted((case_dir / "base").rglob("*")):
            if rel_path.is_file() and "__pycache__" not in rel_path.parts and rel_path.suffix != ".pyc":
                target = work / rel_path.relative_to(case_dir / "base")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(rel_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "base")
        base_sha = _git(work, "rev-parse", "HEAD")

        # Prepare the proposal file for this run.
        proposal_path = Path(temp) / "proposal.json"
        token_scenario = metadata.get("token", "valid")
        prop_data: dict[str, Any] | None = None
        if metadata.get("proposal_file") == "missing":
            proposal_path = Path(temp) / "nonexistent-proposal.json"
        elif metadata.get("proposal_file") == "malformed":
            proposal_path.write_text("{ this is not json", encoding="utf-8")
        else:
            prop_data = json.loads((case_dir / "proposal.json").read_text(encoding="utf-8"))
            if metadata.get("base_commit") != "stale":
                prop_data["base_commit"] = base_sha
            proposal_path.write_text(json.dumps(prop_data, indent=2), encoding="utf-8")

        token = (
            _build_token(token_scenario, prop_data, base_sha, run_id)
            if prop_data is not None
            else None
        )

        # Optional containment scenario: dirty the original worktree.
        if metadata.get("dirty_worktree"):
            (work / "src" / "util.py").write_text(
                (work / "src" / "util.py").read_text(encoding="utf-8") + "# local uncommitted edit\n",
                encoding="utf-8",
            )

        tree_before = _tree_hash(work)
        report_path = Path(temp) / "report.json"
        evidence_path = Path(temp) / "evidence.jsonl"

        cmd = [
            "python", "-m", "codeatlas.cli", "patch", "apply-isolated",
            "--proposal", str(proposal_path),
            "--repo", str(work),
            "--base", base_sha,
            "--report-output", str(report_path),
            "--evidence-output", str(evidence_path),
            "--run-id", run_id,
        ]
        if token is not None:
            cmd += ["--approval-token", token]
        if metadata.get("retain_sandbox_on_failure"):
            cmd += ["--retain-sandbox-on-failure"]

        started = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        duration_ms = (time.perf_counter() - started) * 1000

        checks: dict[str, bool] = {}
        checks["exit"] = proc.returncode == expected.get("exit")

        report: dict[str, Any] | None = None
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))

        if expected.get("status") is None:
            # CLI-level load failures: no report, clear stderr.
            checks["lifecycle"] = report is None and (
                "not found" in proc.stderr or "parsing" in proc.stderr.lower()
            )
            checks["approval"] = True
            checks["applies"] = True
            checks["syntax"] = True
            checks["cleanup"] = True
        else:
            checks["lifecycle"] = bool(report) and report.get("proposal_status") == expected["status"]
            checks["approval"] = bool(report) and report.get("approval_verified") == expected.get("approval_verified")
            checks["applies"] = bool(report) and report.get("applies_cleanly") == expected.get("applies_cleanly")
            checks["syntax"] = bool(report) and report.get("syntax_valid") == expected.get("syntax_valid")
            checks["cleanup"] = bool(report) and report.get("cleanup_status") == expected.get("cleanup_status")

        # Safety invariants that hold for every case.
        checks["worktree_unchanged"] = _tree_hash(work) == tree_before
        checks["tests_build_not_run"] = (
            report is None
            or (report.get("tests_status") == "not_run" and report.get("build_status") == "not_run")
        )
        checks["execution_allowed_false"] = report is None or report.get("execution_allowed") is False

        # Command audit: only narrowly scoped git operations are permitted.
        commands = (report or {}).get("commands_run") or []
        checks["only_git_commands"] = all(
            c.startswith("git ") or c.startswith("git status") or c == "git apply --check" or c == "git apply"
            for c in commands
        )

        # Evidence lifecycle and no-token guarantee.
        events: list[str] = []
        if evidence_path.is_file():
            events = [json.loads(line)["event"] for line in evidence_path.read_text(encoding="utf-8").splitlines() if line]
            checks["evidence_lifecycle"] = "patch_validation_requested" in events and (
                "patch_validation_completed" in events
                or "patch_validation_rejected" in events
                or "approval_verification_failed" in events
            )
        else:
            checks["evidence_lifecycle"] = metadata.get("proposal_file") != "normal"
        artifacts_text = ""
        for artifact in (evidence_path, report_path):
            if artifact.is_file():
                artifacts_text += artifact.read_text(encoding="utf-8", errors="replace")
        checks["no_token_in_artifacts"] = (token is None) or (token not in artifacts_text)
        checks["artifacts_secret_free"] = not any(pat.search(artifacts_text) for pat in SECRET_PATTERNS)

        scenario = metadata["scenario"]
        return {
            "case_id": metadata["case_id"],
            "scenario": scenario,
            "group": (
                "approval" if scenario.startswith(("missing_", "malformed_", "cross_", "provider_"))
                else "isolation" if "worktree" in scenario or "retain" in scenario
                else "validation"
            ),
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "events": events,
            "checks": checks,
            "pass": all(checks.values()),
        }


def run_isolated_validation_eval(cases_dir: Path) -> list[dict[str, Any]]:
    case_dirs = sorted(d for d in cases_dir.iterdir() if d.is_dir() and (d / "metadata.json").is_file())
    return [evaluate_isolated_validation_case(cd) for cd in case_dirs]


def _rate(n: int, d: int) -> float:
    return n / d if d else 1.0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    passed = sum(1 for r in results if r["pass"])

    def scen(prefix: str) -> list[dict[str, Any]]:
        return [r for r in results if r["scenario"].startswith(prefix)]

    valid_token = [r for r in results if r["scenario"].startswith("valid_") or r["scenario"] == "dirty_original_worktree_containment"]
    invalid_token = scen("missing_") + scen("malformed_") + scen("cross_") + scen("provider_")
    stale_conflict = scen("stale_") + scen("patch_conflict_")
    protected = scen("protected_")
    secret = scen("secret_")

    metrics = {
        "total_cases": n,
        "pass_rate": _rate(passed, n),
        "approval_metrics": {
            "label": "approval tests",
            "valid_token_acceptance_rate": _rate(sum(1 for r in valid_token if r["pass"]), len(valid_token)),
            "invalid_token_rejection_rate": _rate(sum(1 for r in invalid_token if r["pass"]), len(invalid_token)),
            "cross_proposal_token_rejection_rate": _rate(
                sum(1 for r in scen("cross_proposal") if r["pass"]), len(scen("cross_proposal"))),
            "cross_commit_token_rejection_rate": _rate(
                sum(1 for r in scen("cross_commit") if r["pass"]), len(scen("cross_commit"))),
            "provider_token_rejection_rate": _rate(
                sum(1 for r in scen("provider_") if r["pass"]), len(scen("provider_"))),
        },
        "isolation_metrics": {
            "label": "isolation tests",
            "original_worktree_safety_rate": _rate(
                sum(1 for r in results if r["checks"]["worktree_unchanged"]), n),
            "sandbox_cleanup_rate": _rate(
                sum(1 for r in results if r["checks"]["cleanup"]), n),
            "sandbox_mutation_containment_rate": _rate(
                sum(1 for r in scen("dirty_original") if r["pass"]), len(scen("dirty_original"))),
            "automatic_application_prevention_rate": _rate(
                sum(1 for r in results if r["checks"]["execution_allowed_false"]), n),
        },
        "validation_metrics": {
            "label": "validation tests",
            "applies_cleanly_accuracy": _rate(sum(1 for r in results if r["checks"]["applies"]), n),
            "syntax_validation_accuracy": _rate(sum(1 for r in results if r["checks"]["syntax"]), n),
            "stale_conflict_rejection_rate": _rate(sum(1 for r in stale_conflict if r["pass"]), len(stale_conflict)),
            "protected_path_rejection_rate": _rate(sum(1 for r in protected if r["pass"]), len(protected)),
            "secret_introducing_patch_rejection_rate": _rate(sum(1 for r in secret if r["pass"]), len(secret)),
        },
        "execution_policy_metrics": {
            "label": "execution policy tests",
            "test_execution_prevention_rate": _rate(sum(1 for r in results if r["checks"]["tests_build_not_run"]), n),
            "build_execution_prevention_rate": _rate(sum(1 for r in results if r["checks"]["tests_build_not_run"]), n),
            "dependency_install_prevention_rate": _rate(sum(1 for r in results if r["checks"]["only_git_commands"]), n),
            "arbitrary_command_prevention_rate": _rate(sum(1 for r in results if r["checks"]["only_git_commands"]), n),
        },
        "disclaimer": (
            "Clean application and valid syntax do not imply the patch is correct. Tests and builds "
            "were never executed; no patch correctness is claimed."
        ),
    }
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 7C Isolated Validation Evaluation Runner")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "isolated-validation")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_isolated_validation_eval(args.cases)
    summary = summarize(results)

    print("\nPhase 7C Isolated Validation Evaluation Summary:", file=sys.stderr)
    print(f"  Total Cases: {summary['total_cases']}  (pass rate {summary['pass_rate']:.4f})", file=sys.stderr)
    for group in ("approval_metrics", "isolation_metrics", "validation_metrics", "execution_policy_metrics"):
        m = summary[group]
        print(f"  [{m['label']}]", file=sys.stderr)
        for key, value in m.items():
            if key == "label":
                continue
            print(f"    {key.replace('_', ' ').title()}: {value:.4f}", file=sys.stderr)
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
