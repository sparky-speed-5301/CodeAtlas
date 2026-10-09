"""Rust language adapter with bounded static parsing and offline Cargo checks."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from codeatlas.adapters.compiled import CompiledLanguageAdapter
from codeatlas.repository.compiled import parser_available


class RustAdapter(CompiledLanguageAdapter):
    language = "rust"
    version_command = ("rustc", "--version")
    test_suffixes = ("_test.rs",)

    def tool_availability(self, root: Path | None = None) -> dict[str, bool]:
        rustc = shutil.which("rustc") is not None
        cargo = shutil.which("cargo") is not None
        rustfmt = shutil.which("rustfmt") is not None
        clippy = shutil.which("cargo-clippy") is not None or cargo
        return {
            "parser": parser_available(self.language),
            "syntax_validation": parser_available(self.language),
            "rustc": rustc,
            "compiler": rustc,
            "type_checker": rustc,
            "cargo": cargo,
            "rustfmt": rustfmt,
            "formatter": rustfmt,
            "clippy": clippy,
            "linter": clippy,
            "targeted_tests": cargo,
        }

    def test_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        if not tools.get("cargo"):
            return ()
        return (("cargo", "test", "--offline"),)

    def check_commands_for_file(self, path: str, tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        commands: list[tuple[str, ...]] = []
        if tools.get("rustfmt"):
            commands.append(("rustfmt", "--check", path))
        if tools.get("cargo"):
            commands.append(("cargo", "check", "--offline"))
        return tuple(commands[:4])


__all__ = ["RustAdapter"]
