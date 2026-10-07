"""Phase 11C-D unit tests for the guarded worktree apply primitive.

The primitive is the only path that may write a validated patch into the
original workspace; these tests pin its mechanical defenses: clean-tree
requirement, exact capture/restore semantics, and the resulting-diff hash
check. Raw git output must never appear in reported errors.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeatlas.patching.apply import (
    MAX_WORKTREE_APPLY_FILE_BYTES,
    WorktreeApplyResult,
    _resulting_diff_stats,
    apply_patch_to_worktree,
    capture_worktree_file_state,
    restore_worktree_files,
)
from codeatlas.patching.proposal import compute_patch_hash, create_patch_proposal
from codeatlas.patching.sandbox import temporary_patch_sandbox


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _create_repo(root: Path) -> tuple[Path, str]:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("def run(x):\n    return x\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _proposal(repo: Path, base_commit: str, diff: str | None = None):
    diff = diff or (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def run(x):\n"
        "-    return x\n"
        "+    return x + 1\n"
    )
    return create_patch_proposal(
        finding_id="CA-TEST",
        provider_name="test",
        provider_version="1.0.0",
        base_commit=base_commit,
        target_files=["src/app.py"],
        unified_diff=diff,
        rationale="test",
        expected_behavior="test",
    )


def _original_bytes(repo: Path) -> bytes:
    return (repo / "src" / "app.py").read_bytes()


def _patched_bytes(repo: Path) -> bytes:
    return b"def run(x):\r\n    return x + 1\r\n"


def _validated_diff_hash(repo: Path, head: str, diff: str) -> str:
    """Compute the sandbox resulting-diff hash exactly as the validator does."""
    with temporary_patch_sandbox(repo, head) as sandbox:
        check = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"], cwd=sandbox.path,
            input=diff, check=True, capture_output=True, text=True,
        )
        assert check.returncode == 0
        _numstat, diff_hash = _resulting_diff_stats(sandbox.path)
    return diff_hash


def test_apply_patch_to_worktree_success_and_hash(tmp_path: Path):
    repo, head = _create_repo(tmp_path / "repo")
    proposal = _proposal(repo, head)
    original = (repo / "src" / "app.py").read_bytes()
    expected = _validated_diff_hash(repo, head, proposal.unified_diff)
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash=expected)
    assert result.success is True
    assert result.resulting_diff_hash == expected
    assert result.changed_files == ["src/app.py"]
    assert (repo / "src" / "app.py").read_bytes() == _patched_bytes(repo)
    assert result.original_contents == {"src/app.py": original}
    assert result.post_apply_file_hashes["src/app.py"]
    assert result.head_commit == head
    assert result.errors == []


def test_apply_diff_hash_mismatch_restores_original(tmp_path: Path):
    repo, head = _create_repo(tmp_path / "repo")
    proposal = _proposal(repo, head)
    # A wrong expected resulting-diff hash fails closed and restores the
    # original bytes; nothing partial is ever left behind.
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash="0" * 64)
    assert result.success is False
    assert result.failure_kind == "diff_mismatch"
    assert (repo / "src" / "app.py").read_bytes() == _original_bytes(repo)
    assert _git(repo, "status", "--porcelain") == ""


def test_apply_refuses_dirty_workspace(tmp_path: Path):
    repo, head = _create_repo(tmp_path / "repo")
    (repo / "other.txt").write_text("user work\n", encoding="utf-8")
    proposal = _proposal(repo, head)
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash="0" * 64)
    assert result.success is False
    assert result.failure_kind == "workspace_dirty"
    assert (repo / "src" / "app.py").read_bytes() == _original_bytes(repo)


def test_apply_scope_exceeded_and_parse_failure(tmp_path: Path):
    repo, head = _create_repo(tmp_path / "repo")
    other = _proposal(
        repo,
        head,
        diff=(
            "--- a/src/other.py\n"
            "+++ b/src/other.py\n"
            "@@ -0,0 +1 @@\n"
            "+new\n"
        ),
    )
    result = apply_patch_to_worktree(other, repo, expected_resulting_diff_hash="0" * 64)
    assert result.success is False
    assert result.failure_kind == "scope_exceeded"
    assert not (repo / "src" / "other.py").exists()

    malformed = _proposal(
        repo,
        head,
        diff="not a unified diff at all",
    )
    result = apply_patch_to_worktree(malformed, repo, expected_resulting_diff_hash="0" * 64)
    assert result.success is False
    assert result.failure_kind == "parse_failed"


def test_restore_worktree_files_round_trip(tmp_path: Path):
    repo, _head = _create_repo(tmp_path / "repo")
    state = capture_worktree_file_state(repo, ["src/app.py", "src/created.py"])
    assert state == {"src/app.py": _original_bytes(repo), "src/created.py": None}
    (repo / "src" / "app.py").write_text("changed\n", encoding="utf-8")
    (repo / "src" / "created.py").write_text("created\n", encoding="utf-8")
    assert restore_worktree_files(repo, state) is True
    assert (repo / "src" / "app.py").read_bytes() == _original_bytes(repo)
    assert not (repo / "src" / "created.py").exists()


def test_apply_bounds_are_reasonable():
    assert MAX_WORKTREE_APPLY_FILE_BYTES >= 64 * 1024
    assert WorktreeApplyResult().success is False
    assert len(compute_patch_hash("")) == 64
