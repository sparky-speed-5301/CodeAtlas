"""Strict test command validation and allowlist enforcement."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any, Sequence

from .models import CommandValidation

# Shell metacharacters and control operators strictly rejected
_DISALLOWED_CHARS = set("&|;><$`(){}[]\n\r*?~")

# Prohibited binaries / utilities
_PROHIBITED_COMMANDS = {
    "curl", "wget", "powershell", "pwsh", "bash", "sh", "cmd", "zsh", "sudo",
    "pip", "pip3", "npm", "pnpm", "yarn", "node", "python3", "npx",
}

# Recognized allowlist runners / prefixes
_ALLOWED_PREFIXES: list[list[str]] = [
    ["python", "-m", "pytest"],
    ["python", "-m", "unittest"],
    ["pytest"],
    ["vitest"],
    ["jest"],
    ["node_modules/.bin/vitest"],
    ["node_modules/.bin/jest"],
    ["./node_modules/.bin/vitest"],
    ["./node_modules/.bin/jest"],
]


def validate_test_command(
    command: Sequence[str] | str,
    *,
    sandbox_path: Path | None = None,
    allowed_targets: Sequence[str] | None = None,
    allow_empty_targets: bool = False,
    policy: dict[str, Any] | None = None,
    platform: str = sys.platform,
) -> CommandValidation:
    """Validate a command array against strict Phase 8A allowlist rules.

    Rejects:
    - Shell operators, redirects, subshells, substitutions.
    - Prohibited binaries (curl, wget, shells, package managers).
    - Executables not on the strict allowlist.
    - Path traversal or targets outside sandbox_path.
    """
    rules_evaluated: list[str] = ["strict_allowlist_check"]

    # Convert string to list if necessary, rejecting shell operators first
    if isinstance(command, str):
        for ch in _DISALLOWED_CHARS:
            if ch in command:
                return CommandValidation(
                    allowed=False,
                    rejection_reason=f"Command contains prohibited shell character '{ch}'",
                    rules_evaluated=rules_evaluated + ["shell_operator_check"],
                )
        try:
            parts = shlex.split(command, posix=False)
        except Exception as err:
            return CommandValidation(
                allowed=False,
                rejection_reason=f"Failed to parse command line: {err}",
                rules_evaluated=rules_evaluated,
            )
    else:
        parts = list(command)

    if not parts:
        return CommandValidation(
            allowed=False,
            rejection_reason="Empty command is not allowed",
            rules_evaluated=rules_evaluated,
        )

    # Check each argument for disallowed characters
    for arg in parts:
        for ch in _DISALLOWED_CHARS:
            if ch in arg:
                return CommandValidation(
                    allowed=False,
                    rejection_reason=f"Command argument '{arg}' contains prohibited character '{ch}'",
                    rules_evaluated=rules_evaluated + ["shell_operator_check"],
                )

    # Check for prohibited command patterns
    cmd_lower = [p.lower() for p in parts]
    first_raw = Path(cmd_lower[0]).name.lower()
    if platform != "win32" and first_raw.endswith((".cmd", ".bat")):
        return CommandValidation(
            allowed=False,
            rejection_reason=f"Windows launcher '{first_raw}' is not allowed on POSIX",
            rules_evaluated=rules_evaluated + ["platform_launcher_check"],
        )

    first_arg = first_raw
    if first_arg.endswith((".exe", ".cmd", ".bat")):
        first_arg = first_arg.rsplit(".", 1)[0]

    if first_arg in _PROHIBITED_COMMANDS:
        # Check if it's package install
        if first_arg in {"pip", "npm", "pnpm", "yarn"}:
            return CommandValidation(
                allowed=False,
                rejection_reason=f"Dependency installation command '{first_arg}' is strictly forbidden in Phase 8A",
                rules_evaluated=rules_evaluated + ["dependency_install_check"],
            )
        return CommandValidation(
            allowed=False,
            rejection_reason=f"Execution of prohibited executable '{first_arg}' is forbidden",
            rules_evaluated=rules_evaluated + ["prohibited_binary_check"],
        )

    # Check package manager invocations in subsequent arguments
    if any(p in {"install", "add", "update", "i"} for p in cmd_lower[1:]):
        if any(p in {"npm", "pnpm", "yarn", "pip"} for p in cmd_lower):
            return CommandValidation(
                allowed=False,
                rejection_reason="Package installation or dependency modification is strictly forbidden",
                rules_evaluated=rules_evaluated + ["dependency_install_check"],
            )

    # Normalize executable: allow sys.executable or python
    matched_prefix: list[str] | None = None
    # Normalize 'python.exe' to 'python'
    norm_parts = list(parts)
    first_clean = Path(norm_parts[0]).name.lower()
    if first_clean in {"python", "python.exe", "python3", "python3.exe"}:
        norm_parts[0] = "python"
    elif "node_modules/.bin/vitest" in norm_parts[0].replace("\\", "/"):
        norm_parts[0] = "vitest"
    elif "node_modules/.bin/jest" in norm_parts[0].replace("\\", "/"):
        norm_parts[0] = "jest"

    for prefix in _ALLOWED_PREFIXES:
        if len(norm_parts) >= len(prefix):
            if norm_parts[:len(prefix)] == prefix:
                matched_prefix = prefix
                break

    if not matched_prefix:
        return CommandValidation(
            allowed=False,
            rejection_reason=(
                f"Command '{' '.join(parts[:3])}' does not match any approved test runner in the strict allowlist"
            ),
            rules_evaluated=rules_evaluated + ["allowlist_prefix_check"],
        )

    # Target arguments verification
    target_args = norm_parts[len(matched_prefix):]
    rules_evaluated.append("target_path_safety_check")

    for arg in target_args:
        # Allow safe flags like -v, -q, -k, --verbose
        if arg.startswith("-"):
            if arg in {"-c", "-e", "--import-mode", "-o", "--output"}:
                return CommandValidation(
                    allowed=False,
                    rejection_reason=f"Prohibited test runner flag '{arg}'",
                    rules_evaluated=rules_evaluated,
                )
            continue

        # Check for path traversal or absolute paths
        if ".." in arg.replace("\\", "/").split("/"):
            return CommandValidation(
                allowed=False,
                rejection_reason=f"Path traversal detected in test target: '{arg}'",
                rules_evaluated=rules_evaluated,
            )
        if Path(arg).is_absolute():
            return CommandValidation(
                allowed=False,
                rejection_reason=f"Absolute path test target is forbidden: '{arg}'",
                rules_evaluated=rules_evaluated,
            )

        # If sandbox_path is provided, verify target resolves inside sandbox
        if sandbox_path is not None:
            resolved_target = (sandbox_path / arg).resolve()
            try:
                resolved_target.relative_to(sandbox_path.resolve())
            except ValueError:
                return CommandValidation(
                    allowed=False,
                    rejection_reason=f"Test target '{arg}' resolves outside the sandbox root",
                    rules_evaluated=rules_evaluated,
                )

    return CommandValidation(
        allowed=True,
        normalized_command=parts,
        executable=matched_prefix[0],
        rules_evaluated=rules_evaluated,
    )


__all__ = [
    "validate_test_command",
]
