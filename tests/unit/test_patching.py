"""Unit and integration tests for Phase 6 patch proposal, policy gating, and isolated sandbox validation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest
from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.patching import (
    DEFAULT_PATCH_CONFIG,
    MockFixer,
    PatchFile,
    PatchHunk,
    PatchPolicyDecision,
    PatchProposal,
    PatchRedactionAudit,
    PatchStatus,
    PatchValidationResult,
    VALID_STATUS_TRANSITIONS,
    apply_patch_in_isolated_sandbox,
    audit_patch_redaction,
    compute_patch_hash,
    create_patch_proposal,
    evaluate_patch_policy,
    generate_approval_token,
    normalize_diff,
    parse_unified_diff,
    validate_patch_proposal,
    verify_approval_token,
)


def _init_test_git_repo(path: Path) -> str:
    """Helper to initialize a clean git repository and return initial commit hash."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)

    src = path / "src"
    src.mkdir(parents=True, exist_ok=True)
    app_file = src / "app.py"
    app_file.write_text("def run():\n    return 42\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, check=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return res.stdout.strip()


def test_patch_proposal_model_and_hash():
    diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n-def run():\n+def run_safe():\n     return 42\n"
    prop = create_patch_proposal(
        finding_id="FIND-001",
        provider_name="test-fixer",
        provider_version="1.0.0",
        base_commit="abc1234",
        target_files=["src/app.py"],
        unified_diff=diff,
        rationale="Fix naming",
        expected_behavior="Safe run",
    )
    assert prop.proposal_id.startswith("prop-")
    assert prop.status == PatchStatus.PROPOSED
    assert prop.patch_hash == compute_patch_hash(diff)
    assert prop.redaction_audit.safe is True


def test_unified_diff_parser_clean():
    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def run():\n"
        "+    print('running')\n"
        "     return 42\n"
    )
    files, errors = parse_unified_diff(diff)
    assert len(errors) == 0
    assert len(files) == 1
    assert files[0].path == "src/app.py"
    assert files[0].operation == "modify"
    assert len(files[0].hunks) == 1
    hunk = files[0].hunks[0]
    assert hunk.old_start == 1
    assert hunk.old_lines == 2
    assert hunk.new_start == 1
    assert hunk.new_lines == 3


def test_unified_diff_parser_malformed_and_limits():
    # Empty diff
    files, errors = parse_unified_diff("")
    assert "Unified diff is empty" in errors[0]

    # Binary diff rejection
    files, errors = parse_unified_diff("GIT binary patch\nliteral 0\n")
    assert any("Binary patch" in err for err in errors)

    # Malformed hunk line count
    broken_diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,10 +1,10 @@\n"
        " def run():\n"
    )
    files, errors = parse_unified_diff(broken_diff)
    assert any("Hunk line count mismatch" in err for err in errors)


def test_path_safety_and_traversal_rejection():
    # Path traversal
    trav_diff = "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    files, errors = parse_unified_diff(trav_diff)
    assert any("Path traversal detected" in err for err in errors)

    # Windows drive letter absolute
    win_diff = "--- a/C:/Windows/System32\n+++ b/C:/Windows/System32\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    files, errors = parse_unified_diff(win_diff)
    assert any("Absolute path detected" in err for err in errors)

    # Root slash absolute
    slash_diff = "--- a//etc/shadow\n+++ b//etc/shadow\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    files, errors = parse_unified_diff(slash_diff)
    assert any("Absolute path detected" in err for err in errors)


def test_policy_path_protections():
    prop = create_patch_proposal(
        finding_id="F1",
        provider_name="m",
        provider_version="1",
        base_commit="c",
        target_files=["tests/test_foo.py"],
        unified_diff="--- a/tests/test_foo.py\n+++ b/tests/test_foo.py\n@@ -1,1 +1,1 @@\n-assert False\n+assert True\n",
        rationale="r",
        expected_behavior="e",
    )
    files, _ = parse_unified_diff(prop.unified_diff)
    dec = evaluate_patch_policy(prop, files)
    assert dec.decision == PatchStatus.REJECTED
    assert any("test files" in r for r in dec.reasons)

    # Workflow file protection
    prop_wf = create_patch_proposal(
        finding_id="F2",
        provider_name="m",
        provider_version="1",
        base_commit="c",
        target_files=[".github/workflows/ci.yml"],
        unified_diff="--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1,1 +1,1 @@\n-a\n+b\n",
        rationale="r",
        expected_behavior="e",
    )
    files_wf, _ = parse_unified_diff(prop_wf.unified_diff)
    dec_wf = evaluate_patch_policy(prop_wf, files_wf)
    assert dec_wf.decision == PatchStatus.REJECTED
    assert any("workflow files" in r for r in dec_wf.reasons)

    # Lockfile protection
    prop_lock = create_patch_proposal(
        finding_id="F3",
        provider_name="m",
        provider_version="1",
        base_commit="c",
        target_files=["package-lock.json"],
        unified_diff="--- a/package-lock.json\n+++ b/package-lock.json\n@@ -1,1 +1,1 @@\n-a\n+b\n",
        rationale="r",
        expected_behavior="e",
    )
    files_lock, _ = parse_unified_diff(prop_lock.unified_diff)
    dec_lock = evaluate_patch_policy(prop_lock, files_lock)
    assert dec_lock.decision == PatchStatus.REJECTED
    assert any("lockfiles" in r for r in dec_lock.reasons)


