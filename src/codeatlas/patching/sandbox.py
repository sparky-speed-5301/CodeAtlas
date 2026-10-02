"""Ephemeral isolated sandbox worktrees for patch application and validation."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from codeatlas.git.errors import SnapshotCleanupError
from codeatlas.git.executable import run_git
from codeatlas.git.refs import resolve_ref
from codeatlas.git.repository import validate_repository


@dataclass(frozen=True)
class PatchSandbox:
    """Detached, isolated sandbox worktree for patch application."""

    sandbox_id: str
    path: Path
    base_commit: str
    repository_root: Path


class temporary_patch_sandbox:
    """Context manager creating a detached temporary worktree for isolated patch testing.

    ``retain_on_failure`` is an explicit opt-in for local debugging only: when
    the validated flow calls :meth:`retain` after a failure, the sandbox
    directory is left in place and reported via ``cleanup_status="retained"``.
    The normal review command never enables it.
    """

    def __init__(self, repository: str | Path, base_commit: str, *, retain_on_failure: bool = False) -> None:
        self.repo = validate_repository(repository)
        self.base_ref = base_commit
        self.sandbox_id = f"sandbox-{uuid.uuid4().hex[:8]}"
        self.retain_on_failure = retain_on_failure
        self._retain = False
        self._path: Path | None = None
        self._resolved_commit: str | None = None

    def retain(self) -> None:
        """Ask the context manager to keep the sandbox directory on exit."""
        if not self.retain_on_failure:
            raise RuntimeError("retain_on_failure was not enabled for this sandbox")
        self._retain = True

    @property
    def retained(self) -> bool:
        return self._retain

    def __enter__(self) -> PatchSandbox:
        resolved = resolve_ref(self.repo.root, self.base_ref)
        self._resolved_commit = resolved.commit

        temp_dir = Path(tempfile.mkdtemp(prefix=f"codeatlas-sandbox-{self.sandbox_id}-"))
        shutil.rmtree(temp_dir, ignore_errors=True)

        try:
            run_git(
                ["worktree", "add", "--detach", str(temp_dir), resolved.commit],
                cwd=self.repo.root,
            )
        except Exception as err:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to create isolated patch sandbox: {err}") from err

        self._path = temp_dir
        return PatchSandbox(
            sandbox_id=self.sandbox_id,
            path=temp_dir,
            base_commit=resolved.commit,
            repository_root=self.repo.root,
        )

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._retain and self.retain_on_failure and self._path is not None:
            # Retained for local debugging; the caller reports cleanup_status.
            self._path = None
            return

        cleanup_error: Exception | None = None
        if self._path is not None:
            # Force remove worktree from git tracking
            res = run_git(
                ["worktree", "remove", "--force", str(self._path)],
                cwd=self.repo.root,
                check=False,
            )
            if res.returncode != 0:
                cleanup_error = SnapshotCleanupError(
                    f"Unable to remove sandbox worktree {self._path}: {res.stderr.strip()}"
                )

            # Ensure filesystem directory is wiped
            if self._path.exists():
                try:
                    shutil.rmtree(self._path)
                except OSError as err:
                    cleanup_error = SnapshotCleanupError(
                        f"Unable to delete sandbox directory {self._path}: {err}"
                    )

            # Prune dead worktree metadata
            run_git(["worktree", "prune"], cwd=self.repo.root, check=False)
            self._path = None

        if cleanup_error is not None:
            raise cleanup_error


__all__ = [
    "PatchSandbox",
    "temporary_patch_sandbox",
]
