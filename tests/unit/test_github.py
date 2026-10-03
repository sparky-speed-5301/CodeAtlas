"""Tests for Phase 9A: read-only GitHub pull-request integration (fake transport only)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.github import (
    FakeGitHubTransport,
    GitHubAuthError,
    GitHubNetworkError,
    GitHubResponseError,
    SHAMismatchError,
    parse_comments,
    parse_marker,
    parse_pr_files,
    parse_pr_metadata,
    run_github_pr_review,
)
from codeatlas.github.adapter import _contains_raw_secret, _is_anchored, render_finding_comment

cli_runner = CliRunner()

BASE_APP = "def f(x):\n    return x\n"
HEAD_APP = "def f(x):\n    token = 'AKIAIOSFODNN7EXAMPLE'\n    return x\n"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _build_pr_repo(root: Path) -> tuple[str, str]:
    """Local checkout whose base->head change introduces a hardcoded secret."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(BASE_APP, encoding="utf-8")

    def commit_all(msg: str) -> str:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", msg)
        return _git(root, "rev-parse", "HEAD")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.invalid")
    _git(root, "config", "user.name", "t")
    base_sha = commit_all("base")
    (root / "src" / "app.py").write_text(HEAD_APP, encoding="utf-8")
    head_sha = commit_all("head")
    return base_sha, head_sha


def _diff_text() -> str:
    return (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def f(x):\n"
        "+    token = 'REDACTED'\n"
        "     return x\n"
    )


def _transport(root: Path, base_sha: str, head_sha: str, **kwargs) -> FakeGitHubTransport:
    defaults: dict = {
        "metadata": {
            "number": 42,
            "title": "Add token",
            "state": "OPEN",
            "base_ref": "main",
            "head_ref": "feature",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_files_count": 1,
        },
        "files": [{"filename": "src/app.py", "status": "modified", "additions": 1, "deletions": 0}],
        "diff_text": _diff_text(),
        "comments": [],
    }
    defaults.update(kwargs)
    return FakeGitHubTransport(**defaults)


