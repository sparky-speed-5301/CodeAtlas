"""Errors raised by the safe Git primitives."""

from __future__ import annotations


class GitError(RuntimeError):
    """Base class for expected Git operation failures."""


class GitExecutableError(GitError):
    """Git is unavailable or could not be executed."""


class GitCommandError(GitError):
    """A Git command returned a non-zero status."""

    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.args_passed = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")


class RepositoryError(GitError):
    """The supplied path is not a usable Git work tree."""


class RefError(GitError):
    """A Git ref cannot be resolved to a commit."""


class SnapshotError(GitError):
    """A temporary snapshot could not be created or cleaned up."""


class SnapshotCleanupError(SnapshotError):
    """A temporary snapshot could not be removed safely."""
