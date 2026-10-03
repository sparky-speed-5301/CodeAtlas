"""Unit tests for Phase 8A: test runner, command validator, discovery, and output redaction."""

from __future__ import annotations

from pathlib import Path
import pytest

from codeatlas.verification.command_policy import validate_test_command
from codeatlas.verification.discovery import discover_test_plan
from codeatlas.verification.models import ResourceLimits, TestPlan, TestResult
from codeatlas.verification.redaction import audit_and_redact_results, redact_test_output
from codeatlas.verification.runner import (
    JestTestRunner,
    PytestTestRunner,
    TestRunner,
    UnittestTestRunner,
    VitestTestRunner,
    get_test_runner,
)
from codeatlas.verification.runner_selection import (
    find_js_runner,
    find_js_runner_executable,
    is_file_executable,
    resolve_runner_command,
    set_simulated_executable,
)


def test_command_allowlist_allowed() -> None:
    # Python pytest
    val = validate_test_command(["python", "-m", "pytest", "tests/test_foo.py"])
    assert val.allowed is True

    # Python unittest
    val = validate_test_command(["python", "-m", "unittest", "discover", "-s", "tests"])
    assert val.allowed is True

    # Direct pytest
    val = validate_test_command(["pytest", "tests/test_foo.py"])
    assert val.allowed is True

    # Vitest
    val = validate_test_command(["vitest", "tests/foo.test.ts"])
    assert val.allowed is True

    # Jest
    val = validate_test_command(["jest", "tests/foo.test.js"])
    assert val.allowed is True


def test_command_allowlist_rejected() -> None:
    # Shell operators
    assert validate_test_command(["pytest", "tests/test_foo.py", "&&", "whoami"]).allowed is False
    assert validate_test_command(["pytest", ";", "rm", "-rf"]).allowed is False
    assert validate_test_command(["pytest", "|", "cat"]).allowed is False
    assert validate_test_command(["pytest", ">", "out.txt"]).allowed is False

    # Command substitution
    assert validate_test_command(["pytest", "$(whoami)"]).allowed is False
    assert validate_test_command(["pytest", "`whoami`"]).allowed is False

    # Package managers / installs
    assert validate_test_command(["pip", "install", "requests"]).allowed is False
    assert validate_test_command(["npm", "install", "lodash"]).allowed is False
    assert validate_test_command(["yarn", "install"]).allowed is False
    assert validate_test_command(["pnpm", "install"]).allowed is False

    # Arbitrary / dangerous commands
    assert validate_test_command(["curl", "https://example.com"]).allowed is False
    assert validate_test_command(["bash", "script.sh"]).allowed is False
    assert validate_test_command(["powershell", "-c", "dir"]).allowed is False
    assert validate_test_command(["sh", "-c", "echo 1"]).allowed is False

    # Path traversal
    assert validate_test_command(["pytest", "../../outside_test.py"]).allowed is False


def test_redact_test_output() -> None:
    text = (
        "Secret AWS key: AKIAIOSFODNN7EXAMPLE\n"
        "Secret GitHub token: ghp_111122223333444455556666777788889999\n"
        "Approval token: CAT-APP-abcdef0123456789\n"
        "Normal test output line\n"
    )
    redacted, audit = redact_test_output(text, extra_tokens=["CAT-APP-abcdef0123456789"])
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "ghp_111122223333444455556666777788889999" not in redacted
    assert "[REDACTED]" in redacted
    assert "Normal test output line" in redacted
    assert audit.raw_value_matches >= 3


def test_discover_test_plan_python(tmp_path: Path) -> None:
    src_file = tmp_path / "src" / "calc.py"
    src_file.parent.mkdir(parents=True)
    src_file.write_text("def add(a, b): return a + b\n")

    test_file = tmp_path / "tests" / "test_calc.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("from src.calc import add\ndef test_add(): assert add(1, 2) == 3\n")

    plan, block_reason = discover_test_plan(tmp_path, ["src/calc.py"])
    assert block_reason is None
    assert plan.language == "python"
    assert "tests/test_calc.py" in plan.test_targets


def test_discover_test_plan_blocked_policies(tmp_path: Path) -> None:
    # Network required
    plan, block = discover_test_plan(tmp_path, ["src/calc.py"], policy={"network_required": True})
    assert block is not None
    assert "network" in block.lower()

    # Dependency install required
    plan, block = discover_test_plan(tmp_path, ["src/calc.py"], policy={"dependency_install_required": True})
    assert block is not None
    assert "dependency" in block.lower()

    # Working directory escaping sandbox
    plan, block = discover_test_plan(tmp_path, ["src/calc.py"], policy={"working_directory": "../../escape"})
    assert block is not None
    assert "escapes" in block.lower()


def test_runner_protocol_implementations(tmp_path: Path) -> None:
    pytest_runner = PytestTestRunner()
    assert isinstance(pytest_runner, TestRunner)
    assert pytest_runner.name == "pytest"
    assert "python" in pytest_runner.supported_languages

    unittest_runner = UnittestTestRunner()
    assert isinstance(unittest_runner, TestRunner)
    assert unittest_runner.name == "unittest"

    vitest_runner = VitestTestRunner()
    assert isinstance(vitest_runner, TestRunner)
    assert vitest_runner.name == "vitest"

    jest_runner = JestTestRunner()
    assert isinstance(jest_runner, TestRunner)
    assert jest_runner.name == "jest"

    assert isinstance(get_test_runner("python"), PytestTestRunner)
    assert isinstance(get_test_runner("typescript"), VitestTestRunner)
    assert get_test_runner("rust") is None


