"""Shared grammar-based adapter behavior for compiled languages."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Sequence

from codeatlas.adapters.base import BaseLanguageAdapter, CapabilityStatus, LanguageParseResult
from codeatlas.findings.models import Finding
from codeatlas.repository.compiled import parse_source, parser_available
from codeatlas.verification.command_policy import validate_check_command, validate_test_command


def safe_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return bool(normalized and len(normalized) <= 512 and not normalized.startswith(("/", "."))
                and ":" not in normalized and all(part not in {"", ".", ".."} for part in normalized.split("/")))


class CompiledLanguageAdapter(BaseLanguageAdapter):
    """Reuse the repair contract; parser support never implies a compiler pass."""

    test_suffixes: tuple[str, ...] = ()
    capability_limitations: tuple[str, ...] = (
        "Grammar parsing checks syntax only; compiler/type checks and repository tests have not run.",
        "Dependency installation is forbidden; test planning requires an installed offline toolchain.",
    )

    def classify_fixability(self, finding: Finding) -> Literal["fix_eligible", "review_only"]:
        if not parser_available(self.language) or self.is_protected(finding.file):
            return "review_only"
        return super().classify_fixability(finding)

    def parse_modified_file(self, path: str, source: str) -> LanguageParseResult:
        if not self.detect_language(path):
            return LanguageParseResult(syntax_status="unsupported", limitations=("Language mismatch",))
        result = parse_source(path, source, self.language)
        status: CapabilityStatus = "passed" if result.valid else "failed" if result.valid is False else (
            "unavailable" if result.reason == "parser_unavailable" else "blocked"
        )
        return LanguageParseResult(
            symbols=result.symbols, syntax_valid=result.valid, syntax_status=status,
            errors=result.errors, limitations=self.capability_limitations + ((result.reason,) if result.reason else ()),
        )

    def discover_targeted_tests(
        self, path: str, available: Sequence[str], consulted: Sequence[str] = (),
    ) -> tuple[str, ...]:
        if not safe_relative_path(path) or not self.detect_language(path):
            return ()
        target = PurePosixPath(path)
        names = {f"{target.stem}{suffix}" for suffix in self.test_suffixes}
        selected = set()
        for test in available[:64]:
            if not safe_relative_path(test) or not self.detect_language(test):
                continue
            candidate = PurePosixPath(test)
            if self.language == "go":
                matched = candidate.parent == target.parent and candidate.name.endswith("_test.go")
            elif self.language == "rust":
                matched = candidate.parent == PurePosixPath("tests") and candidate.name in names
            else:
                matched = candidate.name in names or (test in consulted[:64] and candidate.name.endswith(("Test.java", "Tests.java")))
            if matched:
                selected.add(test)
        return tuple(sorted(selected)[:8])

    def test_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        raise NotImplementedError

    def check_commands_for_file(self, path: str, tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        return ()

    def check_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        if not tests or len(tests) > 8 or any(not safe_relative_path(t) or not self.detect_language(t) for t in tests):
            return ()
        return tuple(c for c in self.test_commands(tests, tools) if validate_test_command(c).allowed)

    def allowlisted_check_commands(self, path: str, tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        if not safe_relative_path(path) or not self.detect_language(path) or self.is_protected(path):
            return ()
        return tuple(c for c in self.check_commands_for_file(path, tools) if validate_check_command(c).allowed)
