"""Safe resolution of Git names to immutable commits."""

from __future__ import annotations

from pathlib import Path

from .errors import RefError
from .executable import run_git
from .models import ResolvedRef


def resolve_ref(repository: str | Path, ref: str) -> ResolvedRef:
    if not ref or "\x00" in ref:
        raise RefError("A non-empty ref is required")
    result = run_git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], cwd=repository, check=False)
    commit = result.stdout.strip()
    if result.returncode or len(commit) != 40:
        raise RefError(f"Unable to resolve ref: {ref}")
    return ResolvedRef(ref, commit)


def resolve_base_head(repository: str | Path, base: str, head: str = "HEAD") -> tuple[ResolvedRef, ResolvedRef]:
    return resolve_ref(repository, base), resolve_ref(repository, head)
