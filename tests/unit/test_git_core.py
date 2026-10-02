from __future__ import annotations

import subprocess

import pytest

from codeatlas.core import Language, detect_language
from codeatlas.git.diff import extract_diff
from codeatlas.git.errors import RefError, RepositoryError
from codeatlas.git.repository import validate_repository
from codeatlas.git.refs import resolve_ref
from codeatlas.git.snapshot import temporary_snapshot


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "main.py").write_text("one\ntwo\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_validate_resolve_and_diff(repo):
    base = resolve_ref(repo, "HEAD").commit
    (repo / "main.py").write_text("one\nchanged\n", encoding="utf-8")
    (repo / "new.ts").write_text("export const x = 1;\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "change")
    checked = validate_repository(repo)
    assert checked.root == repo.resolve()
    diff = extract_diff(repo, base, "HEAD")
    assert {change.path for change in diff.files} == {"main.py", "new.ts"}
    assert any(change.status.value == "added" for change in diff.files)
    assert any(change.new_ranges for change in diff.files)


def test_snapshot_is_detached_and_cleaned(repo):
    original = (repo / "main.py").read_text()
    with temporary_snapshot(repo) as snapshot:
        assert snapshot.path.exists()
        assert snapshot.path != repo
        assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=snapshot.path).returncode != 0
        (snapshot.path / "main.py").write_text("not persisted", encoding="utf-8")
    assert not snapshot.path.exists()
    assert (repo / "main.py").read_text() == original


def test_invalid_repository_and_language_detection(tmp_path):
    invalid = tmp_path / "file"
    invalid.write_text("not a directory", encoding="utf-8")
    with pytest.raises(RepositoryError):
        validate_repository(invalid)
    with pytest.raises(RefError):
        resolve_ref(tmp_path, "missing")
    assert detect_language("x.TSX") is Language.TYPESCRIPT
    assert detect_language("x.py") is Language.PYTHON
    assert detect_language("x.txt") is Language.UNKNOWN
