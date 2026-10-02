from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codeatlas.git.diff import extract_diff
from codeatlas.git.errors import RefError, SnapshotCleanupError
from codeatlas.git.refs import resolve_ref
from codeatlas.git.snapshot import temporary_snapshot
from codeatlas.orchestrator import run_review


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "CodeAtlas Test")
    git(repo, "config", "user.email", "codeatlas@example.test")
    (repo / "same.py").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "delete.txt").write_text("remove\n", encoding="utf-8")
    (repo / "rename.txt").write_text("rename me\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "same.py").write_text("one\nchanged\n", encoding="utf-8")
    (repo / "added.ts").write_text("export const added = true;\n", encoding="utf-8")
    (repo / "delete.txt").unlink()
    git(repo, "mv", "rename.txt", "renamed.txt")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, base, head


def test_invalid_repository_and_refs(tmp_path: Path, repository: tuple[Path, str, str]) -> None:
    repo, _, _ = repository
    invalid = run_review(tmp_path / "missing", "main", "HEAD")
    assert any("RepositoryError" in error for error in invalid.manifest.errors)
    with pytest.raises(RefError):
        resolve_ref(repo, "does-not-exist")
    invalid_base = run_review(repo, "missing-base", "HEAD")
    invalid_head = run_review(repo, "HEAD", "missing-head")
    assert any("RefError" in error for error in invalid_base.manifest.errors)
    assert any("RefError" in error for error in invalid_head.manifest.errors)


def test_diff_reports_all_change_types(repository: tuple[Path, str, str]) -> None:
    repo, base, head = repository
    diff = extract_diff(repo, base, head)
    statuses = {change.path: change.status.value for change in diff.files}
    assert statuses["same.py"] == "modified"
    assert statuses["added.ts"] == "added"
    assert statuses["delete.txt"] == "deleted"
    assert statuses["renamed.txt"] == "renamed"
    assert diff.files[0].new_ranges or diff.files[0].old_ranges


def test_empty_diff_is_explicit(repository: tuple[Path, str, str]) -> None:
    repo, base, _ = repository
    assert extract_diff(repo, base, base).files == ()


def test_snapshot_cleanup_runs_after_failure(repository: tuple[Path, str, str]) -> None:
    repo, _, head = repository
    snapshot_path: Path | None = None
    with pytest.raises(RuntimeError):
        with temporary_snapshot(repo, head) as snapshot:
            snapshot_path = snapshot.path
            raise RuntimeError("simulated review failure")
    assert snapshot_path is not None and not snapshot_path.exists()


def test_snapshot_cleanup_failure_is_visible(repository: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _, head = repository
    import codeatlas.git.snapshot as snapshot_module

    context = temporary_snapshot(repo, head)
    snapshot = context.__enter__()
    real_run_git = snapshot_module.run_git

    def fail_remove(args, **kwargs):
        if list(args[:2]) == ["worktree", "remove"]:
            return subprocess.CompletedProcess(args, 1, "cleanup failed", "cleanup failed")
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(snapshot_module, "run_git", fail_remove)
    with pytest.raises(SnapshotCleanupError):
        context.__exit__(None, None, None)
    assert not snapshot.path.exists()


def test_review_writes_outputs_and_preserves_original(repository: tuple[Path, str, str], tmp_path: Path) -> None:
    repo, base, head = repository
    original = {path.name: path.read_bytes() for path in repo.iterdir() if path.is_file()}
    json_path = tmp_path / "nested" / "review.json"
    markdown_path = tmp_path / "nested" / "review.md"
    manifest_path = tmp_path / "nested" / "manifest.json"
    evidence_path = tmp_path / "nested" / "events.jsonl"
    result = run_review(
        repo,
        base,
        head,
        json_output=json_path,
        markdown_output=markdown_path,
        manifest_output=manifest_path,
        evidence_output=evidence_path,
    )
    assert not result.manifest.errors
    assert result.manifest.base_commit == base
    assert result.manifest.head_commit == head
    assert set(result.manifest.detected_languages) == {"python", "typescript", "unknown"}
    assert json.loads(json_path.read_text()) == json.loads(manifest_path.read_text())
    assert "No findings" in markdown_path.read_text()
    events = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "run_started", "repository_validated", "commits_resolved", "diff_extracted",
        "languages_detected", "snapshot_created", "run_completed",
    ]
    assert {path.name: path.read_bytes() for path in repo.iterdir() if path.is_file()} == original
