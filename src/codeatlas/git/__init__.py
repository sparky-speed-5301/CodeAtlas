"""Git snapshot, diff, and worktree operations."""

from .diff import extract_diff
from .models import ChangeStatus, Diff, FileChange, LineRange, Repository, ResolvedRef
from .refs import resolve_base_head, resolve_ref
from .repository import validate_repository
from .snapshot import Snapshot, temporary_snapshot
from .worktree import temporary_worktree

from .errors import GitError, RefError, RepositoryError, SnapshotCleanupError

__all__ = [
    "ChangeStatus",
    "Diff",
    "FileChange",
    "GitError",
    "LineRange",
    "RefError",
    "Repository",
    "RepositoryError",
    "ResolvedRef",
    "Snapshot",
    "SnapshotCleanupError",
    "extract_diff",
    "resolve_base_head",
    "resolve_ref",
    "temporary_snapshot",
    "temporary_worktree",
    "validate_repository",
]
