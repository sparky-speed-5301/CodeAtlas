"""Unit tests for Phase 7C: human-approved isolated sandbox validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeatlas.evidence import EvidenceLogger
from codeatlas.patching.apply import apply_patch_in_isolated_sandbox
from codeatlas.patching.models import PatchStatus
from codeatlas.patching.proposal import (
    compute_patch_hash,
    create_patch_proposal,
    generate_approval_token,
)
from codeatlas.patching.sandbox import temporary_patch_sandbox
from codeatlas.patching.validator import validate_patch_proposal

APP_CONTENT = "def run():\n    return 42\n"

DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def run():\n"
    "+    # guarded return\n"
    "     return 42\n"
)


def _init_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True)
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "app.py").write_text(APP_CONTENT, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t.invalid")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "init")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()


def _proposal(commit: str) -> object:
    return create_patch_proposal(
        finding_id="F-7C",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=commit,
        target_files=["src/app.py"],
        unified_diff=DIFF,
        rationale="Test",
        expected_behavior="Applies cleanly",
    )


# ---------------------------------------------------------------------------
# Approval checks
# ---------------------------------------------------------------------------

class TestApprovalChecks:
    def test_valid_token_allows_isolated_validation(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = validate_patch_proposal(prop, tmp_path / "repo", approval_token=token, allow_isolated_apply=True)
        assert res.valid is True
        assert res.approval_verified is True
        assert res.applies_cleanly is True
        assert res.syntax_valid is True
        assert res.cleanup_status == "completed"
        assert prop.status == PatchStatus.VALIDATED

    def test_missing_token_fails_closed(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=None)
        assert res.valid is False
        assert res.approval_verified is False
        assert res.sandbox_id is None
        assert "requires explicit human approval token" in res.errors[0]
        # Nothing was applied anywhere.
        assert APP_CONTENT in (tmp_path / "repo" / "src" / "app.py").read_text(encoding="utf-8")

    def test_malformed_token_rejected(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token="CAT-APP-garbage")
        assert res.valid is False and res.approval_verified is False

    def test_wrong_proposal_id_token_rejected(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token("prop-other", commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is False and res.approval_verified is False

    def test_wrong_base_commit_token_rejected(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, "1" * 40, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is False and res.approval_verified is False

    def test_wrong_patch_hash_token_rejected(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, "0" * 64, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is False and res.approval_verified is False

    def test_wrong_target_files_token_rejected(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, ["src/other.py"])
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is False and res.approval_verified is False

    def test_wrong_run_scope_token_rejected(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(
            prop.proposal_id, commit, prop.patch_hash, prop.target_files, run_id="other-run"
        )
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token, run_id="default")
        assert res.valid is False and res.approval_verified is False

    def test_run_scoped_token_accepted_with_matching_scope(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(
            prop.proposal_id, commit, prop.patch_hash, prop.target_files, run_id="run-7c"
        )
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token, run_id="run-7c")
        assert res.valid is True and res.approval_verified is True

    def test_token_never_appears_in_evidence(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        evidence_path = tmp_path / "evidence.jsonl"
        with EvidenceLogger(evidence_path) as evidence:
            apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token, evidence=evidence)
        assert token not in evidence_path.read_text(encoding="utf-8")

    def test_token_prefix_never_appears_in_rejection_errors(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token("prop-other", commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert token[:16] not in " ".join(res.errors)


# ---------------------------------------------------------------------------
# Lifecycle state guards and transitions
# ---------------------------------------------------------------------------

class TestStateGuards:
    def test_rejected_proposal_blocked(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        prop.status = PatchStatus.REJECTED
        res = validate_patch_proposal(prop, tmp_path / "repo", approval_token="x", allow_isolated_apply=True)
        assert res.valid is False
        assert prop.status == PatchStatus.REJECTED
        assert res.cleanup_status == "not_applicable"

    def test_already_validated_proposal_blocked(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        prop.status = PatchStatus.VALIDATED
        res = validate_patch_proposal(prop, tmp_path / "repo", approval_token="x", allow_isolated_apply=True)
        assert res.valid is False
        assert prop.status == PatchStatus.VALIDATED

    def test_already_applied_proposal_blocked(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        prop.status = PatchStatus.APPLIED_IN_ISOLATED_WORKTREE
        res = validate_patch_proposal(prop, tmp_path / "repo", approval_token="x", allow_isolated_apply=True)
        assert res.valid is False

    def test_revalidation_requires_explicit_policy(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        prop.status = PatchStatus.VALIDATED
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = validate_patch_proposal(
            prop, tmp_path / "repo",
            approval_token=token,
            allow_isolated_apply=True,
            config={"patch": {"allow_revalidation": True}},
        )
        assert res.valid is True

    def test_invalid_transition_rules(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        with pytest.raises(ValueError):
            prop.transition_to(PatchStatus.VALIDATED)  # proposed -> validated
        with pytest.raises(ValueError):
            prop.transition_to(PatchStatus.APPLIED_IN_ISOLATED_WORKTREE)  # proposed -> applied
        prop.transition_to(PatchStatus.REQUIRES_HUMAN_APPROVAL)
        prop.transition_to(PatchStatus.REJECTED)
        with pytest.raises(ValueError):
            prop.transition_to(PatchStatus.APPROVED)  # rejected -> approved

    def test_apply_flow_transitions_proposal_to_validated(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert prop.status == PatchStatus.VALIDATED

    def test_apply_flow_transitions_broken_syntax_to_failed(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        broken_diff = DIFF.replace("+    # guarded return", "+    def broken(:")
        prop = create_patch_proposal(
            finding_id="F-BROKEN", provider_name="op", provider_version="1",
            base_commit=commit, target_files=["src/app.py"],
            unified_diff=broken_diff, rationale="r", expected_behavior="e",
        )
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.applies_cleanly is True
        assert res.syntax_valid is False
        assert prop.status == PatchStatus.FAILED_VALIDATION

    def test_failed_validation_cannot_revalidate(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        broken_diff = DIFF.replace("+    # guarded return", "+    def broken(:")
        prop = create_patch_proposal(
            finding_id="F-BROKEN", provider_name="op", provider_version="1",
            base_commit=commit, target_files=["src/app.py"],
            unified_diff=broken_diff, rationale="r", expected_behavior="e",
        )
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert prop.status == PatchStatus.FAILED_VALIDATION
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is False


# ---------------------------------------------------------------------------
# Sandbox behavior
# ---------------------------------------------------------------------------

class TestSandboxBehavior:
    def test_resulting_diff_hash_and_changed_files(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.changed_files == ["src/app.py"]
        assert res.resulting_diff_hash
        assert res.sandbox_id
        assert res.base_commit == commit
        assert res.patch_hash == prop.patch_hash
        assert res.tests_status == "not_run"
        assert res.build_status == "not_run"
        assert res.execution_allowed is False

    def test_changed_file_mismatch_rejected(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        # Declared target does not match the diff's file.
        prop = create_patch_proposal(
            finding_id="F-MISMATCH", provider_name="op", provider_version="1",
            base_commit=commit, target_files=["src/other.py"],
            unified_diff=DIFF, rationale="r", expected_behavior="e",
        )
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is False
        assert any("beyond the declared targets" in e for e in res.errors)

    def test_sandbox_always_cleaned_up_on_success(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is True
        assert res.cleanup_status == "completed"
        assert res.sandbox_retained is False
        worktrees = subprocess.run(
            ["git", "worktree", "list"], cwd=tmp_path / "repo", capture_output=True, text=True
        ).stdout
        assert str(tmp_path / "repo").replace("\\", "/") in worktrees.replace("\\", "/")
        assert res.sandbox_id not in worktrees

    def test_sandbox_cleanup_after_validation_failure(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        conflict = (
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -999,2 +999,1 @@\n"
            "-nope_one()\n-nope_two()\n+replaced()\n"
        )
        prop = create_patch_proposal(
            finding_id="F-CONF", provider_name="op", provider_version="1",
            base_commit=commit, target_files=["src/app.py"],
            unified_diff=conflict, rationale="r", expected_behavior="e",
        )
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is False and res.applies_cleanly is False
        assert res.cleanup_status == "completed"  # cleaned up even after failure
        worktrees = subprocess.run(
            ["git", "worktree", "list"], cwd=tmp_path / "repo", capture_output=True, text=True
        ).stdout
        assert res.sandbox_id not in worktrees

    def test_retain_sandbox_on_failure(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        broken_diff = DIFF.replace("+    # guarded return", "+    def broken(:")
        prop = create_patch_proposal(
            finding_id="F-RET", provider_name="op", provider_version="1",
            base_commit=commit, target_files=["src/app.py"],
            unified_diff=broken_diff, rationale="r", expected_behavior="e",
        )
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(
            prop, tmp_path / "repo", approval_token=token, retain_sandbox_on_failure=True
        )
        assert res.valid is False
        assert res.cleanup_status == "retained"
        assert res.sandbox_retained is True

    def test_retain_requires_opt_in(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        ctx = temporary_patch_sandbox(tmp_path / "repo", commit)
        with pytest.raises(RuntimeError):
            ctx.retain()

    def test_cleanup_failure_reported(self, tmp_path: Path, monkeypatch):
        import codeatlas.patching.sandbox as sandbox_module

        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)

        real_run_git = sandbox_module.run_git

        def failing_remove(args, **kwargs):
            if "remove" in args:
                class R:
                    returncode = 1
                    stderr = "simulated removal failure"
                    stdout = ""
                return R()
            return real_run_git(args, **kwargs)

        monkeypatch.setattr(sandbox_module, "run_git", failing_remove)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is False
        assert res.cleanup_status == "failed"
        assert any("cleanup failed" in e.lower() for e in res.errors)

    def test_original_worktree_untouched_throughout(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        res = apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token)
        assert res.valid is True
        assert "# guarded return" not in (tmp_path / "repo" / "src" / "app.py").read_text(encoding="utf-8")

    def test_evidence_lifecycle_events(self, tmp_path: Path):
        commit = _init_repo(tmp_path / "repo")
        prop = _proposal(commit)
        token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
        evidence_path = tmp_path / "evidence.jsonl"
        with EvidenceLogger(evidence_path) as evidence:
            apply_patch_in_isolated_sandbox(prop, tmp_path / "repo", approval_token=token, evidence=evidence)
        import json as _json
        events = [_json.loads(line)["event"] for line in evidence_path.read_text(encoding="utf-8").splitlines() if line]
        for expected_event in (
            "patch_validation_requested",
            "approval_verification_started",
            "approval_verified",
            "sandbox_created",
            "patch_check_started",
            "patch_check_completed",
            "patch_applied_isolated",
            "syntax_validation_completed",
            "patch_validation_completed",
            "sandbox_cleanup_completed",
        ):
            assert expected_event in events

    def test_patch_hash_stability(self):
        assert compute_patch_hash(DIFF) == compute_patch_hash(DIFF.replace("\n", "\r\n"))
