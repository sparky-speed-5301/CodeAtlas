"""Integration tests / CLI smoke tests for Phase 8A test execution."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest

from codeatlas.patching.apply import apply_patch_in_isolated_sandbox
from codeatlas.patching.models import PatchStatus
from codeatlas.patching.proposal import (
    compute_patch_hash,
    create_patch_proposal,
    generate_approval_token,
)

CALC_PY = "def add(a, b):\n    return a + b\n"
TEST_CALC_PY = "from src.calc import add\ndef test_add():\n    assert add(1, 2) == 3\n"
FAIL_TEST_CALC_PY = "from src.calc import add\ndef test_add():\n    assert add(1, 2) == 999\n"

DIFF_PASS = (
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def add(a, b):\n"
    "+    # safe add\n"
    "     return a + b\n"
)


def _init_repo(repo_dir: Path, test_content: str = TEST_CALC_PY) -> str:
    repo_dir.mkdir(parents=True)
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "calc.py").write_text(CALC_PY, encoding="utf-8")
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_calc.py").write_text(test_content, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "eval@example.invalid")
    git("config", "user.name", "eval")
    git("add", "-A")
    git("commit", "-qm", "init")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()


def test_cli_smoke_no_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8A-NO-TESTS",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Smoke test without --run-tests",
        expected_behavior="Passes isolated validation without running tests",
    )
    p_path = tmp_path / "proposal.json"
    p_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )
    rep_path = tmp_path / "report.json"

    cmd = [
        "python", "-m", "codeatlas.cli", "patch", "validate",
        "--proposal", str(p_path),
        "--repo", str(repo),
        "--base", base_sha,
        "--approval-token", token,
        "--run-id", "run-1",
        "--report-output", str(rep_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    assert report["valid"] is True
    assert report["proposal_status"] == "requires_human_approval"
    assert report["tests_status"] == "not_run"


def test_cli_smoke_passing_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8A-PASS-TESTS",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Smoke test with --run-tests passing",
        expected_behavior="Runs targeted tests and passes",
    )
    p_path = tmp_path / "proposal.json"
    p_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )
    rep_path = tmp_path / "report.json"

    cmd = [
        "python", "-m", "codeatlas.cli", "patch", "validate",
        "--proposal", str(p_path),
        "--repo", str(repo),
        "--base", base_sha,
        "--approval-token", token,
        "--run-id", "run-1",
        "--run-tests",
        "--report-output", str(rep_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    assert report["valid"] is True
    assert report["proposal_status"] == "validated"
    assert report["tests_status"] == "passed"
    assert report["test_execution_attempted"] is True


def test_cli_smoke_failing_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo, test_content=FAIL_TEST_CALC_PY)

    proposal = create_patch_proposal(
        finding_id="F-8A-FAIL-TESTS",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Smoke test with --run-tests failing",
        expected_behavior="Runs targeted tests and fails",
    )
    p_path = tmp_path / "proposal.json"
    p_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )
    rep_path = tmp_path / "report.json"

    cmd = [
        "python", "-m", "codeatlas.cli", "patch", "validate",
        "--proposal", str(p_path),
        "--repo", str(repo),
        "--base", base_sha,
        "--approval-token", token,
        "--run-id", "run-1",
        "--run-tests",
        "--report-output", str(rep_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 1
    assert "Test status: failed" in res.stdout
    assert "Runner: pytest" in res.stdout
    assert "Network isolation: not independently verified" in res.stdout

    report = json.loads(rep_path.read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert report["proposal_status"] == "failed_validation"
    assert report["tests_status"] == "failed"
    assert report["test_failure_count"] == 1
    assert report["network_policy_requested"] == "disabled"
    assert report["network_policy_enforced"] is True
    assert report["network_isolation_verified"] is False
    assert report["diagnostic_summary"] is not None


def test_cli_smoke_blocked_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8A-BLOCKED-TESTS",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Smoke test with blocked command",
        expected_behavior="Blocked by allowlist",
    )
    p_path = tmp_path / "proposal.json"
    p_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )
    rep_path = tmp_path / "report.json"
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"test": {"override_command": ["curl", "https://example.com"]}}), encoding="utf-8")

    cmd = [
        "python", "-m", "codeatlas.cli", "patch", "validate",
        "--proposal", str(p_path),
        "--repo", str(repo),
        "--base", base_sha,
        "--approval-token", token,
        "--run-id", "run-1",
        "--run-tests",
        "--config", str(cfg_path),
        "--report-output", str(rep_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 1
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert report["proposal_status"] == "failed_validation"
    assert report["tests_status"] == "blocked"
