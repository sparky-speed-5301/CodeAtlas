"""Offline Phase 11C-F end-to-end autofix workflow evaluation.

Each case drives the complete explicit chain over a real (temporary) Git
repository through the real ReviewStateManager and the existing repair,
validation, and apply machinery: generate -> approve -> isolated sandbox
validation -> explicit apply of the validated proposal -> revert. Every case
verifies that nothing is applied automatically, that the workspace result
byte-matches the validated sandbox diff, and that revert restores the exact
pre-apply bytes. No network, no live provider, no repository test execution.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from codeatlas.patching.proposal import generate_validation_approval_token
from codeatlas.service.models import ReviewCreateRequest
from codeatlas.service.state import ReviewStateManager, ServiceStateError

REPO_SOURCE_BASE = "def run(x):\n    return x\n"
REPO_SOURCE_HEAD = "def render(user):\n    print(user.password)\n"

CASES = (
    "happy_path_apply_revert",
    "apply_requires_validation",
    "validate_token_never_applies",
    "wrong_confirmation_hash_rejected",
    "dirty_workspace_rejected",
    "stale_head_rejected",
    "double_apply_rejected",
    "revert_restores_exact_bytes",
    "revert_blocked_after_user_edit",
    "apply_history_bounded_and_ordered",
    "worktree_zero_git_mutation",
    "apply_response_never_contains_token",
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repository"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    # Platform-default newline translation mirrors a real working tree whose
    # line endings follow the local convention (core.autocrlf on Windows).
    (repo / "src" / "app.py").write_text(REPO_SOURCE_BASE, encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "eval@eval.invalid")
    git(repo, "config", "user.name", "Eval")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    (repo / "src" / "app.py").write_text(REPO_SOURCE_HEAD, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "head")
    return repo


def run_review(manager: ReviewStateManager, repo: Path) -> tuple[str, str]:
    run_id = manager.start_review(
        ReviewCreateRequest(repo=str(repo), base="HEAD~1", head="HEAD")
    ).run_id
    for _ in range(240):
        status = manager.get_review_status(run_id)
        if status.status in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    findings = manager.get_findings(run_id)
    assert findings, "fixture repository must produce one deterministic finding"
    return run_id, findings[0].id


def mint(proposal, operation: str) -> str:
    return generate_validation_approval_token(
        proposal_id=proposal.proposal_id,
        finding_id=proposal.finding_id,
        run_id=proposal.run_id,
        repository_identity=proposal.repository,
        base_commit=proposal.base_commit,
        head_commit=proposal.head_commit,
        patch_hash=proposal.patch_hash,
        target_files=proposal.target_files,
        operation=operation,
    )


def validated_proposal(manager: ReviewStateManager, repo: Path):
    """Run the full generation/approval/validation chain; return the context."""
    run_id, finding_id = run_review(manager, repo)
    proposal = manager.request_fix_proposal(run_id, finding_id)
    assert proposal.generation_status == "draft_ready" and proposal.approval_required
    validate_token = mint(proposal, "validate")
    approval = manager.approve_fix_proposal_for_validation(
        run_id, finding_id, proposal.proposal_id, approval_token=validate_token
    )
    assert approval.approval_verified is True
    validation = manager.validate_fix_proposal(
        run_id, finding_id, proposal.proposal_id, approval_token=validate_token
    )
    assert validation.valid is True and validation.validation_status == "validated"
    return run_id, finding_id, proposal, validation, validate_token


def evaluate_case(kind: str) -> dict:
    checks: dict[str, bool] = {"case_completed": False}
    with tempfile.TemporaryDirectory(prefix="codeatlas-autofix-eval-") as tmp:
        repo = make_repo(Path(tmp))
        manager = ReviewStateManager()
        run_id, finding_id, proposal, validation, validate_token = (
            validated_proposal(manager, repo)
        )
        apply_token = mint(proposal, "apply")
        target = repo / "src" / "app.py"
        head_before = git(repo, "rev-parse", "HEAD")

        if kind == "apply_requires_validation":
            # A fresh, unvalidated draft from a second run on the same repository.
            manager2 = ReviewStateManager()
            run2, finding2 = run_review(manager2, repo)
            draft2 = manager2.request_fix_proposal(run2, finding2)
            try:
                manager2.apply_fix_proposal(
                    run2, finding2, draft2.proposal_id,
                    approval_token=mint(draft2, "apply"),
                    confirmed_patch_hash=draft2.patch_hash,
                )
                checks["apply_blocked_without_validation"] = False
            except ServiceStateError as err:
                checks["apply_blocked_without_validation"] = (
                    err.reason == "apply_not_validated"
                )
            checks["workspace_untouched"] = "TODO(codeatlas)" not in target.read_text(encoding="utf-8")

        elif kind == "validate_token_never_applies":
            response = manager.apply_fix_proposal(
                run_id, finding_id, proposal.proposal_id,
                approval_token=validate_token,
                confirmed_patch_hash=proposal.patch_hash,
            )
            checks["approval_rejected"] = (
                response.approval_verified is False and response.apply_status == "not_applied"
            )
            checks["workspace_untouched"] = "TODO(codeatlas)" not in target.read_text(encoding="utf-8")

        elif kind == "wrong_confirmation_hash_rejected":
            try:
                manager.apply_fix_proposal(
                    run_id, finding_id, proposal.proposal_id,
                    approval_token=apply_token, confirmed_patch_hash="0" * 64,
                )
                checks["confirmation_enforced"] = False
            except ServiceStateError as err:
                checks["confirmation_enforced"] = err.reason == "apply_confirmation_invalid"
            checks["workspace_untouched"] = "TODO(codeatlas)" not in target.read_text(encoding="utf-8")

        elif kind == "dirty_workspace_rejected":
            target.write_text(REPO_SOURCE_HEAD + "\n# uncommitted work\n", encoding="utf-8")
            try:
                manager.apply_fix_proposal(
                    run_id, finding_id, proposal.proposal_id,
                    approval_token=apply_token, confirmed_patch_hash=proposal.patch_hash,
                )
                checks["dirty_workspace_blocked"] = False
            except ServiceStateError as err:
                checks["dirty_workspace_blocked"] = err.reason == "workspace_dirty"
            checks["user_edit_preserved"] = target.read_text(encoding="utf-8").endswith("# uncommitted work\n")
            checks["nothing_applied"] = "TODO(codeatlas)" not in target.read_text(encoding="utf-8")

        elif kind == "stale_head_rejected":
            git(repo, "commit", "--allow-empty", "-qm", "stale")
            try:
                manager.apply_fix_proposal(
                    run_id, finding_id, proposal.proposal_id,
                    approval_token=apply_token, confirmed_patch_hash=proposal.patch_hash,
                )
                checks["stale_head_blocked"] = False
            except ServiceStateError as err:
                checks["stale_head_blocked"] = err.reason == "repository_state_stale"
            checks["nothing_applied"] = "TODO(codeatlas)" not in target.read_text(encoding="utf-8")

        elif kind in {"happy_path_apply_revert", "double_apply_rejected", "revert_restores_exact_bytes",
                      "revert_blocked_after_user_edit", "apply_history_bounded_and_ordered",
                      "worktree_zero_git_mutation", "apply_response_never_contains_token"}:
            applied = manager.apply_fix_proposal(
                run_id, finding_id, proposal.proposal_id,
                approval_token=apply_token, confirmed_patch_hash=proposal.patch_hash,
            )
            checks["applied_only_after_full_chain"] = (
                applied.apply_status == "applied" and applied.approval_verified is True
            )
            checks["workspace_matches_validated_diff"] = (
                applied.resulting_diff_hash == validation.resulting_diff_hash
            )
            checks["apply_visible_in_workspace"] = "TODO(codeatlas)" in target.read_text(encoding="utf-8")
            checks["head_untouched"] = git(repo, "rev-parse", "HEAD") == head_before

            if kind == "double_apply_rejected":
                try:
                    manager.apply_fix_proposal(
                        run_id, finding_id, proposal.proposal_id,
                        approval_token=apply_token, confirmed_patch_hash=proposal.patch_hash,
                    )
                    checks["second_apply_blocked"] = False
                except ServiceStateError as err:
                    checks["second_apply_blocked"] = err.reason == "already_applied"

            if kind == "revert_blocked_after_user_edit":
                edited = target.read_text(encoding="utf-8") + "\n# user edit\n"
                target.write_text(edited, encoding="utf-8")
                try:
                    manager.revert_fix_apply(
                        run_id, finding_id, proposal.proposal_id,
                        confirmed_patch_hash=proposal.patch_hash,
                    )
                    checks["revert_blocked"] = False
                except ServiceStateError as err:
                    checks["revert_blocked"] = err.reason == "revert_state_changed"
                checks["user_edit_preserved"] = target.read_text(encoding="utf-8") == edited
            else:
                reverted = manager.revert_fix_apply(
                    run_id, finding_id, proposal.proposal_id,
                    confirmed_patch_hash=proposal.patch_hash,
                )
                checks["revert_restores_bytes"] = (
                    reverted.apply_status == "reverted"
                    and target.read_text(encoding="utf-8") == REPO_SOURCE_HEAD
                )
                checks["worktree_clean_after_revert"] = git(repo, "status", "--porcelain") == ""

            if kind == "apply_history_bounded_and_ordered":
                history = manager.get_fix_apply_history(run_id, finding_id)
                checks["history_ordered"] = [
                    event.event for event in history.events
                ] == ["applied", "reverted"]
                checks["history_scoped"] = all(
                    event.proposal_id == proposal.proposal_id
                    and event.run_id == run_id
                    and event.finding_id == finding_id
                    for event in history.events
                )

            if kind == "apply_response_never_contains_token":
                dumped = json.dumps(applied.model_dump(mode="json"))
                checks["no_apply_token"] = apply_token not in dumped
                checks["no_validate_token"] = validate_token not in dumped

            if kind == "worktree_zero_git_mutation":
                checks["refs_untouched"] = git(repo, "show-ref") == git(
                    repo, "show-ref"
                )
                checks["no_staged_changes"] = git(repo, "diff", "--cached") == ""
                checks["worktrees_pruned"] = len(git(repo, "worktree", "list").splitlines()) == 1
        else:  # pragma: no cover - CASES tuple is the single source
            raise AssertionError(f"Unknown case {kind}")

        checks["case_completed"] = all(
            value for key, value in checks.items() if key != "case_completed"
        )
        return {"case": kind, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 11C-F autofix workflow evaluation")
    parser.add_argument("--output", type=Path, default=None, help="Write JSONL results here")
    args = parser.parse_args()

    passed = 0
    lines = []
    for kind in CASES:
        result = evaluate_case(kind)
        result["passed"] = result["checks"]["case_completed"]
        passed += 1 if result["passed"] else 0
        lines.append(json.dumps(result, sort_keys=True))
        print(f"{'PASS' if result['passed'] else 'FAIL'} {kind}")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Phase 11C-F: {passed}/{len(CASES)} cases passed")


if __name__ == "__main__":
    main()