def _tree_hash(work: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for p in sorted(work.rglob("*")):
        if ".git" in p.relative_to(work).parts or "__pycache__" in p.parts:
            continue
        if p.is_file():
            digest.update(str(p.relative_to(work)).encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Transport parsing
# ---------------------------------------------------------------------------

def test_pr_metadata_fetch():
    transport = FakeGitHubTransport(metadata={"number": 7, "base_sha": "a", "head_sha": "b"})
    meta = transport.get_pr_metadata("o/r", 7)
    assert meta.number == 7 and meta.head_sha == "b"
    assert ("get_pr_metadata", ("o/r", 7)) in transport.calls


def test_diff_fetch():
    transport = FakeGitHubTransport(
        metadata={"number": 7, "base_sha": "a", "head_sha": "b"}, diff_text="diff"
    )
    assert transport.get_pr_diff("o/r", 7) == "diff"
    assert ("get_pr_diff", ("o/r", 7)) in transport.calls


def test_parse_pr_metadata_malformed_response():
    with pytest.raises(GitHubResponseError):
        parse_pr_metadata("this is not json {{{")
    with pytest.raises(GitHubResponseError):
        parse_pr_metadata(json.dumps({"number": 1}))  # missing SHAs
    with pytest.raises(GitHubResponseError):
        parse_pr_metadata(json.dumps([1, 2, 3]))  # not an object


def test_parse_pr_files_malformed_response():
    with pytest.raises(GitHubResponseError):
        parse_pr_files("garbage")
    with pytest.raises(GitHubResponseError):
        parse_pr_files(json.dumps([{"additions": 1}]))  # missing path


def test_parse_comments_malformed_response():
    with pytest.raises(GitHubResponseError):
        parse_comments("{{{")


def test_missing_authentication(monkeypatch):
    import codeatlas.github.transport as transport_module

    monkeypatch.setattr(transport_module.shutil, "which", lambda _: None)
    transport = transport_module.GhCliTransport()
    with pytest.raises(GitHubAuthError, match="gh auth login"):
        transport.get_pr_metadata("o/r", 1)


# ---------------------------------------------------------------------------
# End-to-end adapter behavior with a local PR checkout
# ---------------------------------------------------------------------------

def test_dry_run_makes_zero_write_calls(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport)
    assert transport.write_calls == 0
    assert report.mode == "dry_run"
    assert report.comments_planned == 1
    assert report.findings_anchored == 1
    assert report.posted_comment_ids == []


def test_post_mode_publishes_anchored_comment(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, post=True
    )
    assert transport.write_calls == 1
    assert report.comments_posted == 1
    body = transport.posted_bodies[0]
    assert "`src/app.py:2-" in body
    assert "HARD_CODED_SECRET" in body
    # The raw secret value never reaches the comment body.
    assert "AKIAIOSFODNN7EXAMPLE" not in body
    # Idempotency marker binds PR, head SHA, finding, and run ID.
    marker = parse_marker(body)
    assert marker is not None
    assert marker["pr"] == "42" and marker["head"] == head_sha and marker["run"].startswith("ghrun-")


def test_duplicate_comment_suppression_across_run_ids(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    first = _transport(tmp_path / "work", base_sha, head_sha)
    run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=first, post=True)
    assert first.write_calls == 1

    existing_body = first.posted_bodies[0]
    second = _transport(tmp_path / "work", base_sha, head_sha, comments=[{"comment_id": 1, "body": existing_body}])
    report = run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=second, post=True)
    # A different run ID with the same finding + head SHA must not duplicate.
    assert second.write_calls == 0
    assert report.comments_planned == 0
    assert report.comments_skipped_duplicate == 1


def test_sha_mismatch_fails_closed(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(
        tmp_path / "work", base_sha, head_sha,
        metadata={"number": 42, "base_sha": base_sha, "head_sha": "f" * 40},
    )
    tree_before = _tree_hash(tmp_path / "work")
    with pytest.raises(SHAMismatchError):
        run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport)
    assert transport.write_calls == 0
    assert _tree_hash(tmp_path / "work") == tree_before


def test_sha_change_during_run_fails_closed_before_writes(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(
        tmp_path / "work", base_sha, head_sha, head_sha_on_refetch="e" * 40,
    )
    with pytest.raises(SHAMismatchError, match="changed during the review run"):
        run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport, post=True)
    assert transport.write_calls == 0


def test_changed_files_cross_check_fails_closed(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(
        tmp_path / "work", base_sha, head_sha,
        files=[
            {"filename": "src/app.py"},
            {"filename": "src/phantom.py"},
        ],
    )
    with pytest.raises(SHAMismatchError, match="do not match the PR files"):
        run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport)
    assert transport.write_calls == 0


def test_network_api_failure_fails_closed(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(
        tmp_path / "work", base_sha, head_sha,
        metadata_error=GitHubNetworkError("simulated API outage"),
    )
    tree_before = _tree_hash(tmp_path / "work")
    with pytest.raises(GitHubNetworkError):
        run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport, post=True)
    assert transport.write_calls == 0
    assert _tree_hash(tmp_path / "work") == tree_before


def test_diff_fetch_failure_fails_closed(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha, diff_error=GitHubNetworkError("diff failed"))
    with pytest.raises(GitHubNetworkError):
        run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport)
    assert transport.write_calls == 0


def test_original_local_repository_unchanged(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    tree_before = _tree_hash(tmp_path / "work")
    run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport, post=True)
    assert _tree_hash(tmp_path / "work") == tree_before
    assert _git(tmp_path / "work", "status", "--porcelain") == ""


