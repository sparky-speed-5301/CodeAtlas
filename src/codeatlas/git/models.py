"""Data models returned by Git operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ChangeStatus(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"


@dataclass(frozen=True)
class LineRange:
    start: int
    count: int = 1

    @property
    def end(self) -> int:
        return self.start + max(self.count, 1) - 1


@dataclass(frozen=True)
class FileChange:
    status: ChangeStatus
    path: str
    old_path: str | None = None
    old_ranges: tuple[LineRange, ...] = ()
    new_ranges: tuple[LineRange, ...] = ()

    @property
    def added_ranges(self) -> tuple[LineRange, ...]:
        return self.new_ranges

    @property
    def deleted_ranges(self) -> tuple[LineRange, ...]:
        return self.old_ranges


@dataclass(frozen=True)
class Diff:
    base: str
    head: str
    files: tuple[FileChange, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ResolvedRef:
    name: str
    commit: str


@dataclass(frozen=True)
class Repository:
    path: Path
    root: Path
