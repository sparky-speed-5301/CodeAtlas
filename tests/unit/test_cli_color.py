from __future__ import annotations

import os

from typer import rich_utils

from codeatlas.cli_color import configure_cli_color


def test_non_tty_disables_ansi_even_when_color_is_forced(monkeypatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("_TYPER_FORCE_DISABLE_TERMINAL", raising=False)
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)

    configure_cli_color(is_terminal=False)

    assert os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] == "1"
    assert rich_utils.FORCE_TERMINAL is False


def test_tty_preserves_typer_color_policy(monkeypatch) -> None:
    monkeypatch.delenv("_TYPER_FORCE_DISABLE_TERMINAL", raising=False)
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)

    configure_cli_color(is_terminal=True)

    assert "_TYPER_FORCE_DISABLE_TERMINAL" not in os.environ
    assert rich_utils.FORCE_TERMINAL is True
