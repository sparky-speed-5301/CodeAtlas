"""Small, shell-free Git subprocess wrapper."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

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
    """Run Git without a shell and capture text stdout/stderr.

    ``input`` is handed to Git as raw UTF-8 bytes: text-mode pipes would
    translate ``\\n`` to ``os.linesep``, which corrupts patch payloads such as
    unified diffs on Windows.
    """
    command = tuple(str(arg) for arg in args)
    if any("\x00" in arg for arg in command):
        raise ValueError("Git arguments cannot contain NUL bytes")
    input_bytes = input.encode("utf-8") if input is not None else None
    result = subprocess.run(
        [str(git_executable()), *command],
        cwd=str(cwd) if cwd is not None else None,
        input=input_bytes,
        shell=False,
        check=False,
        capture_output=True,
    )
    text_result = subprocess.CompletedProcess(
        command,
        result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace") if result.stdout is not None else None,
        stderr=result.stderr.decode("utf-8", errors="replace") if result.stderr is not None else None,
    )
    if check and text_result.returncode:
        raise GitCommandError(command, text_result.returncode, text_result.stderr or "")
    return text_result
