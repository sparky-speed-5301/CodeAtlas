"""Java language adapter with bounded parsing and offline build-tool commands."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from codeatlas.adapters.compiled import CompiledLanguageAdapter
from codeatlas.repository.compiled import parser_available


class JavaAdapter(CompiledLanguageAdapter):
    language = "java"
    version_command = ("javac", "-version")
    test_suffixes = ("Test.java", "Tests.java")

    def tool_availability(self, root: Path | None = None) -> dict[str, bool]:
        javac = shutil.which("javac") is not None
        maven_wrapper = bool(root and (root / "mvnw").is_file())
        gradle_wrapper = bool(root and (root / "gradlew").is_file())
        maven = shutil.which("mvn") is not None or maven_wrapper
        gradle = shutil.which("gradle") is not None or gradle_wrapper
        return {
            "parser": parser_available(self.language),
            "syntax_validation": parser_available(self.language),
            "javac": javac,
            "compiler": javac,
            "type_checker": javac,
            "maven": maven,
            "maven_wrapper": maven_wrapper,
            "gradle": gradle,
            "gradle_wrapper": gradle_wrapper,
            "formatter": shutil.which("google-java-format") is not None,
            "google_java_format": shutil.which("google-java-format") is not None,
            "linter": shutil.which("checkstyle") is not None,
            "checkstyle": shutil.which("checkstyle") is not None,
            "targeted_tests": maven or gradle,
        }

    def test_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        if tools.get("maven"):
            executable = "./mvnw" if tools.get("maven_wrapper") else "mvn"
            return ((executable, "-o", "test"),)
        if tools.get("gradle"):
            executable = "./gradlew" if tools.get("gradle_wrapper") else "gradle"
            return ((executable, "--offline", "test"),)
        return ()

    def check_commands_for_file(self, path: str, tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        if tools.get("google_java_format"):
            return (("google-java-format", "--dry-run", "--set-exit-if-changed", path),)
        return ()


__all__ = ["JavaAdapter"]