def test_secret_introduction_rejection():
    secret_diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def run():\n"
        "+    API_KEY = 'AKIA1234567890ABCDEF'\n"
        "     return 42\n"
    )
    prop = create_patch_proposal(
        finding_id="F-SEC",
        provider_name="m",
        provider_version="1",
        base_commit="c",
        target_files=["src/app.py"],
        unified_diff=secret_diff,
        rationale="Add api key",
        expected_behavior="Works",
    )
    assert prop.redaction_audit.safe is False
    assert prop.redaction_audit.raw_value_matches >= 1

    files, _ = parse_unified_diff(secret_diff)
    dec = evaluate_patch_policy(prop, files)
    assert dec.decision == PatchStatus.REJECTED
    assert any("sensitive tokens" in r for r in dec.reasons)


def test_approval_token_scope_and_verification():
    diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n-def run():\n+def run_new():\n     return 42\n"
    prop = create_patch_proposal(
        finding_id="F10",
        provider_name="m",
        provider_version="1",
        base_commit="commit-aaa",
        target_files=["src/app.py"],
        unified_diff=diff,
        rationale="r",
        expected_behavior="e",
    )
    token = generate_approval_token(
        proposal_id=prop.proposal_id,
        base_commit=prop.base_commit,
        patch_hash=prop.patch_hash,
        allowed_paths=prop.target_files,
    )
    assert token.startswith("CAT-APP-")

    # Correct match
    ok, err = verify_approval_token(token, prop, base_commit="commit-aaa")
    assert ok is True
    assert err is None

    # Mismatched base commit
    ok, err = verify_approval_token(token, prop, base_commit="wrong-commit")
    assert ok is False
    assert err is not None and "does not match" in err

    # Tampered path scope
    prop.target_files = ["src/other.py"]
    ok, err = verify_approval_token(token, prop, base_commit="commit-aaa")
    assert ok is False


def test_state_machine_transitions():
    diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n-def run():\n+def run_new():\n     return 42\n"
    prop = create_patch_proposal(
        finding_id="F20",
        provider_name="m",
        provider_version="1",
        base_commit="c",
        target_files=["src/app.py"],
        unified_diff=diff,
        rationale="r",
        expected_behavior="e",
    )
    assert prop.status == PatchStatus.PROPOSED

    # Proposed -> Requires human approval
    prop.transition_to(PatchStatus.REQUIRES_HUMAN_APPROVAL)
    assert prop.status == PatchStatus.REQUIRES_HUMAN_APPROVAL

    # Requires human approval -> Approved
    prop.transition_to(PatchStatus.APPROVED)
    assert prop.status == PatchStatus.APPROVED

    # Approved -> Applied in isolated worktree
    prop.transition_to(PatchStatus.APPLIED_IN_ISOLATED_WORKTREE)
    assert prop.status == PatchStatus.APPLIED_IN_ISOLATED_WORKTREE

    # Applied -> Validated
    prop.transition_to(PatchStatus.VALIDATED)
    assert prop.status == PatchStatus.VALIDATED

    # Invalid jump from Validated to Approved raises ValueError
    with pytest.raises(ValueError):
        prop.transition_to(PatchStatus.APPROVED)


def test_isolated_patch_application_clean(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    commit = _init_test_git_repo(repo_dir)

    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def run():\n"
        "+    # Valid safe null check\n"
        "     return 42\n"
    )
    prop = create_patch_proposal(
        finding_id="F-CLEAN",
        provider_name="m",
        provider_version="1",
        base_commit=commit,
        target_files=["src/app.py"],
        unified_diff=diff,
        rationale="Safe comment",
        expected_behavior="No change in behavior",
    )
    token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)

    res = apply_patch_in_isolated_sandbox(
        prop,
        repository_root=repo_dir,
        approval_token=token,
    )
    assert res.valid is True
    assert res.applies_cleanly is True
    assert res.syntax_valid is True
    assert res.changed_files == ["src/app.py"]
    assert res.execution_allowed is False

    # Check original working tree was NOT modified
    app_orig = (repo_dir / "src" / "app.py").read_text(encoding="utf-8")
    assert "# Valid safe null check" not in app_orig


