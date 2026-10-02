"""Unit and integration tests for Phase 8B-2 selective full-suite execution."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
import pytest

from codeatlas.patching.apply import apply_patch_in_isolated_sandbox
from codeatlas.patching.models import PatchStatus
from codeatlas.patching.proposal import (
    create_patch_proposal,
    generate_approval_token,
)
from codeatlas.patching.validator import validate_patch_proposal

CALC_PY = "def add(a, b):\n    return a + b\n"
TEST_CALC_PY = "from src.calc import add\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_OTHER_PY = "def test_other():\n    assert True\n"
FAIL_TEST_CALC_PY = "from src.calc import add\ndef test_add():\n    assert add(1, 2) == 999\n"

DIFF_PASS = (
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def add(a, b):\n"
    "+    # safe add\n"
    "     return a + b\n"
)


def _init_repo(repo_dir: Path, test_content: str = TEST_CALC_PY, extra_test: str | None = None) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "src").mkdir(parents=True, exist_ok=True)
    (repo_dir / "src" / "calc.py").write_text(CALC_PY, encoding="utf-8")
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_calc.py").write_text(test_content, encoding="utf-8")
    if extra_test:
        (repo_dir / "tests" / "test_other.py").write_text(extra_test, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "eval@example.invalid")
    git("config", "user.name", "eval")
    git("add", "-A")
    git("commit", "-qm", "init")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()


def test_disabled_by_default(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-DEFAULT",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Check default disabled state",
        expected_behavior="Full-suite execution is disabled by default",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_tests=False,
        run_full_suite=False,
    )
    assert res.valid is True
    assert res.full_suite_requested is False
    assert res.full_suite_status == "not_run"
    assert res.full_suite_command is None
    assert res.full_suite_result is None


def test_cli_flag_without_policy_opt_in(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-NO-OPTIN",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Flag without policy opt-in",
        expected_behavior="Fails closed with blocked reason",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={},
    )
    assert res.valid is False
    assert res.full_suite_requested is True
    assert res.full_suite_policy_opted_in is False
    assert res.full_suite_status == "blocked"
    assert "policy" in (res.full_suite_blocked_reason or "").lower()


def test_policy_opt_in_without_approval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-NO-TOKEN",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Opt-in without approval token",
        expected_behavior="Fails closed on approval token verification",
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=None,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True},
    )
    assert res.valid is False
    assert res.approval_verified is False
    assert res.full_suite_policy_opted_in is True
    assert res.full_suite_status == "blocked"


def test_valid_full_suite_execution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo, extra_test=TEST_OTHER_PY)

    proposal = create_patch_proposal(
        finding_id="F-8B2-VALID",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Valid full-suite run",
        expected_behavior="Runs full suite and passes",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True},
    )
    assert res.valid is True
    assert res.full_suite_requested is True
    assert res.full_suite_policy_opted_in is True
    assert res.full_suite_status == "passed"
    assert res.full_suite_command is not None
    assert any("pytest" in c for c in res.full_suite_command)
    assert res.full_suite_result is not None
    assert res.full_suite_result["status"] == "passed"
    assert res.full_suite_result["tests_passed"] >= 2


def test_unsupported_language(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    # Patch targeting unsupported language file
    diff_unsupported = (
        "--- a/src/main.rs\n"
        "+++ b/src/main.rs\n"
        "@@ -1 +1,2 @@\n"
        " fn main() {}\n"
        "+// comment\n"
    )
    (repo / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add rs"], cwd=repo, check=True)
    new_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    proposal = create_patch_proposal(
        finding_id="F-8B2-UNSUPPORTED",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=new_sha,
        target_files=["src/main.rs"],
        unified_diff=diff_unsupported,
        rationale="Unsupported language",
        expected_behavior="Blocks full-suite execution",
    )
    token = generate_approval_token(
        proposal.proposal_id, new_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True},
    )
    assert res.valid is False
    assert res.full_suite_status == "blocked"
    assert "unsupported" in (res.full_suite_blocked_reason or "").lower()


def test_missing_runner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-MISSING-RUNNER",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Simulated missing runner",
        expected_behavior="Blocks test execution without attempting dependency install",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True, "missing_runner": True},
    )
    assert res.valid is False
    assert res.full_suite_status == "blocked"
    assert "not installed" in (res.full_suite_blocked_reason or "").lower() or "missing" in (res.full_suite_blocked_reason or "").lower()


def test_dependency_install_command(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-DEP-INSTALL",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Reject dependency install",
        expected_behavior="Blocks package manager command",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True, "override_command": ["pip", "install", "pytest"]},
    )
    assert res.valid is False
    assert res.full_suite_status == "blocked"
    assert "forbidden" in (res.full_suite_blocked_reason or "").lower() or "prohibited" in (res.full_suite_blocked_reason or "").lower()


def test_shell_command(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-SHELL",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Reject shell operator / injection",
        expected_behavior="Command policy rejects prohibited command",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True, "override_command": ["bash", "-c", "echo hello"]},
    )
    assert res.valid is False
    assert res.full_suite_status == "blocked"
    assert "prohibited" in (res.full_suite_blocked_reason or "").lower() or "forbidden" in (res.full_suite_blocked_reason or "").lower()


def test_timeout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    hang_test = "import time\ndef test_sleep():\n    time.sleep(2.0)\n"
    base_sha = _init_repo(repo, test_content=hang_test)

    proposal = create_patch_proposal(
        finding_id="F-8B2-TIMEOUT",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Timeout enforcement",
        expected_behavior="Terminates on timeout",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_timeout=0.3,
        test_config={"allow_full_suite": True},
    )
    assert res.valid is False
    assert res.full_suite_status == "timed_out"
    assert "timed out" in (res.full_suite_blocked_reason or "").lower()


def test_excessive_output(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    noisy_test = "def test_noisy():\n    print('A' * 20000)\n"
    base_sha = _init_repo(repo, test_content=noisy_test)

    proposal = create_patch_proposal(
        finding_id="F-8B2-OUTPUT",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Excessive output bounding",
        expected_behavior="Output truncated to limit",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        max_output_bytes=500,
        test_config={"allow_full_suite": True},
    )
    assert res.full_suite_status == "passed"
    assert res.full_suite_result is not None
    assert res.full_suite_result.get("output_truncated") is True


def test_network_required_test(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-NETWORK",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Network required",
        expected_behavior="Blocks when test requires network",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True, "network_required": True},
    )
    assert res.valid is False
    assert res.full_suite_status == "blocked"
    assert "network" in (res.full_suite_blocked_reason or "").lower()


def test_original_worktree_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-WORKTREE",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Verify worktree safety",
        expected_behavior="Original worktree status and commit remain clean",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True},
    )
    assert res.valid is True

    # Original repo status must be completely clean
    git_status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
    assert git_status.strip() == ""
    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert git_head == base_sha


def test_cleanup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8B2-CLEANUP",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Verify worktree cleanup",
        expected_behavior="Worktree sandbox is pruned",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True},
    )
    assert res.cleanup_status == "completed"
    assert res.sandbox_retained is False

    # Check git worktree list has only the main worktree
    wt_list = subprocess.check_output(["git", "worktree", "list", "--porcelain"], cwd=repo, text=True)
    assert wt_list.count("worktree ") == 1


def test_redacted_diagnostics(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    leak_test = (
        "def test_leak():\n"
        "    token = 'ghp_' + '111122223333444455556666777788889999'\n"
        "    raise ValueError(f'Leaked secret: {token}')\n"
    )
    base_sha = _init_repo(repo, test_content=leak_test)

    proposal = create_patch_proposal(
        finding_id="F-8B2-REDACT",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Redaction in diagnostics",
        expected_behavior="Secret tokens are redacted in all output summaries",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_full_suite=True,
        test_config={"allow_full_suite": True},
    )
    assert res.valid is False
    assert res.full_suite_status == "failed"

    raw_secret = "ghp_111122223333444455556666777788889999"
    assert raw_secret not in (res.diagnostic_summary or "")
    assert raw_secret not in (res.test_stdout_summary or "")
    assert raw_secret not in (res.test_stderr_summary or "")
    if res.full_suite_result:
        assert raw_secret not in str(res.full_suite_result)


def test_phase_8a_targeted_test_regression(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo, extra_test=TEST_OTHER_PY)

    proposal = create_patch_proposal(
        finding_id="F-8B2-REGRESSION",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Phase 8A regression verification",
        expected_behavior="Targeted tests run unchanged when --run-tests is passed",
    )
    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )

    res = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root=repo,
        approval_token=token,
        run_id="run-1",
        run_tests=True,
        run_full_suite=False,
    )
    assert res.valid is True
    assert res.tests_status == "passed"
    assert res.full_suite_requested is False
    assert res.full_suite_status == "not_run"
    assert res.tests_run == ["tests/test_calc.py"]
    # Verify targeted test only ran test_calc.py, not test_other.py
    assert "tests/test_other.py" not in res.tests_run
