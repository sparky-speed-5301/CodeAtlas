"""Platform-aware test runner discovery and executable resolution."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Sequence


_SIMULATED_EXECUTABLE: dict[str, bool] = {}


def set_simulated_executable(path: Path | None, executable: bool | None = None) -> None:
    """Set simulated executable permission for a file (useful in cross-platform testing on Windows)."""
    if path is None:
        _SIMULATED_EXECUTABLE.clear()
    elif executable is None:
        _SIMULATED_EXECUTABLE.pop(str(path.resolve()), None)
    else:
        _SIMULATED_EXECUTABLE[str(path.resolve())] = executable


def is_file_executable(path: Path, platform: str = sys.platform) -> bool:
    """Determine whether a file is considered executable on the target platform.

    - On Windows, files with runnable extensions (.exe, .cmd, .bat) or executable rights are accepted.
    - On POSIX, Windows batch/cmd files (.cmd, .bat) are NEVER considered executable.
      The POSIX execution bits (st_mode & 0o111) and os.access(..., os.X_OK) are verified.
    """
    if not path.is_file():
        return False

    resolved_str = str(path.resolve())
    if resolved_str in _SIMULATED_EXECUTABLE:
        return _SIMULATED_EXECUTABLE[resolved_str]

    if platform == "win32":
        return path.suffix.lower() in {".exe", ".cmd", ".bat"} or os.access(path, os.X_OK)

    # POSIX: Windows launchers are strictly rejected
    if path.suffix.lower() in {".cmd", ".bat"}:
        return False

    if sys.platform != "win32":
        try:
            st_mode = path.stat().st_mode
            return bool(st_mode & 0o111) and os.access(path, os.X_OK)
        except OSError:
            return False

    # Running on Windows host but checking against POSIX:
    # On Windows NTFS, chmod does not set 0o111 bits in stat().
    # Check if stat() was monkeypatched with executable bits:
    try:
        st_mode = path.stat().st_mode
        if bool(st_mode & 0o111):
            return True
    except OSError:
        return False

    # On Windows filesystem without explicit executable bits, scripts without Windows extensions
    # (e.g. node_modules/.bin/vitest) are considered executable candidates for POSIX simulation
    return True


def find_js_runner_executable(
    sandbox_path: Path,
    runner_name: str,
    *,
    platform: str = sys.platform,
) -> Path | str | None:
    """Find a concrete executable binary or launcher for a JS runner in sandbox or system.

    On Windows:
      Prefers local .cmd, .exe, .bat, or bare binary in sandbox node_modules/.bin, then system PATH.
    On POSIX:
      Checks for bare executable binary in sandbox node_modules/.bin (never .cmd/.bat), then system PATH.
      If a local file exists but lacks executable permission, it falls back to an available system binary.
    """
    bin_dir = sandbox_path / "node_modules" / ".bin"

    if platform == "win32":
        candidates = [
            bin_dir / f"{runner_name}.cmd",
            bin_dir / f"{runner_name}.exe",
            bin_dir / f"{runner_name}.bat",
            bin_dir / runner_name,
        ]
        for c in candidates:
            if c.is_file():
                return c
        system_bin = shutil.which(runner_name)
        if system_bin:
            return system_bin
        return None

    # POSIX:
    local_bin = bin_dir / runner_name
    if local_bin.is_file():
        if is_file_executable(local_bin, platform):
            return local_bin
        # Present but missing executable permissions on POSIX
        # Attempt deterministic fallback to system runner
        system_bin = shutil.which(runner_name)
        if system_bin and not system_bin.lower().endswith((".cmd", ".bat")):
            return system_bin
        return None

    # Not present in sandbox node_modules/.bin, check system PATH
    system_bin = shutil.which(runner_name)
    if system_bin and not system_bin.lower().endswith((".cmd", ".bat")):
        return system_bin
    return None


def find_js_runner(
    sandbox_path: Path,
    preference: str | None = None,
    *,
    platform: str = sys.platform,
) -> str | None:
    """Find an available JS/TS runner name ('vitest' or 'jest') with deterministic fallback.

    If preference is specified:
      - Validated against approved JS/TS runners {'vitest', 'jest'}.
      - If preference is unsupported, returns None.
      - If preference is available, returns it.
      - Otherwise, falls back to the other approved runner if available.
    If no preference is specified:
      - Deterministic order: 'vitest', then 'jest'.
    """
    supported = ("vitest", "jest")
    if preference:
        pref = preference.lower()
        if pref not in supported:
            return None
        if find_js_runner_executable(sandbox_path, pref, platform=platform) is not None:
            return pref
        # Deterministic fallback to other supported runner
        fallback = "jest" if pref == "vitest" else "vitest"
        if find_js_runner_executable(sandbox_path, fallback, platform=platform) is not None:
            return fallback
        return None

    for runner in supported:
        if find_js_runner_executable(sandbox_path, runner, platform=platform) is not None:
            return runner
    return None


def resolve_runner_command(
    sandbox_path: Path,
    command: Sequence[str],
    *,
    platform: str = sys.platform,
) -> list[str]:
    """Resolve runner executable in command line in a platform-aware manner.

    - Replaces 'python' with sys.executable.
    - Resolves 'vitest' and 'jest' to platform-appropriate local sandbox binaries
      or system executables.
    - Never selects a Windows .cmd launcher on Linux/POSIX.
    """
    cmd_to_run = list(command)
    if not cmd_to_run:
        return cmd_to_run

    first = cmd_to_run[0]
    if first == "python":
        cmd_to_run[0] = sys.executable
        return cmd_to_run

    # Check if target executable is vitest or jest
    first_norm = first.replace("\\", "/")
    runner_name: str | None = None
    if first in {"vitest", "jest"}:
        runner_name = first
    elif "node_modules/.bin/vitest" in first_norm:
        runner_name = "vitest"
    elif "node_modules/.bin/jest" in first_norm:
        runner_name = "jest"

    if runner_name in {"vitest", "jest"}:
        resolved = find_js_runner_executable(sandbox_path, runner_name, platform=platform)
        if resolved is not None:
            cmd_to_run[0] = str(resolved)
        elif platform == "win32":
            local_cmd = sandbox_path / "node_modules" / ".bin" / f"{runner_name}.cmd"
            local_bin = sandbox_path / "node_modules" / ".bin" / runner_name
            if local_cmd.is_file():
                cmd_to_run[0] = str(local_cmd)
            elif local_bin.is_file():
                cmd_to_run[0] = str(local_bin)
        else:
            # POSIX: Never select .cmd launcher
            local_bin = sandbox_path / "node_modules" / ".bin" / runner_name
            if local_bin.is_file():
                cmd_to_run[0] = str(local_bin)

    return cmd_to_run
