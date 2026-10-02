"""Repository file scanner with configurable exclusions and safety bounds."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Sequence

from codeatlas.repository.files import classify_file
from codeatlas.repository.models import RepositoryFile

DEFAULT_EXCLUSIONS: tuple[str, ...] = (
    ".git/**",
    ".git",
    "node_modules/**",
    "node_modules",
    ".venv/**",
    ".venv",
    "venv/**",
    "venv",
    "__pycache__/**",
    "__pycache__",
    "dist/**",
    "dist",
    "build/**",
    "build",
    "coverage/**",
    "coverage",
    "vendor/**",
    "vendor",
)


def _matches_any_pattern(path_str: str, patterns: Sequence[str]) -> bool:
    """Check if posix path string matches any glob pattern."""
    norm = path_str.replace("\\", "/").strip("/")
    parts = norm.split("/")

    for pattern in patterns:
        pat = pattern.replace("\\", "/").strip("/")
        # Exact match or full glob match
        if fnmatch.fnmatch(norm, pat):
            return True
        # If pattern is like "node_modules/**" or "node_modules", match if any parent dir matches
        clean_pat = pat.rstrip("/*")
        if any(part == clean_pat for part in parts):
            return True
        if fnmatch.fnmatch(parts[0], clean_pat):
            return True
    return False


def scan_repository(
    repo_root: Path | str,
    *,
    exclude_paths: Sequence[str] | None = None,
    max_file_bytes: int = 1_000_000,
) -> dict[str, RepositoryFile]:
    """Scan the repository for files, applying default and custom exclusions.

    Never traverses into .git or outside the repository root.
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        return {}

    all_patterns = list(DEFAULT_EXCLUSIONS)
    if exclude_paths:
        all_patterns.extend(exclude_paths)

    inventory: dict[str, RepositoryFile] = {}

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        try:
            rel_path = path.relative_to(root).as_posix()
        except ValueError:
            # File is outside repo root
            continue

        # Prevent escaping or indexing .git internals
        if rel_path.startswith(".git/") or rel_path == ".git":
            continue

        if _matches_any_pattern(rel_path, all_patterns):
            continue

        repo_file = classify_file(root, rel_path, max_bytes=max_file_bytes)
        inventory[rel_path] = repo_file

    return inventory


__all__ = ["DEFAULT_EXCLUSIONS", "scan_repository"]
