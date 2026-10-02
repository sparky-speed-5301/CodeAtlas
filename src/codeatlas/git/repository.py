"""Validation and discovery of Git repositories."""

from __future__ import annotations

from pathlib import Path

from .errors import RepositoryError
from .executable import run_git
from .models import Repository


def validate_repository(path: str | Path) -> Repository:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_dir():
        raise RepositoryError(f"Repository path is not a directory: {candidate}")
    result = run_git(["rev-parse", "--show-toplevel"], cwd=candidate, check=False)
    if result.returncode:
        raise RepositoryError(f"Not a Git repository: {candidate}")
    root = Path(result.stdout.strip()).resolve()
    inside = run_git(["rev-parse", "--is-inside-work-tree"], cwd=candidate, check=False)
    if inside.returncode or inside.stdout.strip().lower() != "true":
        raise RepositoryError(f"Not a Git work tree: {candidate}")
    return Repository(path=candidate, root=root)