def test_redacted_finding_suppressed(tmp_path: Path):
    finding = {
        "id": "CA-REV-LEAK",
        "file": "src/app.py",
        "start_line": 1,
        "end_line": 2,
        "claim": "uses AKIA1234567890EXAMPLE",
        "impact": "leak",
    }
    body = render_finding_comment(42, "a" * 40, "ghrun-x", finding)
    assert _contains_raw_secret(body) is True


def test_anchoring_logic():
    ranges = {"src/app.py": [[5, 6]]}
    anchored = {"file": "src/app.py", "start_line": 5, "end_line": 6}
    unanchored = {"file": "src/app.py", "start_line": 50, "end_line": 51}
    other_file = {"file": "src/other.py", "start_line": 1, "end_line": 2}
    assert _is_anchored(anchored, ranges) is True
    assert _is_anchored(unanchored, ranges) is False
    assert _is_anchored(other_file, ranges) is False


def test_unanchored_finding_suppressed_not_posted():
    """The adapter's anchoring gate is defense in depth behind the validator.

    E2E injection of unanchored findings is impossible by design (the shared
    output validator rejects them first), so the gate is verified directly.
    """
    ranges = {"src/app.py": [[2, 3]]}
    context_only_unanchored = {
        "id": "CA-REV-CTX",
        "file": "src/unchanged.py",
        "start_line": 1,
        "end_line": 1,
    }
    assert _is_anchored(context_only_unanchored, ranges) is False
    # Anchored equivalents pass the gate.
    assert _is_anchored(
        {"file": "src/app.py", "start_line": 2, "end_line": 3}, ranges
    ) is True


def test_cli_post_and_dry_run_mutually_exclusive(tmp_path: Path):
    result = cli_runner.invoke(app, [
        "github", "review", "--repo", "o/r", "--pr", "1", "--post", "--dry-run",
    ])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_cli_review_dry_run_with_fake_transport(tmp_path: Path, monkeypatch):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    monkeypatch.setattr(
        "codeatlas.github.adapter.GhCliTransport", lambda: transport
    )
    report_path = tmp_path / "report.json"
    result = cli_runner.invoke(app, [
        "github", "review", "--repo", "o/r", "--pr", "42",
        "--local-repo", str(tmp_path / "work"),
        "--json-output", str(report_path),
    ])
    assert result.exit_code == 0, result.output
    assert "Mode:             dry_run" in result.output
    assert "Write calls:      0" in result.output
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["write_calls"] == 0 and report["comments_planned"] == 1
    assert transport.write_calls == 0


def test_cli_review_post_with_fake_transport(tmp_path: Path, monkeypatch):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    monkeypatch.setattr("codeatlas.github.adapter.GhCliTransport", lambda: transport)
    result = cli_runner.invoke(app, [
        "github", "review", "--repo", "o/r", "--pr", "42",
        "--local-repo", str(tmp_path / "work"), "--post",
    ])
    assert result.exit_code == 0, result.output
    assert "Comments posted:  1" in result.output
    assert transport.write_calls == 1
    # No review verdict was ever submitted (comments only, never approve).
    assert all(method != "submit_review" for method, _ in transport.calls)


def test_cli_review_github_error_exit(tmp_path: Path, monkeypatch):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha, diff_error=GitHubNetworkError("boom"))
    monkeypatch.setattr("codeatlas.github.adapter.GhCliTransport", lambda: transport)
    result = cli_runner.invoke(app, [
        "github", "review", "--repo", "o/r", "--pr", "42",
        "--local-repo", str(tmp_path / "work"),
    ])
    assert result.exit_code == 1
    assert "boom" in result.output


def test_no_network_in_fake_transport_tests():
    """The fake transport performs no subprocess or network I/O by construction."""
    transport = FakeGitHubTransport(metadata={"number": 1, "base_sha": "a", "head_sha": "b"})
    transport.get_pr_metadata("o/r", 1)
    transport.get_pr_diff("o/r", 1)
    transport.list_comments("o/r", 1)
    assert transport.write_calls == 0
