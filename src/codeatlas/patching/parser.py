"""Safe parsing and validation for unified diff text."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Sequence

from .models import PatchFile, PatchHunk

_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$")
_BINARY_MARKERS = ("GIT binary patch", "Binary files ", "GIT binary diff")


def _is_absolute_or_traversal(path_str: str) -> tuple[bool, str | None]:
    """Check if a path string is absolute or attempts path traversal."""
    if "\0" in path_str:
        return True, "Path contains null bytes"

    # Reject backslash absolute or drive letters
    if re.match(r"^[a-zA-Z]:", path_str):
        return True, f"Absolute path detected (drive letter): {path_str}"
    if path_str.startswith("/") or path_str.startswith("\\"):
        return True, f"Absolute path detected: {path_str}"

    # Normalize components
    norm = path_str.replace("\\", "/")
    parts = norm.split("/")
    if any(p == ".." for p in parts):
        return True, f"Path traversal detected: {path_str}"

    return False, None


def _clean_diff_path(raw_path: str) -> str:
    """Clean a/ or b/ prefixes and strip whitespace/quotes."""
    path = raw_path.strip().strip("'\"")
    # Remove tab and timestamp if present (unified diff format: 'file\t2023-01-01 ...')
    if "\t" in path:
        path = path.split("\t", 1)[0]

    norm = path.replace("\\", "/")
    if norm.startswith("a/") or norm.startswith("b/"):
        norm = norm[2:]
    if norm.startswith("./"):
        norm = norm[2:]
    return norm.rstrip("/")


def parse_unified_diff(
    diff_text: str,
    *,
    max_patch_bytes: int = 500_000,
    max_files: int = 5,
    max_changed_lines: int = 150,
) -> tuple[list[PatchFile], list[str]]:
    """Parse unified diff text safely without touching the filesystem.

    Returns:
        tuple of (list of parsed PatchFile objects, list of error strings).
    """
    errors: list[str] = []

    if not diff_text or not diff_text.strip():
        return [], ["Unified diff is empty"]

    if len(diff_text.encode("utf-8")) > max_patch_bytes:
        errors.append(f"Patch exceeds maximum byte size of {max_patch_bytes} bytes")
        return [], errors

    for marker in _BINARY_MARKERS:
        if marker in diff_text:
            return [], [f"Binary patch content is unsupported: found '{marker}'"]

    diff_clean = diff_text.rstrip("\r\n")
    lines = diff_clean.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    files: list[PatchFile] = []

    i = 0
    n = len(lines)

    current_old_path: str | None = None
    current_new_path: str | None = None
    current_op: str = "modify"
    current_hunks: list[PatchHunk] = []
    in_file = False

    def finish_current_file() -> None:
        nonlocal current_old_path, current_new_path, current_op, current_hunks, in_file
        if not in_file:
            return

        final_path = current_new_path or current_old_path
        if not final_path or final_path == "/dev/null":
            final_path = current_old_path

        if not final_path:
            errors.append("File header in diff missing valid path")
            in_file = False
            return

        is_unsafe, reason = _is_absolute_or_traversal(final_path)
        if is_unsafe:
            errors.append(reason or f"Unsafe file path: {final_path}")

        files.append(
            PatchFile(
                path=final_path,
                operation=current_op,
                hunks=list(current_hunks),
                old_path=current_old_path if current_op == "rename" else None,
            )
        )
        current_old_path = None
        current_new_path = None
        current_op = "modify"
        current_hunks = []
        in_file = False

    while i < n:
        line = lines[i]

        # Check file headers
        if line.startswith("--- "):
            finish_current_file()
            in_file = True
            raw = line[4:].strip()
            if raw == "/dev/null":
                current_old_path = "/dev/null"
                current_op = "add"
            else:
                current_old_path = _clean_diff_path(raw)

            # Look ahead for +++
            i += 1
            if i < n and lines[i].startswith("+++ "):
                raw_new = lines[i][4:].strip()
                if raw_new == "/dev/null":
                    current_new_path = "/dev/null"
                    current_op = "delete"
                else:
                    current_new_path = _clean_diff_path(raw_new)
                i += 1
            else:
                errors.append(f"Malformed diff: '---' line without matching '+++' at line {i}")
            continue

        if line.startswith("rename from "):
            finish_current_file()
            in_file = True
            current_op = "rename"
            current_old_path = line[12:].strip()
            i += 1
            if i < n and lines[i].startswith("rename to "):
                current_new_path = lines[i][10:].strip()
                i += 1
            continue

        if line.startswith("@@ "):
            if not in_file:
                # Malformed diff: hunk header without file header
                errors.append(f"Hunk header without file header at line {i + 1}: {line}")
                i += 1
                continue

            match = _HUNK_HEADER_RE.match(line)
            if not match:
                errors.append(f"Malformed hunk header at line {i + 1}: {line}")
                i += 1
                continue

            old_start = int(match.group(1))
            old_lines = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_lines = int(match.group(4)) if match.group(4) is not None else 1
            header_rest = match.group(5)

            hunk_lines: list[str] = []
            actual_old_count = 0
            actual_new_count = 0
            i += 1

            while i < n:
                hline = lines[i]
                if hline.startswith("@@ ") or hline.startswith("--- ") or hline.startswith("diff --git"):
                    break

                if not hline:
                    # In git diff, an empty line in a hunk is treated as a context line with single space stripped
                    if actual_old_count < old_lines or actual_new_count < new_lines:
                        actual_old_count += 1
                        actual_new_count += 1
                        hunk_lines.append(" ")
                        i += 1
                        continue
                    else:
                        break

                prefix = hline[0]
                if prefix == " ":
                    actual_old_count += 1
                    actual_new_count += 1
                    hunk_lines.append(hline)
                elif prefix == "-":
                    actual_old_count += 1
                    hunk_lines.append(hline)
                elif prefix == "+":
                    actual_new_count += 1
                    hunk_lines.append(hline)
                elif prefix == "\\":
                    # '\ No newline at end of file'
                    hunk_lines.append(hline)
                else:
                    errors.append(
                        f"Malformed hunk line at line {i + 1}: expected prefix ' ', '+', '-', or '\\', got '{prefix}'"
                    )
                    break

                i += 1

            # Validate line counts against header
            if actual_old_count != old_lines or actual_new_count != new_lines:
                errors.append(
                    f"Hunk line count mismatch: header expects (-{old_lines}, +{new_lines}) "
                    f"but hunk has (-{actual_old_count}, +{actual_new_count})"
                )

            current_hunks.append(
                PatchHunk(
                    old_start=old_start,
                    old_lines=old_lines,
                    new_start=new_start,
                    new_lines=new_lines,
                    lines=hunk_lines,
                    header=line,
                )
            )
            continue

        i += 1

    finish_current_file()

    if not files and not errors:
        errors.append("No valid file patches found in unified diff")

    # Check overall limits
    total_changed_lines = sum(
        sum(1 for line in hunk.lines if line.startswith(("+", "-")))
        for f in files
        for hunk in f.hunks
    )

    if len(files) > max_files:
        errors.append(f"Patch modifies {len(files)} files, exceeding limit of {max_files}")

    if total_changed_lines > max_changed_lines:
        errors.append(f"Patch changes {total_changed_lines} lines, exceeding limit of {max_changed_lines}")

    return files, errors


def unsafe_diff_path_reason(diff_text: str) -> str | None:
    """Check every diff header path (both sides) for absolute or traversal paths.

    The structural parser validates the effective target path only; this
    extends the same safety rule to old-side headers, which a crafted diff
    could otherwise abuse.
    """
    for match in re.finditer(r"^(?:---|\+\+\+|rename from|rename to)\s+(.+)$", diff_text, re.MULTILINE):
        raw = match.group(1).strip().strip('"').split("\t", 1)[0]
        if raw == "/dev/null":
            continue
        norm = raw.replace("\\", "/")
        if norm.startswith("a/") or norm.startswith("b/"):
            norm = norm[2:]
        if "\0" in norm:
            return "Path contains null bytes"
        if re.match(r"^[a-zA-Z]:", norm):
            return f"Absolute path detected (drive letter): {raw}"
        if norm.startswith("/") or norm.startswith("\\"):
            return f"Absolute path detected: {raw}"
        if any(p == ".." for p in norm.split("/")):
            return f"Path traversal detected: {raw}"
    return None


__all__ = [
    "parse_unified_diff",
    "unsafe_diff_path_reason",
]
