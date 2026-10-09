"""Non-executing language adapter contract for bounded repair planning."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Sequence

from codeatlas.core.language import detect_language
from codeatlas.findings.models import Finding
from codeatlas.patching.models import PatchFile
from codeatlas.patching.policy import is_dependency_manifest_path, is_lockfile_path, is_workflow_path
from codeatlas.repository.files import is_configuration_file, is_generated_file, is_test_file
from codeatlas.repository.models import Symbol
from codeatlas.verification.command_policy import validate_check_command, validate_test_command
from codeatlas.verification.discovery import _is_pytest_available
from codeatlas.verification.runner_selection import find_js_runner


CapabilityStatus = Literal[
    "available",
    "unavailable",
    "unsupported",
    "not_run",
    "passed",
    "failed",
    "blocked",
]


@dataclass(frozen=True)
class LanguageCapabilityReport:
    """Typed, non-executing capability information for one language adapter."""

    language: str
    language_version: str | None
    parser: CapabilityStatus
    syntax_validation: CapabilityStatus
    formatter: CapabilityStatus
    linter: CapabilityStatus
    type_checker: CapabilityStatus
    test_runner: CapabilityStatus
    protected_generated_files: CapabilityStatus = "available"
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class LanguageParseResult:
    symbols: tuple[Symbol, ...] = ()
    syntax_valid: bool | None = None
    syntax_status: CapabilityStatus = "not_run"
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class LanguageAdapter(Protocol):
    """Adapters inspect data and plan allowlisted checks; they never execute them."""

    language: str

    def detect_language(self, path: str) -> bool: ...
    def language_version(self, root: Path | None = None) -> str | None: ...
    def capability_report(self, root: Path | None = None) -> LanguageCapabilityReport: ...
    def tool_availability(self, root: Path | None = None) -> dict[str, bool]: ...
    def parse_modified_file(self, path: str, source: str) -> LanguageParseResult: ...
    def classify_fixability(self, finding: Finding) -> Literal["fix_eligible", "review_only"]: ...
    def is_protected(self, path: str, source: str = "") -> bool: ...
    def check_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]: ...
    def discover_targeted_tests(self, path: str, available: Sequence[str], consulted: Sequence[str] = ()) -> tuple[str, ...]: ...
    def validate_patch_constraints(self, file: PatchFile, source: str) -> tuple[str, ...]: ...

    # Descriptive aliases are part of the common language-independent surface.
    def report_tool_availability(self, root: Path | None = None) -> dict[str, bool]: ...
    def syntax_check(self, path: str, source: str) -> LanguageParseResult: ...
    def identify_protected_generated_files(self, path: str, source: str = "") -> bool: ...
    def produce_allowlisted_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]: ...
    def allowlisted_check_commands(self, path: str, tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]: ...


class BaseLanguageAdapter:
    """Shared classification uses the existing repository and command policies."""

    language = "unknown"

    def parse_modified_file(self, path: str, source: str) -> LanguageParseResult:
        raise NotImplementedError(f"No parser implementation for {self.language}: {path}")

    def language_version(self, root: Path | None = None) -> str | None:
        """Return a bounded version string from a known tool, when available.

        Adapters never inspect project scripts or execute repository code. The
        command is a fixed language-tool version probe and has a short timeout.
        Subclasses provide ``version_command`` where this is supported.
        """
        command = getattr(self, "version_command", None)
        if not command or not shutil.which(command[0]):
            return None
        try:
            result = subprocess.run(
                list(command), cwd=str(root) if root else None, capture_output=True,
                text=True, check=False, timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = (result.stdout or result.stderr).strip().splitlines()
        return output[0][:128] if result.returncode == 0 and output else None

    @staticmethod
    def _status(enabled: bool) -> CapabilityStatus:
        return "available" if enabled else "unavailable"

    def capability_report(self, root: Path | None = None) -> LanguageCapabilityReport:
        tools = self.report_tool_availability(root)
        return LanguageCapabilityReport(
            language=self.language,
            language_version=self.language_version(root),
            parser=self._status(bool(tools.get("parser") or tools.get("ast") or tools.get("static_symbol_parser"))),
            syntax_validation=self._status(bool(tools.get("syntax_validation", True))),
            formatter=self._status(bool(tools.get("formatter"))),
            linter=self._status(bool(tools.get("linter") or tools.get("ruff") or tools.get("eslint") or tools.get("golangci_lint"))),
            type_checker=self._status(bool(tools.get("type_checker") or tools.get("compiler"))),
            test_runner=self._status(bool(tools.get("targeted_tests") or tools.get("pytest") or tools.get("vitest")
                                          or tools.get("jest") or tools.get("go_test") or tools.get("maven")
                                          or tools.get("gradle") or tools.get("cargo"))),
            limitations=tuple(getattr(self, "capability_limitations", ())),
        )

    def detect_language(self, path: str) -> bool:
        return detect_language(path).value == self.language

    def detected_language(self, path: str) -> str:
        return detect_language(path).value

    def classify_fixability(self, finding: Finding) -> Literal["fix_eligible", "review_only"]:
        return "review_only" if finding.fixability in {"unknown", "not_fixable", "validated"} else "fix_eligible"

    def is_protected(self, path: str, source: str = "") -> bool:
        parts = path.replace("\\", "/").lower().split("/")
        return (
            any(part in {".git", "node_modules", "vendor", "dist", "build", "target", ".gradle", "__pycache__", ".venv"} for part in parts)
            or path.lower().endswith((".d.ts", ".d.tsx"))
            or is_generated_file(path, source)
            or is_test_file(path)
            or is_configuration_file(path)
            or is_workflow_path(path)
            or is_dependency_manifest_path(path)
            or is_lockfile_path(path)
        )

    def tool_availability(self, root: Path | None = None) -> dict[str, bool]:
        if self.language == "python":
            return {
                "ast": True, "pytest": _is_pytest_available(), "ruff": shutil.which("ruff") is not None,
                "black": shutil.which("black") is not None,
                "formatter": shutil.which("ruff") is not None or shutil.which("black") is not None,
            }
        runner = find_js_runner(root) if root is not None else None
        return {"static_symbol_parser": True, "compiler": shutil.which("tsc") is not None,
                "eslint": shutil.which("eslint") is not None, "prettier": shutil.which("prettier") is not None,
                "formatter": shutil.which("prettier") is not None,
                "vitest": runner == "vitest", "jest": runner == "jest"}

    def report_tool_availability(self, root: Path | None = None) -> dict[str, bool]:
        return self.tool_availability(root)

    def syntax_check(self, path: str, source: str) -> LanguageParseResult:
        return self.parse_modified_file(path, source)

    def identify_protected_generated_files(self, path: str, source: str = "") -> bool:
        return self.is_protected(path, source)

    def discover_targeted_tests(self, path: str, available: Sequence[str], consulted: Sequence[str] = ()) -> tuple[str, ...]:
        """Select from already available packet paths; never walk the repository."""
        stem = Path(path).stem
        names = {f"test_{stem}.py", f"{stem}_test.py"} if self.language == "python" else {
            f"{stem}.{kind}.{ext}" for kind in ("test", "spec") for ext in ("js", "jsx", "ts", "tsx")
        }
        selected = set()
        for test in available[:64]:
            test_language = detect_language(test).value
            compatible = test_language == "python" if self.language == "python" else test_language in {"javascript", "typescript"}
            if (compatible and len(test) <= 512 and not test.startswith("-") and "\\" not in test and ":" not in test
                    and all(p not in {"", ".", ".."} for p in test.split("/")) and is_test_file(test)
                    and (Path(test).name in names or test in consulted)
                    and validate_test_command(["pytest", test]).allowed):
                selected.add(test)
        return tuple(sorted(selected)[:8])

    def check_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        """Plan targeted tests through the existing test command allowlist."""
        if not tests:
            return ()
        if any(test.startswith("-") or "\\" in test or ":" in test
               or not is_test_file(test) or not validate_test_command(["pytest", test]).allowed for test in tests[:8]):
            return ()
        if self.language == "python" and tools.get("pytest"):
            command = ("python", "-m", "pytest", *tests[:8])
        elif tools.get("vitest"):
            command = ("vitest", "run", *tests[:8])
        elif tools.get("jest"):
            command = ("jest", *tests[:8])
        else:
            return ()
        return (command,) if validate_test_command(command).allowed else ()

    def produce_allowlisted_commands(self, tests: Sequence[str], tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        return self.check_commands(tests, tools)

    def allowlisted_check_commands(self, path: str, tools: dict[str, bool]) -> tuple[tuple[str, ...], ...]:
        """Return command data only; the repair layer never executes it."""
        commands: list[tuple[str, ...]] = []
        if self.language == "python":
            if tools.get("ruff"):
                commands.extend([("ruff", "check", "--no-fix", path), ("ruff", "format", "--check", path)])
            elif tools.get("black"):
                commands.append(("black", "--check", path))
        elif self.language == "typescript":
            if tools.get("compiler"):
                commands.append(("tsc", "--noEmit", "--pretty", "false", path))
            if tools.get("prettier"):
                commands.append(("prettier", "--check", path))
        elif self.language == "javascript":
            if tools.get("eslint"):
                commands.append(("eslint", path))
            if tools.get("prettier"):
                commands.append(("prettier", "--check", path))
        return tuple(command for command in commands[:4] if validate_check_command(command).allowed)

    def validate_patch_constraints(self, file: PatchFile, source: str) -> tuple[str, ...]:
        if file.operation != "modify" or not file.hunks:
            return ("Repair requires a nonempty modification of an existing source file",)
        if not self.detect_language(file.path) or self.is_protected(file.path, source):
            return ("Repair target is unsupported, protected, or generated",)
        if not any(line.startswith(("+", "-")) for h in file.hunks for line in h.lines):
            return ("Repair patch has no changed lines",)
        return ()
