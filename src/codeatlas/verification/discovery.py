"""Targeted test discovery based on changed files, symbols, conventions, and index."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from .models import ResourceLimits, TestPlan
from .runner_selection import find_js_runner
from .command_policy import validate_test_command

_find_js_runner = find_js_runner


def _detect_language(changed_files: Sequence[str]) -> str | None:
    """Infer the primary language from changed files or return None if mixed/unsupported."""
    py_files = [f for f in changed_files if f.endswith(".py")]
    ts_files = [f for f in changed_files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]
    go_files = [f for f in changed_files if f.endswith(".go")]
    java_files = [f for f in changed_files if f.endswith(".java")]
    rust_files = [f for f in changed_files if f.endswith(".rs")]
    compiled = go_files or java_files or rust_files

    if py_files and not ts_files:
        return "python"
    if ts_files and not py_files:
        return "typescript"
    if py_files and ts_files:
        return "mixed"
    if compiled and not (py_files or ts_files) and len({Path(f).suffix.lower() for f in compiled}) == 1:
        suffix = Path(compiled[0]).suffix.lower()
        return {".go": "go", ".java": "java", ".rs": "rust"}.get(suffix, "unsupported")
    if compiled:
        return "mixed"
    return "unsupported"


def _compiled_test_plan(
    sandbox_path: Path,
    changed_files: Sequence[str],
    language: str,
    *,
    full_suite: bool,
    timeout: float,
    runner_preference: str | None,
    policy: dict[str, Any],
) -> tuple[TestPlan, str | None]:
    """Discover Go/Java/Rust tests using fixed, offline command templates."""
    if language == "go":
        patterns = ["**/*_test.go"]
        runner = "go"
        command = ["go", "test", "-mod=readonly", "./..."] if full_suite else None
        installed = shutil.which("go") is not None
    elif language == "java":
        patterns = ["**/*Test.java", "**/*Tests.java", "**/src/test/**/*.java"]
        preferred = (runner_preference or "").lower()
        if preferred not in {"", "maven", "gradle"}:
            return TestPlan(language=language, discovery_reason="Unknown Java test runner", confidence=0.0,
                            limitations=["unknown_runner"], timeout=timeout, full_suite=full_suite), "Unapproved Java test runner"
        maven = (sandbox_path / "pom.xml").is_file() and (shutil.which("mvn") is not None or (sandbox_path / "mvnw").is_file())
        gradle = ((sandbox_path / "build.gradle").is_file() or (sandbox_path / "build.gradle.kts").is_file()) and (
            shutil.which("gradle") is not None or (sandbox_path / "gradlew").is_file()
        )
        if preferred == "maven":
            maven, gradle = True, False
        elif preferred == "gradle":
            maven, gradle = False, True
        if maven:
            executable = "./mvnw" if (sandbox_path / "mvnw").is_file() else "mvn"
            runner, command = "maven", [executable, "-o", "test"]
        elif gradle:
            executable = "./gradlew" if (sandbox_path / "gradlew").is_file() else "gradle"
            runner, command = "gradle", [executable, "--offline", "test"]
        else:
            runner, command = "maven", None
        installed = maven or gradle
    else:
        patterns = ["**/*_test.rs", "tests/**/*.rs"]
        runner, command = "cargo", ["cargo", "test", "--offline"] if full_suite else None
        installed = (sandbox_path / "Cargo.toml").is_file() and shutil.which("cargo") is not None

    found: set[str] = set()
    for pattern in patterns:
        for match in sandbox_path.glob(pattern):
            if match.is_file() and "target" not in match.parts:
                found.add(match.relative_to(sandbox_path).as_posix())
    targets = sorted(found)
    if not full_suite:
        stems = {Path(item).stem.removesuffix("_test") for item in changed_files}
        targets = [item for item in targets if Path(item).stem.removesuffix("_test") in stems or language == "java"]
        if language == "java" and changed_files:
            target_stems = {Path(item).stem for item in changed_files}
            targets = [item for item in targets if any(stem in Path(item).stem for stem in target_stems)]
        if targets and language == "go":
            command = ["go", "test", "-mod=readonly", "./" + str(Path(targets[0]).parent).replace("\\", "/").rstrip(".")]
        elif targets and language == "rust":
            command = ["cargo", "test", "--offline"]

    if not installed:
        return TestPlan(language=language, test_files=targets, test_targets=targets,
                        discovery_reason=f"{language} test toolchain is unavailable",
                        confidence=0.0, limitations=["missing_runner"], timeout=timeout, full_suite=full_suite), \
            f"{language} test toolchain is unavailable; dependency installation is forbidden"
    if not targets and not full_suite:
        return TestPlan(language=language, discovery_reason=f"No reliable targeted {language} tests found",
                        confidence=0.0, limitations=["no_targeted_tests"], timeout=timeout), \
            f"No reliable targeted {language} tests found"
    if command is None:
        return TestPlan(language=language, test_files=targets, test_targets=targets,
                        discovery_reason=f"No {language} test command could be bounded",
                        confidence=0.0, limitations=["no_test_command"], timeout=timeout, full_suite=full_suite), \
            f"No reliable {language} test command found"
    if policy.get("missing_runner") or not validate_test_command(command).allowed:
        return TestPlan(language=language, test_files=targets, test_targets=targets,
                        discovery_reason=f"{runner} command is blocked by the strict allowlist",
                        confidence=0.0, limitations=["blocked_runner"], timeout=timeout, full_suite=full_suite), \
            f"{runner} command is blocked by the strict allowlist"
    return TestPlan(language=language, test_files=targets, test_targets=targets,
                    exact_command=command, discovery_reason=f"Discovered {language} tests with {runner}",
                    confidence=1.0, timeout=timeout, full_suite=full_suite), None


def _is_pytest_available() -> bool:
    """Check whether pytest is importable and available in the current environment."""
    return importlib.util.find_spec("pytest") is not None


def discover_test_plan(
    sandbox_path: Path,
    changed_files: Sequence[str] = (),
    *,
    full_suite: bool = False,
    index: Any = None,
    limits: ResourceLimits | None = None,
    runner_preference: str | None = None,
    policy: dict[str, Any] | None = None,
    platform: str = sys.platform,
) -> tuple[TestPlan, str | None]:
    """Discover targeted or full-suite tests for changed files and symbols.

    Returns (TestPlan, blocked_reason).
    If blocked_reason is not None, test execution must not proceed.
    """
    timeout = limits.timeout_seconds if limits else 30.0
    pol = policy or {}

    if pol.get("network_required"):
        return (
            TestPlan(
                language="python",
                discovery_reason="Test plan requires network access which is strictly disabled",
                confidence=0.0,
                network_policy="required",
                limitations=["network_required_blocked"],
                timeout=timeout,
                full_suite=full_suite,
            ),
            "Network access is required for tests but strictly disabled in Phase 8A/8B",
        )

    if pol.get("dependency_install_required"):
        return (
            TestPlan(
                language="python",
                discovery_reason="Test plan requires dependency installation which is strictly forbidden",
                confidence=0.0,
                dependency_install_policy="required",
                limitations=["dependency_install_blocked"],
                timeout=timeout,
                full_suite=full_suite,
            ),
            "Dependency installation is required for tests but strictly forbidden in Phase 8A/8B",
        )

    custom_cwd = pol.get("working_directory")
    if custom_cwd:
        try:
            resolved_cwd = (sandbox_path / custom_cwd).resolve()
            if not (resolved_cwd == sandbox_path.resolve() or resolved_cwd.is_relative_to(sandbox_path.resolve())):
                return (
                    TestPlan(
                        language="python",
                        discovery_reason=f"Working directory '{custom_cwd}' escapes sandbox",
                        confidence=0.0,
                        limitations=["wrong_working_dir"],
                        timeout=timeout,
                        full_suite=full_suite,
                    ),
                    f"Working directory '{custom_cwd}' escapes sandbox directory",
                )
        except Exception:
            return (
                TestPlan(
                    language="python",
                    discovery_reason=f"Invalid working directory '{custom_cwd}'",
                    confidence=0.0,
                    limitations=["wrong_working_dir"],
                    timeout=timeout,
                    full_suite=full_suite,
                ),
                f"Invalid working directory '{custom_cwd}'",
            )

    lang = pol.get("override_language") or _detect_language(changed_files)

    if pol.get("override_command"):
        return (
            TestPlan(
                language=lang or "python",
                test_files=list(changed_files),
                test_targets=list(changed_files),
                exact_command=pol["override_command"],
                working_directory=pol.get("working_directory", "."),
                discovery_reason="Explicit command override for security validation",
                confidence=1.0,
                timeout=timeout,
                full_suite=full_suite,
            ),
            None,
        )

    if lang in {"unsupported", "mixed"} or lang is None:
        return (
            TestPlan(
                language=lang or "unknown",
                discovery_reason=f"Language '{lang}' is unsupported for automated test execution",
                confidence=0.0,
                limitations=["unsupported_language"],
                timeout=timeout,
                full_suite=full_suite,
            ),
            f"Unsupported or mixed language '{lang}'; no compatible test plan is available",
        )

    if lang in {"go", "java", "rust"}:
        return _compiled_test_plan(
            sandbox_path, changed_files, lang, full_suite=full_suite, timeout=timeout,
            runner_preference=runner_preference, policy=pol,
        )

    # 1. Full-suite test discovery
    if full_suite:
        discovered_test_files: set[str] = set()
        if lang == "python":
            candidate_patterns = [
                "tests/**/test_*.py",
                "tests/**/*_test.py",
                "tests/test_*.py",
                "test/test_*.py",
                "test_*.py",
            ]
            for pat in candidate_patterns:
                for match in sandbox_path.glob(pat):
                    if match.is_file() and "__pycache__" not in match.parts:
                        rel = match.relative_to(sandbox_path).as_posix()
                        discovered_test_files.add(rel)

            test_files_sorted = sorted(discovered_test_files)
            if not test_files_sorted:
                return (
                    TestPlan(
                        language="python",
                        discovery_reason="No test files found in repository for full-suite execution",
                        confidence=0.0,
                        limitations=["no_full_suite_tests"],
                        timeout=timeout,
                        full_suite=True,
                    ),
                    "No test files found in repository for full-suite execution",
                )

            runner_to_use = runner_preference or "pytest"
            if runner_to_use == "pytest":
                if not _is_pytest_available() or pol.get("missing_pytest") or pol.get("missing_runner"):
                    return (
                        TestPlan(
                            language="python",
                            test_files=test_files_sorted,
                            test_targets=test_files_sorted,
                            discovery_reason="pytest requested but not installed in environment",
                            confidence=0.0,
                            limitations=["missing_pytest_dependency"],
                            timeout=timeout,
                            full_suite=True,
                        ),
                        "Runner 'pytest' is not installed in the execution environment; dependency installation is forbidden",
                    )
                cmd = ["python", "-m", "pytest", "-s"]
            elif runner_to_use == "unittest":
                cmd = ["python", "-m", "unittest"]
            else:
                return (
                    TestPlan(
                        language="python",
                        discovery_reason=f"Unknown or unapproved Python runner '{runner_to_use}'",
                        confidence=0.0,
                        limitations=["unknown_runner"],
                        timeout=timeout,
                        full_suite=True,
                    ),
                    f"Unapproved Python runner '{runner_to_use}'",
                )

            return (
                TestPlan(
                    language="python",
                    test_files=test_files_sorted,
                    test_targets=test_files_sorted,
                    exact_command=cmd,
                    discovery_reason=f"Discovered {len(test_files_sorted)} test file(s) for full-suite Python runner '{runner_to_use}'",
                    confidence=1.0,
                    timeout=timeout,
                    full_suite=True,
                ),
                None,
            )

        if lang == "typescript":
            candidate_patterns = [
                "tests/**/*.test.ts",
                "tests/**/*.spec.ts",
                "tests/**/*.test.js",
                "tests/**/*.spec.js",
                "src/**/*.test.ts",
                "src/**/*.spec.ts",
                "__tests__/**/*.test.ts",
                "__tests__/**/*.test.js",
                "*.test.ts",
            ]
            for pat in candidate_patterns:
                for match in sandbox_path.glob(pat):
                    if match.is_file() and "node_modules" not in match.parts:
                        rel = match.relative_to(sandbox_path).as_posix()
                        discovered_test_files.add(rel)

            test_files_sorted = sorted(discovered_test_files)
            if not test_files_sorted:
                return (
                    TestPlan(
                        language="typescript",
                        discovery_reason="No test files found in repository for full-suite execution",
                        confidence=0.0,
                        limitations=["no_full_suite_tests"],
                        timeout=timeout,
                        full_suite=True,
                    ),
                    "No test files found in repository for full-suite execution",
                )

            if runner_preference and runner_preference.lower() not in {"vitest", "jest"}:
                return (
                    TestPlan(
                        language="typescript",
                        test_files=test_files_sorted,
                        test_targets=test_files_sorted,
                        discovery_reason=f"Unknown or unapproved JS/TS runner '{runner_preference}'",
                        confidence=0.0,
                        limitations=["unknown_runner"],
                        timeout=timeout,
                        full_suite=True,
                    ),
                    f"Unapproved JS/TS runner '{runner_preference}'",
                )

            js_runner = find_js_runner(sandbox_path, preference=runner_preference, platform=platform)
            if not js_runner or pol.get("missing_runner"):
                return (
                    TestPlan(
                        language="typescript",
                        test_files=test_files_sorted,
                        test_targets=test_files_sorted,
                        discovery_reason="Neither vitest nor jest is installed; npm install is forbidden",
                        confidence=0.0,
                        limitations=["missing_js_runner"],
                        timeout=timeout,
                        full_suite=True,
                    ),
                    "JavaScript/TypeScript test runner ('vitest' or 'jest') is not installed. Package installation is strictly forbidden.",
                )

            cmd = ["vitest", "run"] if js_runner == "vitest" else ["jest"]
            return (
                TestPlan(
                    language="typescript",
                    test_files=test_files_sorted,
                    test_targets=test_files_sorted,
                    exact_command=cmd,
                    discovery_reason=f"Discovered {len(test_files_sorted)} test file(s) for full-suite JS/TS runner '{js_runner}'",
                    confidence=1.0,
                    timeout=timeout,
                    full_suite=True,
                ),
                None,
            )

    # 2. Targeted test discovery
    targeted_test_files: set[str] = set()

    if lang == "python":
        for cf in changed_files:
            p = Path(cf)
            stem = p.stem  # e.g. "calc" from "src/calc.py"
            candidate_patterns = [
                f"tests/**/test_{stem}.py",
                f"tests/**/{stem}_test.py",
                f"tests/test_{stem}.py",
                f"test_{stem}.py",
                f"test/test_{stem}.py",
            ]
            for pat in candidate_patterns:
                for match in sandbox_path.glob(pat):
                    if match.is_file():
                        rel = match.relative_to(sandbox_path).as_posix()
                        targeted_test_files.add(rel)

        # Consult index if available to find referencing tests
        if index is not None and hasattr(index, "imports"):
            for cf in changed_files:
                stem = Path(cf).stem
                for imp in getattr(index, "imports", []):
                    source_file = getattr(imp, "file", "")
                    if "test" in source_file.lower() and stem in getattr(imp, "module", ""):
                        if (sandbox_path / source_file).is_file():
                            targeted_test_files.add(source_file)

        test_files_sorted = sorted(targeted_test_files)
        if not test_files_sorted:
            return (
                TestPlan(
                    language="python",
                    discovery_reason="No reliable targeted tests found matching changed Python files",
                    confidence=0.0,
                    limitations=["no_targeted_tests"],
                    timeout=timeout,
                ),
                "No reliable targeted test files found in repository for changed files",
            )

        # Check runner availability
        runner_to_use = runner_preference or "pytest"
        if runner_to_use == "pytest":
            if not _is_pytest_available() or pol.get("missing_pytest") or pol.get("missing_runner"):
                return (
                    TestPlan(
                        language="python",
                        test_files=test_files_sorted,
                        test_targets=test_files_sorted,
                        discovery_reason="pytest requested but not installed in environment",
                        confidence=0.0,
                        limitations=["missing_pytest_dependency"],
                        timeout=timeout,
                    ),
                    "Runner 'pytest' is not installed in the execution environment; dependency installation is forbidden",
                )
            cmd = ["python", "-m", "pytest", "-s", *test_files_sorted]
        elif runner_to_use == "unittest":
            cmd = ["python", "-m", "unittest", *test_files_sorted]
        else:
            return (
                TestPlan(
                    language="python",
                    discovery_reason=f"Unknown or unapproved Python runner '{runner_to_use}'",
                    confidence=0.0,
                    limitations=["unknown_runner"],
                    timeout=timeout,
                ),
                f"Unapproved Python runner '{runner_to_use}'",
            )

        return (
            TestPlan(
                language="python",
                test_files=test_files_sorted,
                test_targets=test_files_sorted,
                exact_command=cmd,
                discovery_reason=f"Discovered {len(test_files_sorted)} targeted test file(s) via file naming conventions",
                confidence=1.0,
                timeout=timeout,
            ),
            None,
        )

    if lang == "typescript":
        for cf in changed_files:
            p = Path(cf)
            stem = p.stem  # e.g. "math" from "src/math.ts"
            candidate_patterns = [
                f"tests/**/{stem}.test.ts",
                f"tests/**/{stem}.spec.ts",
                f"tests/**/{stem}.test.js",
                f"tests/**/{stem}.spec.js",
                f"src/**/{stem}.test.ts",
                f"src/**/{stem}.spec.ts",
                f"__tests__/**/{stem}.test.ts",
                f"__tests__/**/{stem}.test.js",
                f"{stem}.test.ts",
            ]
            for pat in candidate_patterns:
                for match in sandbox_path.glob(pat):
                    if match.is_file():
                        rel = match.relative_to(sandbox_path).as_posix()
                        targeted_test_files.add(rel)

        test_files_sorted = sorted(targeted_test_files)
        if not test_files_sorted:
            return (
                TestPlan(
                    language="typescript",
                    discovery_reason="No reliable targeted tests found matching changed JS/TS files",
                    confidence=0.0,
                    limitations=["no_targeted_tests"],
                    timeout=timeout,
                ),
                "No reliable targeted test files found in repository for changed files",
            )

        if runner_preference and runner_preference.lower() not in {"vitest", "jest"}:
            return (
                TestPlan(
                    language="typescript",
                    test_files=test_files_sorted,
                    test_targets=test_files_sorted,
                    discovery_reason=f"Unknown or unapproved JS/TS runner '{runner_preference}'",
                    confidence=0.0,
                    limitations=["unknown_runner"],
                    timeout=timeout,
                    full_suite=False,
                ),
                f"Unapproved JS/TS runner '{runner_preference}'",
            )

        js_runner = find_js_runner(sandbox_path, preference=runner_preference, platform=platform)
        if not js_runner or pol.get("missing_runner"):
            return (
                TestPlan(
                    language="typescript",
                    test_files=test_files_sorted,
                    test_targets=test_files_sorted,
                    discovery_reason="Neither vitest nor jest is installed; npm install is forbidden",
                    confidence=0.0,
                    limitations=["missing_js_runner"],
                    timeout=timeout,
                ),
                "JavaScript/TypeScript test runner ('vitest' or 'jest') is not installed. Package installation is strictly forbidden.",
            )

        cmd = [js_runner, *test_files_sorted]
        return (
            TestPlan(
                language="typescript",
                test_files=test_files_sorted,
                test_targets=test_files_sorted,
                exact_command=cmd,
                discovery_reason=f"Discovered {len(test_files_sorted)} targeted test file(s) for JS/TS runner '{js_runner}'",
                confidence=1.0,
                timeout=timeout,
            ),
            None,
        )

    return (
        TestPlan(
            language=lang,
            discovery_reason="Unsupported test runner environment",
            confidence=0.0,
            timeout=timeout,
            full_suite=full_suite,
        ),
        "Unsupported test discovery mode",
    )


__all__ = [
    "discover_test_plan",
]
