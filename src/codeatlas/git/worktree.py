"""Compatibility-facing names for ephemeral analysis worktrees."""

from .snapshot import Snapshot, temporary_snapshot

temporary_worktree = temporary_snapshot

__all__ = ["Snapshot", "temporary_snapshot", "temporary_worktree"]
