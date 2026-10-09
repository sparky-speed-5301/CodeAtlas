"""Phase 11C-D unit tests for the guarded worktree apply primitive.

The primitive is the only path that may write a validated patch into the
original workspace; these tests pin its mechanical defenses: clean-tree
requirement, exact capture/restore semantics, and the resulting-diff hash
check. Raw git output must never appear in reported errors.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from codeatlas.patching.apply import (
    MAX_WORKTREE_APPLY_FILE_BYTES,
    WorktreeApplyResult,
    _resulting_diff_stats,
    apply_patch_to_worktree,
    capture_worktree_file_state,
    restore_worktree_files,
)
from codeatlas.patching.proposal import compute_patch_hash, create_patch_proposal, generate_approval_token
from codeatlas.patching.validator import validate_patch_proposal
from codeatlas.patching.sandbox import temporary_patch_sandbox
from codeatlas.git.executable import run_git


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _create_repo(
    root: Path,
    *,
    source: bytes = b"def run(x):\n    return x\n",
    autocrlf: str | None = None,
) -> tuple[Path, str]:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_bytes(source)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")
    if autocrlf is not None:
        _git(root, "config", "core.autocrlf", autocrlf)
    _git(root, "config", "core.safecrlf", "false")
    _git(root, "config", "core.eol", "native")
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


def _normalise_text(content: bytes) -> str:
    """Compare source semantics without hiding raw-byte assertions."""
    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("utf-8")


def _line_endings(content: bytes) -> tuple[int, int, int]:
    """Count CRLF, LF-only, and CR-only endings without normalising bytes."""
    crlf = content.count(b"\r\n")
    without_crlf = content.replace(b"\r\n", b"")
    return crlf, without_crlf.count(b"\n"), without_crlf.count(b"\r")


def _assert_logical_patch(content: bytes) -> None:
    assert _normalise_text(content) == "def run(x):\n    return x + 1\n"


def _validated_diff_hash(repo: Path, head: str, diff: str) -> str:
    """Compute the sandbox resulting-diff hash exactly as the validator does."""
    with temporary_patch_sandbox(repo, head) as sandbox:
        check = run_git(
            ["apply", "--ignore-whitespace", "--whitespace=nowarn", "-"],
            cwd=sandbox.path, input=diff,
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
    _assert_logical_patch((repo / "src" / "app.py").read_bytes())
    assert result.original_contents == {"src/app.py": original}
    assert result.post_apply_file_hashes["src/app.py"]
    assert result.head_commit == head
    assert result.errors == []


@pytest.mark.parametrize(
    ("label", "source", "autocrlf", "expected_endings"),
    [
        ("linux-lf", b"def run(x):\n    return x\n", "false", (0, 2, 0)),
        # This is the Windows checkout behavior exercised on every host. On a
        # real Windows runner the same case additionally covers native paths.
        ("windows-crlf", b"def run(x):\r\n    return x\r\n", "true", (2, 0, 0)),
        ("crlf-without-autocrlf", b"def run(x):\r\n    return x\r\n", "false", (1, 1, 0)),
        ("mixed-endings", b"def run(x):\r\n    return x\n", "false", (1, 1, 0)),
    ],
)
def test_apply_preserves_platform_semantics_and_exact_validated_hash(
    tmp_path: Path,
    label: str,
    source: bytes,
    autocrlf: str,
    expected_endings: tuple[int, int, int],
):
    """Line endings are explicit while the validated diff remains byte-bound."""
    repo, head = _create_repo(tmp_path / label, source=source, autocrlf=autocrlf)
    proposal = _proposal(repo, head)
    expected_hash = _validated_diff_hash(repo, head, proposal.unified_diff)
    before = source

    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash=expected_hash)

    assert result.success is True
    assert result.resulting_diff_hash == expected_hash
    applied = (repo / "src" / "app.py").read_bytes()
    _assert_logical_patch(applied)
    assert _line_endings(applied) == expected_endings
    assert result.original_contents == {"src/app.py": before}
    assert result.post_apply_file_hashes["src/app.py"] == hashlib.sha256(applied).hexdigest()


def test_core_autocrlf_configuration_is_visible_and_does_not_change_hash_contract(tmp_path: Path):
    for setting in ("true", "false"):
        repo, head = _create_repo(
            tmp_path / f"autocrlf-{setting}",
            source=b"def run(x):\r\n    return x\r\n",
            autocrlf=setting,
        )
        assert _git(repo, "config", "--get", "core.autocrlf") == setting
        proposal = _proposal(repo, head)
        expected_hash = _validated_diff_hash(repo, head, proposal.unified_diff)
        result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash=expected_hash)
        assert result.success and result.resulting_diff_hash == expected_hash
        _assert_logical_patch((repo / "src/app.py").read_bytes())


def test_mixed_line_endings_are_restored_byte_exactly_after_hash_failure(tmp_path: Path):
    source = b"def run(x):\r\n    return x\n"
    repo, head = _create_repo(tmp_path / "mixed-failure", source=source, autocrlf="false")
    proposal = _proposal(repo, head)
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash="0" * 64)
    assert not result.success and result.failure_kind == "diff_mismatch"
    assert (repo / "src/app.py").read_bytes() == source
    assert _git(repo, "status", "--porcelain") == ""


def test_apply_success_then_restore_is_byte_safe_and_git_metadata_invariant(tmp_path: Path):
    repo, head = _create_repo(tmp_path / "restore", source=b"def run(x):\r\n    return x\r\n", autocrlf="false")
    proposal = _proposal(repo, head)
    expected_hash = _validated_diff_hash(repo, head, proposal.unified_diff)
    before = {
        "file": (repo / "src/app.py").read_bytes(),
        "head": _git(repo, "rev-parse", "HEAD"),
        "refs": _git(repo, "show-ref"),
        "index": (repo / ".git/index").read_bytes(),
    }
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash=expected_hash)
    assert result.success
    assert restore_worktree_files(repo, result.original_contents)
    assert (repo / "src/app.py").read_bytes() == before["file"]
    assert _git(repo, "rev-parse", "HEAD") == before["head"]
    assert _git(repo, "show-ref") == before["refs"]
    assert (repo / ".git/index").read_bytes() == before["index"]
    assert _git(repo, "status", "--porcelain") == ""


def test_apply_check_failure_leaves_original_bytes_and_metadata_untouched(tmp_path: Path):
    repo, head = _create_repo(tmp_path / "check-failure")
    proposal = _proposal(repo, head, diff=(
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n"
        " def does_not_exist():\n-    return x\n+    return x + 1\n"
    ))
    before = (repo / "src/app.py").read_bytes()
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash="0" * 64)
    assert not result.success and result.failure_kind == "check_failed"
    assert (repo / "src/app.py").read_bytes() == before
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("autocrlf", ["false", "true"])
@pytest.mark.parametrize("source", [
    b"def run(x):\n    return x\n",
    b"def run(x):\r\n    return x\r\n",
    b"def run(x):\r\n    return x\n",
], ids=["lf", "crlf", "mixed"])
def test_real_validation_apply_and_byte_exact_restore(tmp_path: Path, autocrlf: str, source: bytes):
    """Exercise the real approval-gated validator, not just a test-applied diff."""
    repo, head = _create_repo(tmp_path / "validation", source=source, autocrlf=autocrlf)
    proposal = _proposal(repo, head)
    token = generate_approval_token(proposal.proposal_id, head, proposal.patch_hash, proposal.target_files)
    before = (repo / "src/app.py").read_bytes()
    index = (repo / ".git/index").read_bytes()
    refs = _git(repo, "show-ref")
    validation = validate_patch_proposal(
        proposal, repository_root=repo, approval_token=token, allow_isolated_apply=True,
    )
    assert validation.valid and validation.approval_verified and validation.syntax_valid
    assert validation.cleanup_status == "completed"
    assert validation.resulting_diff_hash
    assert (repo / "src/app.py").read_bytes() == before
    assert (repo / ".git/index").read_bytes() == index
    assert _git(repo, "show-ref") == refs
    assert _git(repo, "status", "--porcelain") == ""

    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash=validation.resulting_diff_hash)
    assert result.success and result.resulting_diff_hash == validation.resulting_diff_hash
    # Hash raw git diff bytes independently, with no text-mode newline conversion.
    diff_bytes = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, check=True).stdout
    assert hashlib.sha256(diff_bytes).hexdigest() == validation.resulting_diff_hash
    _assert_logical_patch((repo / "src/app.py").read_bytes())
    assert restore_worktree_files(repo, result.original_contents)
    assert (repo / "src/app.py").read_bytes() == before
    assert (repo / ".git/index").read_bytes() == index
    assert _git(repo, "show-ref") == refs
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("autocrlf", ["false", "true"])
def test_validation_syntax_failure_preserves_original_bytes(tmp_path: Path, autocrlf: str):
    source = b"def run(x):\r\n    return x\n"
    repo, head = _create_repo(tmp_path / "syntax-failure", source=source, autocrlf=autocrlf)
    proposal = _proposal(repo, head, diff=(
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n"
        " def run(x):\n-    return x\n+    return (\n"
    ))
    token = generate_approval_token(proposal.proposal_id, head, proposal.patch_hash, proposal.target_files)
    index = (repo / ".git/index").read_bytes()
    validation = validate_patch_proposal(
        proposal, repository_root=repo, approval_token=token, allow_isolated_apply=True,
    )
    assert not validation.valid and validation.syntax_valid is False
    assert validation.cleanup_status == "completed"
    assert (repo / "src/app.py").read_bytes() == source
    assert (repo / ".git/index").read_bytes() == index
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""


def test_partial_apply_failure_restores_original_mixed_bytes(tmp_path: Path, monkeypatch):
    source = b"def run(x):\r\n    return x\n"
    repo, head = _create_repo(tmp_path / "partial-failure", source=source, autocrlf="false")
    proposal = _proposal(repo, head)
    index = (repo / ".git/index").read_bytes()

    def fail_apply(args, **kwargs):
        if args[0] == "apply" and "--check" not in args:
            (repo / "src/app.py").write_bytes(b"partial write\n")
            return subprocess.CompletedProcess(args, 1, "", "private error")
        return run_git(args, **kwargs)

    monkeypatch.setattr("codeatlas.patching.apply.run_git", fail_apply)
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash="0" * 64)
    assert not result.success and result.failure_kind == "apply_failed"
    assert "private error" not in str(result.errors)
    assert (repo / "src/app.py").read_bytes() == source
    assert (repo / ".git/index").read_bytes() == index
    assert _git(repo, "status", "--porcelain") == ""


def test_apply_diff_hash_mismatch_restores_original(tmp_path: Path):
    repo, head = _create_repo(tmp_path / "repo")
    original = _original_bytes(repo)
    proposal = _proposal(repo, head)
    # A wrong expected resulting-diff hash fails closed and restores the
    # original bytes; nothing partial is ever left behind.
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash="0" * 64)
    assert result.success is False
    assert result.failure_kind == "diff_mismatch"
    assert (repo / "src" / "app.py").read_bytes() == original
    assert _git(repo, "status", "--porcelain") == ""


def test_apply_refuses_dirty_workspace(tmp_path: Path):
    repo, head = _create_repo(tmp_path / "repo")
    original = _original_bytes(repo)
    (repo / "other.txt").write_text("user work\n", encoding="utf-8")
    proposal = _proposal(repo, head)
    result = apply_patch_to_worktree(proposal, repo, expected_resulting_diff_hash="0" * 64)
    assert result.success is False
    assert result.failure_kind == "workspace_dirty"
    assert (repo / "src" / "app.py").read_bytes() == original
    assert (repo / "other.txt").read_text(encoding="utf-8") == "user work\n"


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
    original = _original_bytes(repo)
    state = capture_worktree_file_state(repo, ["src/app.py", "src/created.py"])
    assert state == {"src/app.py": _original_bytes(repo), "src/created.py": None}
    (repo / "src" / "app.py").write_text("changed\n", encoding="utf-8")
    (repo / "src" / "created.py").write_text("created\n", encoding="utf-8")
    assert restore_worktree_files(repo, state) is True
    assert (repo / "src" / "app.py").read_bytes() == original
    assert not (repo / "src" / "created.py").exists()


def test_apply_bounds_are_reasonable():
    assert MAX_WORKTREE_APPLY_FILE_BYTES >= 64 * 1024
    assert WorktreeApplyResult().success is False
    assert len(compute_patch_hash("")) == 64
