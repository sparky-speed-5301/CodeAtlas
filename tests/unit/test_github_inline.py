"""Tests for Phase 9B: inline GitHub PR comments (fake transport only)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.github import FakeGitHubTransport, SHAMismatchError, parse_marker, run_github_pr_review
from codeatlas.github.adapter import build_diff_line_map, classify_inline_target

cli_runner = CliRunner()

BASE_APP = "def f(x):\n    return x\n"
HEAD_APP = "def f(x):\n    token = 'AKIAIOSFODNN7EXAMPLE'\n    return x\n"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _build_pr_repo(root: Path) -> tuple[str, str]:
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
    return _git_diff_for(BASE_APP, HEAD_APP)


def _git_diff_for(before: str, after: str) -> str:
    """Produce a real unified diff for two file versions."""
    with tempfile_dir() as temp:
        root = Path(temp)
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text(before, encoding="utf-8")

        def git(*args: str) -> str:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout

        git("init", "-q")
        git("config", "user.email", "t@t.invalid")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "b")
        (root / "src" / "app.py").write_text(after, encoding="utf-8")
        return git("diff")


import contextlib


@contextlib.contextmanager
def tempfile_dir():
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        yield temp


def _transport(root: Path, base_sha: str, head_sha: str, **kwargs) -> FakeGitHubTransport:
    defaults: dict = {
        "metadata": {
            "number": 42,
            "title": "Add token",
            "base_sha": base_sha,
            "head_sha": head_sha,
        },
        "files": [{"filename": "src/app.py", "status": "modified"}],
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
# Line map and classification (pure functions)
# ---------------------------------------------------------------------------

DIFF = _diff_text()


class TestLineMap:
    def test_added_lines_mapped(self):
        line_map = build_diff_line_map(DIFF)
        assert 2 in line_map["src/app.py"]["added"]
        assert 1 in line_map["src/app.py"]["context"]
        assert 3 in line_map["src/app.py"]["context"]

    def test_unchanged_line_is_context(self):
        line_map = build_diff_line_map(DIFF)
        decision, details = classify_inline_target(
            {"file": "src/app.py", "start_line": 1, "end_line": 1}, line_map
        )
        assert decision == "fallback"
        assert details["reason"] == "unchanged_context_line"

    def test_invalid_line_range_suppressed(self):
        line_map = build_diff_line_map(DIFF)
        decision, details = classify_inline_target(
            {"file": "src/app.py", "start_line": 5, "end_line": 2}, line_map
        )
        assert decision == "suppress" and details["reason"] == "invalid_line_coordinates"

    def test_deleted_line_suppressed(self):
        diff = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-def gone():\n"
            "+def here():\n"
            "     return 1\n"
        )
        line_map = build_diff_line_map(diff)
        # Old line 1 was deleted; deleted lines exist only on the old side and
        # can never anchor an inline comment.
        assert 1 in line_map["src/app.py"]["deleted"]
        # A line beyond the file has no mapping at all.
        decision, details = classify_inline_target(
            {"file": "src/app.py", "start_line": 99, "end_line": 99}, line_map
        )
        assert decision == "suppress" and details["reason"] == "line_outside_diff"

    def test_multi_line_fully_added_is_inline(self):
        diff = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,4 @@\n"
            " def f(x):\n"
            "+    a = 1\n"
            "+    b = 2\n"
            "+    c = 3\n"
            "     return x\n"
        )
        line_map = build_diff_line_map(diff)
        decision, details = classify_inline_target(
            {"file": "src/app.py", "start_line": 2, "end_line": 4}, line_map
        )
        assert decision == "inline"
        assert details == {"path": "src/app.py", "line": 4, "start_line": 2}

    def test_ambiguous_mapping_falls_back(self):
        diff = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def f(x):\n"
            "+    a = 1\n"
            "     return x\n"
        )
        line_map = build_diff_line_map(diff)
        decision, details = classify_inline_target(
            {"file": "src/app.py", "start_line": 1, "end_line": 2}, line_map
        )
        assert decision == "fallback"
        assert details["reason"] == "ambiguous_line_mapping"

    def test_file_not_in_diff_suppressed(self):
        line_map = build_diff_line_map(DIFF)
        decision, details = classify_inline_target(
            {"file": "src/other.py", "start_line": 1, "end_line": 1}, line_map
        )
        assert decision == "suppress" and details["reason"] == "file_not_in_diff"


# ---------------------------------------------------------------------------
# End-to-end inline behavior
# ---------------------------------------------------------------------------

def test_inline_valid_finding_posts_inline_comment(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, post=True, inline=True
    )
    assert transport.write_calls == 1
    assert len(transport.posted_inline) == 1
    inline = transport.posted_inline[0]
    assert inline["path"] == "src/app.py"
    assert inline["commit_id"] == head_sha
    assert report.inline_comments_posted == 1
    assert report.issue_comments_posted == 0
    marker = parse_marker(inline["body"])
    assert marker["mode"] == "inline"
    # Raw secret value never appears in any body.
    assert "AKIAIOSFODNN7EXAMPLE" not in inline["body"]


def test_inline_dry_run_zero_writes(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, inline=True
    )
    assert transport.write_calls == 0
    assert report.mode == "dry_run+inline"
    assert report.comments_planned == 1


def test_inline_without_post_never_writes(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport, inline=True)
    assert transport.write_calls == 0


def test_inline_duplicate_suppressed_from_inline_comment(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    first = _transport(tmp_path / "work", base_sha, head_sha)
    run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=first, post=True, inline=True)
    assert first.write_calls == 1

    second = _transport(
        tmp_path / "work", base_sha, head_sha,
        inline_comments=[{"comment_id": 5, "body": first.posted_bodies[0]}],
    )
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=second, post=True, inline=True
    )
    assert second.write_calls == 0
    assert report.comments_planned == 0
    assert report.comments_skipped_duplicate == 1


def test_inline_duplicate_suppressed_from_issue_comment(tmp_path: Path):
    """A finding already posted as an issue comment is not duplicated inline."""
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    issue_run = _transport(tmp_path / "work", base_sha, head_sha)
    run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=issue_run, post=True)
    assert issue_run.write_calls == 1

    inline_run = _transport(
        tmp_path / "work", base_sha, head_sha,
        comments=[{"comment_id": 5, "body": issue_run.posted_bodies[0]}],
    )
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=inline_run, post=True, inline=True
    )
    assert inline_run.write_calls == 0
    assert report.comments_skipped_duplicate == 1


def test_sha_change_before_write_batch_aborts(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(
        tmp_path / "work", base_sha, head_sha, head_sha_on_write_check="d" * 40,
    )
    tree_before = _tree_hash(tmp_path / "work")
    with pytest.raises(SHAMismatchError, match="before the comment write batch"):
        run_github_pr_review(
            "o/r", 42, local_repo=tmp_path / "work", transport=transport, post=True, inline=True
        )
    assert transport.write_calls == 0
    assert _tree_hash(tmp_path / "work") == tree_before


def test_issue_mode_unchanged_by_inline_flag(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, post=True, inline=False
    )
    assert transport.posted_inline == []
    assert len(transport.calls) > 0
    assert any(m == "post_comment" for m, _ in transport.calls)
    assert report.issue_comments_posted == 1
    marker = parse_marker(transport.posted_bodies[0])
    assert marker["mode"] == "issue"


def test_legacy_markers_without_mode_still_dedupe(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    seeded = _transport(tmp_path / "work", base_sha, head_sha)
    run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=seeded, post=True)
    marker = parse_marker(seeded.posted_bodies[0])
    assert marker is not None and marker["finding"].startswith("CA-SECRET-")
    # Legacy 9A marker (no mode= segment) must still suppress.
    legacy_body = seeded.posted_bodies[0].replace(f" mode={marker['mode']}", "")
    assert "mode=" not in legacy_body
    legacy = _transport(
        tmp_path / "work", base_sha, head_sha,
        comments=[{"comment_id": 9, "body": legacy_body}],
    )
    report = run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=legacy, post=True)
    assert report.comments_skipped_duplicate == 1


def test_inline_original_repository_unchanged(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    tree_before = _tree_hash(tmp_path / "work")
    run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, post=True, inline=True
    )
    assert _tree_hash(tmp_path / "work") == tree_before
    assert _git(tmp_path / "work", "status", "--porcelain") == ""


def test_cli_inline_flag_end_to_end(tmp_path: Path, monkeypatch):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    monkeypatch.setattr("codeatlas.github.adapter.GhCliTransport", lambda: transport)
    result = cli_runner.invoke(app, [
        "github", "review", "--repo", "o/r", "--pr", "42",
        "--local-repo", str(tmp_path / "work"), "--post", "--inline",
    ])
    assert result.exit_code == 0, result.output
    assert "inline: 1, issue: 0" in result.output
    assert transport.write_calls == 1
    assert len(transport.posted_inline) == 1


def test_cli_inline_dry_run_no_writes(tmp_path: Path, monkeypatch):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    monkeypatch.setattr("codeatlas.github.adapter.GhCliTransport", lambda: transport)
    result = cli_runner.invoke(app, [
        "github", "review", "--repo", "o/r", "--pr", "42",
        "--local-repo", str(tmp_path / "work"), "--inline",
    ])
    assert result.exit_code == 0, result.output
    assert "Write calls:      0" in result.output
