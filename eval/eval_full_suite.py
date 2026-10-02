"""Phase 8B-2 selective full-suite execution evaluation runner.

Executes the real CLI (``codeatlas patch validate --run-full-suite``) against isolated Git fixtures.
Evaluates Execution, Safety, and Policy gates.
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
    return None


def evaluate_full_suite_case(case_dir: Path) -> dict[str, Any]:
    metadata = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
    expected = metadata["expected"]
    run_id = metadata.get("run_id", "default")

    with tempfile.TemporaryDirectory(prefix="codeatlas-fullsuite-") as temp:
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

        proposal_path = Path(temp) / "proposal.json"
        prop_data = json.loads((case_dir / "proposal.json").read_text(encoding="utf-8"))
        prop_data["base_commit"] = base_sha
        proposal_path.write_text(json.dumps(prop_data, indent=2), encoding="utf-8")

        token = _build_token(metadata.get("token", "valid"), prop_data, base_sha, run_id)
        tree_before = _tree_hash(work)
        report_path = Path(temp) / "report.json"
        evidence_path = Path(temp) / "evidence.jsonl"
        config_path = Path(temp) / "config.json"

        if metadata.get("test_config"):
            cfg = {"test": metadata["test_config"]}
            config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        cmd = [
            "python", "-m", "codeatlas.cli", "patch", "validate",
            "--proposal", str(proposal_path),
            "--repo", str(work),
            "--base", base_sha,
            "--report-output", str(report_path),
            "--evidence-output", str(evidence_path),
            "--run-id", run_id,
        ]
        if metadata.get("run_full_suite", False):
            cmd += ["--run-full-suite"]
        if metadata.get("run_tests", False):
            cmd += ["--run-tests"]
        if token is not None:
            cmd += ["--approval-token", token]
        if metadata.get("test_timeout"):
            cmd += ["--test-timeout", str(metadata["test_timeout"])]
        if metadata.get("max_output_bytes"):
            cmd += ["--max-output-bytes", str(metadata["max_output_bytes"])]
        if metadata.get("test_config"):
            cmd += ["--config", str(config_path)]

        started = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        duration_ms = (time.perf_counter() - started) * 1000

        checks: dict[str, bool] = {}
        checks["exit"] = proc.returncode == expected.get("exit")

        report: dict[str, Any] | None = None
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                report = None

        if report:
            checks["status"] = report.get("proposal_status") == expected["status"]
            if expected.get("full_suite_requested") is not None:
                checks["full_suite_requested"] = report.get("full_suite_requested") == expected["full_suite_requested"]
            if expected.get("full_suite_status") is not None:
                checks["full_suite_status"] = report.get("full_suite_status") == expected["full_suite_status"]
            if expected.get("full_suite_policy_opted_in") is not None:
                checks["full_suite_policy_opted_in"] = report.get("full_suite_policy_opted_in") == expected["full_suite_policy_opted_in"]
            if expected.get("output_truncated") is not None:
                checks["output_truncated"] = (
                    report.get("test_output_truncated") is True
                    or (report.get("full_suite_result") or {}).get("output_truncated") is True
                )
        else:
            checks["status"] = True
            checks["full_suite_status"] = True

        # Safety & Worktree checks
        checks["worktree_unchanged"] = _tree_hash(work) == tree_before
        checks["network_prevented"] = (report is None or report.get("network_allowed") is False)
        checks["dependency_install_prevented"] = (report is None or report.get("dependency_install_allowed") is False)

        # Artifacts check for secrets & tokens
        artifacts_text = ""
        for art in (report_path, evidence_path):
            if art.is_file():
                artifacts_text += art.read_text(encoding="utf-8", errors="replace")
        checks["no_token_in_artifacts"] = (token is None) or (token not in artifacts_text)
        checks["artifacts_secret_free"] = not any(pat.search(artifacts_text) for pat in SECRET_PATTERNS)

        return {
            "case_id": metadata["case_id"],
            "scenario": metadata["scenario"],
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "checks": checks,
            "pass": all(checks.values()),
        }


def run_full_suite_eval(cases_dir: Path) -> list[dict[str, Any]]:
    case_dirs = sorted(d for d in cases_dir.iterdir() if d.is_dir() and (d / "metadata.json").is_file())
    return [evaluate_full_suite_case(cd) for cd in case_dirs]


def main() -> int:
    cases_dir = Path(__file__).resolve().parent / "cases" / "full-suite"
    print(f"Evaluating Phase 8B-2 Full-Suite Execution on {cases_dir}...", file=sys.stderr)
    results = run_full_suite_eval(cases_dir)
    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    print("\nPhase 8B-2 Selective Full-Suite Execution Evaluation Summary:", file=sys.stderr)
    print(f"  Total Cases: {total}", file=sys.stderr)
    print(f"  Passed Cases: {passed}/{total} ({passed/total*100:.1f}%)", file=sys.stderr)
    for r in results:
        status_str = "PASS" if r["pass"] else "FAIL"
        failed_checks = [k for k, v in r["checks"].items() if not v]
        suffix = f" (failed: {', '.join(failed_checks)})" if failed_checks else ""
        print(f"  [{status_str}] {r['case_id']}{suffix}", file=sys.stderr)

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