def test_isolated_patch_application_conflict(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    commit = _init_test_git_repo(repo_dir)

    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -999,2 +999,2 @@\n"
        "-nonexistent_code_line()\n"
        "+fixed_code_line()\n"
    )
    prop = create_patch_proposal(
        finding_id="F-CONFLICT",
        provider_name="m",
        provider_version="1",
        base_commit=commit,
        target_files=["src/app.py"],
        unified_diff=diff,
        rationale="Conflict test",
        expected_behavior="Should fail cleanly",
    )
    token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)

    res = apply_patch_in_isolated_sandbox(
        prop,
        repository_root=repo_dir,
        approval_token=token,
    )
    assert res.valid is False
    assert res.applies_cleanly is False
    assert any("Patch conflict" in err for err in res.errors)


def test_isolated_patch_syntax_breaking(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    commit = _init_test_git_repo(repo_dir)

    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def run():\n"
        "+    def broken(:\n"
        "     return 42\n"
    )
    prop = create_patch_proposal(
        finding_id="F-SYNTAX",
        provider_name="m",
        provider_version="1",
        base_commit=commit,
        target_files=["src/app.py"],
        unified_diff=diff,
        rationale="Broken syntax test",
        expected_behavior="Should be caught by syntax parser",
    )
    token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)

    res = apply_patch_in_isolated_sandbox(
        prop,
        repository_root=repo_dir,
        approval_token=token,
    )
    assert res.valid is False
    assert res.applies_cleanly is True
    assert res.syntax_valid is False
    assert any("Syntax error" in err for err in res.errors)


def test_mock_fixer_modes():
    modes = [
        "valid_patch",
        "malformed_patch",
        "path_traversal",
        "outside_snapshot_path",
        "conflict_patch",
        "syntax_breaking_patch",
        "test_modifying_patch",
        "workflow_modifying_patch",
        "dependency_modifying_patch",
        "secret_introducing_patch",
        "oversized_patch",
        "empty_patch",
    ]
    for mode in modes:
        fixer = MockFixer(mode=mode)
        proposal = fixer.propose(finding_id=f"FIND-{mode}", base_commit="commit123")
        assert proposal.provider_name == "mock-fixer"
        assert proposal.provenance.get("mode") == mode
        if mode == "empty_patch":
            assert proposal.unified_diff == ""
        elif mode == "secret_introducing_patch":
            assert proposal.redaction_audit.safe is False


def test_cli_patch_commands(tmp_path: Path):
    runner = CliRunner()
    repo_dir = tmp_path / "repo"
    commit = _init_test_git_repo(repo_dir)

    diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def run():\n"
        "+    # Safe comment\n"
        "     return 42\n"
    )
    prop = create_patch_proposal(
        finding_id="F-CLI",
        provider_name="m",
        provider_version="1",
        base_commit=commit,
        target_files=["src/app.py"],
        unified_diff=diff,
        rationale="CLI test",
        expected_behavior="Passes CLI checks",
    )
    prop_path = tmp_path / "proposal.json"
    prop_path.write_text(prop.model_dump_json(indent=2), encoding="utf-8")

    # 1. Inspect
    inspect_res = runner.invoke(app, ["patch", "inspect", "--proposal", str(prop_path)])
    assert inspect_res.exit_code == 0
    assert "Proposal ID:" in inspect_res.output
    assert "Finding ID:" in inspect_res.output

    # 2. Validate
    val_res = runner.invoke(app, ["patch", "validate", "--proposal", str(prop_path), "--repo", str(repo_dir), "--base", commit])
    assert val_res.exit_code == 0
    assert "Valid:            True" in val_res.output

    # 3. Apply without approval token -> fails
    apply_fail = runner.invoke(app, ["patch", "apply-isolated", "--proposal", str(prop_path), "--repo", str(repo_dir), "--base", commit, "--approval-token", "CAT-APP-invalid"])
    assert apply_fail.exit_code != 0
    assert "does not match expected token" in apply_fail.output

    # 4. Apply with valid approval token -> succeeds
    token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
    apply_ok = runner.invoke(app, ["patch", "apply-isolated", "--proposal", str(prop_path), "--repo", str(repo_dir), "--base", commit, "--approval-token", token])
    assert apply_ok.exit_code == 0
    assert "Applies Cleanly:    True" in apply_ok.output
    assert "Syntax Valid:       True" in apply_ok.output
    assert "Execution Allowed:  False" in apply_ok.output
