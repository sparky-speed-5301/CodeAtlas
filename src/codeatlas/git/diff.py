"""Deterministic Git diff extraction."""

from __future__ import annotations

import re
from pathlib import Path

from .errors import GitError
from .executable import run_git
from .models import ChangeStatus, Diff, FileChange, LineRange

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def extract_diff(repository: str | Path, base: str, head: str = "HEAD") -> Diff:
    """Extract file statuses and both sides' changed line ranges."""
    result = run_git(
        ["diff", "--find-renames", "--find-copies", "--unified=0", "--no-color", base, head, "--"],
        cwd=repository,
    )
    changes: list[FileChange] = []
    for raw in result.stdout.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split(" ")
            if len(parts) < 4:
                raise GitError(f"Malformed Git diff header: {raw}")
            old = parts[2][2:]
            new = parts[3][2:]
            current = {"old": old, "new": new, "old_ranges": [], "new_ranges": []}
            changes.append(_status_change(current))
        elif raw.startswith("new file mode ") and changes:
            item = changes[-1]
            changes[-1] = FileChange(ChangeStatus.ADDED, item.path, None, item.old_ranges, item.new_ranges)
        elif raw.startswith("deleted file mode ") and changes:
            item = changes[-1]
            changes[-1] = FileChange(ChangeStatus.DELETED, item.path, item.old_path or item.path, item.old_ranges, item.new_ranges)
        elif raw.startswith("@@ ") and changes:
            match = _HUNK.match(raw)
            if match:
                old_start, old_count, new_start, new_count = match.groups()
                old_n = int(old_count) if old_count is not None else 1
                new_n = int(new_count) if new_count is not None else 1
                changes[-1] = FileChange(
                    changes[-1].status, changes[-1].path, changes[-1].old_path,
                    changes[-1].old_ranges + ((LineRange(int(old_start), old_n),) if old_n else ()),
                    changes[-1].new_ranges + ((LineRange(int(new_start), new_n),) if new_n else ()),
                )
    return Diff(base, head, tuple(changes))


def _status_change(item: dict[str, object]) -> FileChange:
    old, new = str(item["old"]), str(item["new"])
    if old == new:
        status = ChangeStatus.MODIFIED
        old_path = None
    elif old == "/dev/null":
        status, old_path = ChangeStatus.ADDED, None
    elif new == "/dev/null":
        status, old_path = ChangeStatus.DELETED, old
    else:
        status, old_path = ChangeStatus.RENAMED, old
    return FileChange(status, new if new != "/dev/null" else old, old_path)