def test_runner_selection_linux_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    posix_vitest = bin_dir / "vitest"
    posix_vitest.write_text("#!/usr/bin/env python\nprint('posix')\n")
    posix_vitest.chmod(0o755)

    cmd_vitest = bin_dir / "vitest.cmd"
    cmd_vitest.write_text("@echo off\n")

    # On POSIX (platform='linux'), must select POSIX executable, NEVER .cmd launcher
    resolved = find_js_runner_executable(tmp_path, "vitest", platform="linux")
    assert resolved == posix_vitest
    assert resolved != cmd_vitest

    cmd_resolved = resolve_runner_command(tmp_path, ["vitest", "run"], platform="linux")
    assert cmd_resolved[0] == str(posix_vitest)
    assert not cmd_resolved[0].endswith(".cmd")

    # Validation on POSIX rejects .cmd launchers
    assert validate_test_command(["node_modules/.bin/vitest.cmd", "tests/foo.ts"], platform="linux").allowed is False


def test_runner_selection_windows_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    posix_vitest = bin_dir / "vitest"
    posix_vitest.write_text("#!/usr/bin/env python\n")
    cmd_vitest = bin_dir / "vitest.cmd"
    cmd_vitest.write_text("@echo off\n")

    # On Windows, may select .cmd launcher
    resolved = find_js_runner_executable(tmp_path, "vitest", platform="win32")
    assert resolved == cmd_vitest

    cmd_resolved = resolve_runner_command(tmp_path, ["vitest", "run"], platform="win32")
    assert cmd_resolved[0] == str(cmd_vitest)

    # Validation on Windows allows .cmd launcher
    assert validate_test_command(["node_modules/.bin/vitest.cmd", "tests/foo.ts"], platform="win32").allowed is True


def test_runner_missing_executable_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    posix_vitest = bin_dir / "vitest"
    posix_vitest.write_text("#!/usr/bin/env python\n")
    posix_vitest.chmod(0o644)  # lacks execute permission
    set_simulated_executable(posix_vitest, False)
    try:
        # Should not be executable on POSIX
        assert is_file_executable(posix_vitest, platform="linux") is False

        # Runner is not executable, and no system fallback exists
        resolved = find_js_runner_executable(tmp_path, "vitest", platform="linux")
        assert resolved is None

        # Test plan discovery fails with missing runner
        src = tmp_path / "src" / "calc.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function add() {}\n")
        test = tmp_path / "tests" / "calc.test.ts"
        test.parent.mkdir(parents=True)
        test.write_text("// test\n")

        plan, blocked = discover_test_plan(tmp_path, ["src/calc.ts"], platform="linux")
        assert blocked is not None
        assert "missing_js_runner" in plan.limitations
    finally:
        set_simulated_executable(None)


def test_runner_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    # Neither vitest nor jest installed in sandbox
    src = tmp_path / "src" / "calc.ts"
    src.parent.mkdir(parents=True)
    src.write_text("export function add() {}\n")
    test = tmp_path / "tests" / "calc.test.ts"
    test.parent.mkdir(parents=True)
    test.write_text("// test\n")

    assert find_js_runner(tmp_path, platform="linux") is None
    assert find_js_runner(tmp_path, platform="win32") is None

    plan, blocked = discover_test_plan(tmp_path, ["src/calc.ts"], platform="linux")
    assert blocked is not None
    assert "missing_js_runner" in plan.limitations

    plan_win, blocked_win = discover_test_plan(tmp_path, ["src/calc.ts"], platform="win32")
    assert blocked_win is not None
    assert "missing_js_runner" in plan_win.limitations


def test_runner_deterministic_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)

    jest_bin = bin_dir / "jest"
    jest_bin.write_text("#!/usr/bin/env python\n")
    jest_bin.chmod(0o755)

    # Vitest is not available, but jest is:
    # 1. No preference -> falls back to jest
    assert find_js_runner(tmp_path, platform="linux") == "jest"
    # 2. Vitest preferred -> falls back to jest
    assert find_js_runner(tmp_path, preference="vitest", platform="linux") == "jest"

    # Now add vitest
    vitest_bin = bin_dir / "vitest"
    vitest_bin.write_text("#!/usr/bin/env python\n")
    vitest_bin.chmod(0o755)

    # Default order prefers vitest
    assert find_js_runner(tmp_path, platform="linux") == "vitest"
    # Preference jest selects jest
    assert find_js_runner(tmp_path, preference="jest", platform="linux") == "jest"


def test_unsupported_runner_rejection(tmp_path: Path) -> None:
    src = tmp_path / "src" / "calc.ts"
    src.parent.mkdir(parents=True)
    src.write_text("export function add() {}\n")
    test = tmp_path / "tests" / "calc.test.ts"
    test.parent.mkdir(parents=True)
    test.write_text("// test\n")

    # Unsupported runner rejected by discovery
    plan, blocked = discover_test_plan(tmp_path, ["src/calc.ts"], runner_preference="mocha")
    assert blocked is not None
    assert "unapproved" in blocked.lower()
    assert "unknown_runner" in plan.limitations

    plan, blocked = discover_test_plan(tmp_path, ["src/calc.ts"], runner_preference="karma")
    assert blocked is not None
    assert "unknown_runner" in plan.limitations


def test_phase8a_safety_behavior(tmp_path: Path) -> None:
    # Metacharacters rejected
    for op in ["&", "|", ";", "$", "`", "<", ">"]:
        val = validate_test_command(["pytest", op])
        assert val.allowed is False

    # Prohibited commands rejected
    for prog in ["curl", "wget", "bash", "sh", "powershell", "pip", "npm"]:
        val = validate_test_command([prog, "arg"])
        assert val.allowed is False

    # Redaction active
    redacted, audit = redact_test_output("Simulated AKIAIOSFODNN7EXAMPLE key")
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert audit.raw_value_matches >= 1
