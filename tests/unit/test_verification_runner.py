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
