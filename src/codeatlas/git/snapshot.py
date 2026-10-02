"""Ephemeral detached worktrees for read-only analysis."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import SnapshotCleanupError
from .executable import run_git
from .refs import resolve_ref
from .repository import validate_repository


@dataclass(frozen=True)
class Snapshot:
    path: Path
    commit: str


class temporary_snapshot:
    def __init__(self, repository: str | Path, ref: str = "HEAD") -> None:
        self.repository = validate_repository(repository)
        self.ref = ref
        self._path: Path | None = None
        self._commit: str | None = None

    def __enter__(self) -> Snapshot:
        resolved = resolve_ref(self.repository.root, self.ref)
        path = Path(tempfile.mkdtemp(prefix="codeatlas-snapshot-"))
        shutil.rmtree(path)
        try:
            run_git(["worktree", "add", "--detach", str(path), resolved.commit], cwd=self.repository.root)
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            raise
        self._path, self._commit = path, resolved.commit
        return Snapshot(path, resolved.commit)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._path is not None:
            remove_result = run_git(["worktree", "remove", "--force", str(self._path)], cwd=self.repository.root, check=False)
            cleanup_error: Exception | None = None
            if remove_result.returncode:
                cleanup_error = SnapshotCleanupError(
                    f"Unable to remove temporary worktree {self._path}: {remove_result.stderr.strip()}"
                )
            if self._path.exists():
                try:
                    shutil.rmtree(self._path)
                except OSError as error:
                    cleanup_error = SnapshotCleanupError(f"Unable to delete temporary snapshot {self._path}: {error}")
            self._path = None
            if cleanup_error is not None:
                raise cleanup_error
