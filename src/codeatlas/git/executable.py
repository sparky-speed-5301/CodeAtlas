"""Small, shell-free Git subprocess wrapper."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .errors import GitCommandError, GitExecutableError


def git_executable() -> Path:
    """Return the Git executable, or fail with a useful typed error."""
    found = shutil.which("git")
    if not found:
        raise GitExecutableError("Git executable was not found on PATH")
    return Path(found)


def run_git(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    check: bool = True,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git without a shell and capture text stdout/stderr."""
    command = tuple(str(arg) for arg in args)
    if any("\x00" in arg for arg in command):
        raise ValueError("Git arguments cannot contain NUL bytes")
    result = subprocess.run(
        [str(git_executable()), *command],
        cwd=str(cwd) if cwd is not None else None,
        input=input,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode:
        raise GitCommandError(command, result.returncode, result.stderr)
    return result
