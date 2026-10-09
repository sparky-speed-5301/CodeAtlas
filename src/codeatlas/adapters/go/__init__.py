"""Go language adapter with bounded static parsing and offline test commands."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from codeatlas.adapters.compiled import CompiledLanguageAdapter
from codeatlas.repository.compiled import parser_available


class GoAdapter(CompiledLanguageAdapter):
    language = "go"
    version_command = ("go", "version")
    test_suffixes = ("_test.go",)

    def tool_availability(self, root: Path | None = None) -> dict[str, bool]:
        go = shutil.which("go") is not None
        gofmt = shutil.which("gofmt") is not None
        return {
            "parser": parser_available(self.language),
            "syntax_validation": parser_available(self.language),
            "go": go,
            "compiler": go,
            "type_checker": go,
            "gofmt": gofmt,
            "formatter": gofmt,
            "golangci_lint": shutil.which("golangci-lint") is not None,
            "staticcheck": shutil.which("staticcheck") is not None,
            "linter": shutil.which("golangci-lint") is not None or shutil.which("staticcheck") is not None,
            "go_test": go,
            "targeted_tests": go,
        }

    def test_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        if not tools.get("go_test"):
            return ()
        packages = tuple(
            package if package != "./" else "."
            for package in sorted({"./" + (str(Path(test).parent).replace("\\", "/").rstrip(".")) for test in tests})
        )
        return (("go", "test", "-mod=readonly", *packages),)

    def check_commands_for_file(self, path: str, tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        commands: list[tuple[str, ...]] = []
        if tools.get("gofmt"):
            commands.append(("gofmt", "-d", path))
        if tools.get("staticcheck"):
            commands.append(("staticcheck", path))
        return tuple(commands[:4])


__all__ = ["GoAdapter"]
