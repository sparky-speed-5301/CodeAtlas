"""Terminal color policy for deterministic CLI output."""

from __future__ import annotations

import os
import sys


def configure_cli_color(*, is_terminal: bool | None = None) -> None:
    """Disable Typer ANSI styling for redirected stdout while preserving TTY color."""
    terminal = sys.stdout.isatty() if is_terminal is None else is_terminal
    if terminal:
        return
    os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
    from typer import rich_utils

    rich_utils.FORCE_TERMINAL = False


__all__ = ["configure_cli_color"]
