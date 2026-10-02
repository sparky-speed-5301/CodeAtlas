"""Generate Phase 8A test-execution fixtures under eval/cases/test-execution/.

Each fixture contains:
  - metadata.json: scenario, token, limits, expected statuses and invariants
  - base/: git tree committed as base commit
  - proposal.json: PatchProposal with base_commit placeholder
"""

from __future__ import annotations

import json
from pathlib import Path

from codeatlas.patching.proposal import create_patch_proposal

CASES_DIR = Path(__file__).resolve().parent / "cases" / "test-execution"


def ctx(text: str) -> str:
    return " " + text


def _save_case(
    case_name: str,
    metadata: dict,
    base_files: dict[str, str],
    proposal_diff: str,
    target_files: list[str],
    *,
    finding_id: str = "F-TEST-8A",
    rationale: str = "Test execution evaluation fixture",
    expected_behavior: str = "Tests executed safely",
    initial_status: str = "requires_human_approval",
) -> None:
    case_dir = CASES_DIR / case_name
    case_dir.mkdir(parents=True, exist_ok=True)

    base_dir = case_dir / "base"
    base_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in base_files.items():
        fp = base_dir / rel_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8", newline="\n")

    prop = create_patch_proposal(
        finding_id=finding_id,
        provider_name="operator",
        provider_version="1.0.0",
        base_commit="BASE_SHA",
        target_files=target_files,
        unified_diff=proposal_diff,
        rationale=rationale,
        expected_behavior=expected_behavior,
    )
    prop.status = initial_status
    (case_dir / "proposal.json").write_text(prop.model_dump_json(indent=2), encoding="utf-8")
    (case_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def generate_all() -> None:
    # 1. Python targeted test passes
    calc_py = "def add(a, b):\n    return a + b\n"
    test_calc_py = "from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    diff_pass = "\n".join([
        "--- a/src/calc.py",
        "+++ b/src/calc.py",
        "@@ -1,2 +1,3 @@",
        ctx("def add(a, b):"),
        "+    # audited add",
        ctx("    return a + b"),
    ]) + "\n"
    _save_case(
        "case-01-python-targeted-pass",
        {
            "case_id": "case-01-python-targeted-pass",
            "scenario": "python_targeted_pass",
            "group": "execution",
            "token": "valid",
            "expected": {
                "exit": 0,
                "status": "validated",
                "tests_status": "passed",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 2. Python targeted test fails
    diff_fail = "\n".join([
        "--- a/src/calc.py",
        "+++ b/src/calc.py",
        "@@ -1,2 +1,2 @@",
        ctx("def add(a, b):"),
        "-    return a + b",
        "+    return a - b",
    ]) + "\n"
    _save_case(
        "case-02-python-targeted-fail",
        {
            "case_id": "case-02-python-targeted-fail",
            "scenario": "python_targeted_fail",
            "group": "execution",
            "token": "valid",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "failed",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_fail,
        ["src/calc.py"],
    )

    # 3. Python syntax passes but test fails
    diff_syntax_pass_test_fail = "\n".join([
        "--- a/src/calc.py",
        "+++ b/src/calc.py",
        "@@ -1,2 +1,2 @@",
        ctx("def add(a, b):"),
        "-    return a + b",
        "+    return 0",
    ]) + "\n"
    _save_case(
        "case-03-python-syntax-pass-test-fail",
        {
            "case_id": "case-03-python-syntax-pass-test-fail",
            "scenario": "python_syntax_pass_test_fail",
            "group": "execution",
            "token": "valid",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "failed",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_syntax_pass_test_fail,
        ["src/calc.py"],
    )

    # 4. JS/TS test passes when runner is installed
    calc_ts = "export function multiply(a: number, b: number): number {\n  return a * b;\n}\n"
    calc_test_ts = "import { multiply } from '../src/calc';\n// test multiply\n"
    # Create fake vitest executable in node_modules/.bin
    vitest_script = "#!/usr/bin/env python\nimport sys\nprint('Tests passed (1/1)')\nsys.exit(0)\n"
    vitest_cmd = "@echo off\r\npython \"%~dp0\\vitest\" %*\r\n"
    diff_ts_pass = "\n".join([
        "--- a/src/calc.ts",
        "+++ b/src/calc.ts",
        "@@ -1,3 +1,4 @@",
        ctx("export function multiply(a: number, b: number): number {"),
        "+  // safe multiply",
        ctx("  return a * b;"),
        ctx("}"),
    ]) + "\n"
    _save_case(
        "case-04-ts-test-pass",
        {
            "case_id": "case-04-ts-test-pass",
            "scenario": "ts_test_pass",
            "group": "execution",
            "token": "valid",
            "expected": {
                "exit": 0,
                "status": "validated",
                "tests_status": "passed",
                "syntax_valid": None,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
            },
        },
        {
            "src/calc.ts": calc_ts,
            "tests/calc.test.ts": calc_test_ts,
            "node_modules/.bin/vitest": vitest_script,
            "node_modules/.bin/vitest.cmd": vitest_cmd,
        },
        diff_ts_pass,
        ["src/calc.ts"],
    )

    # 5. Multiple targeted tests
    user_py = "def format_user(name):\n    return f'User: {name}'\n"
    test_user_1 = "from src.user import format_user\ndef test_fmt():\n    assert format_user('Alice') == 'User: Alice'\n"
    test_user_2 = "from src.user import format_user\ndef test_empty():\n    assert format_user('') == 'User: '\n"
    diff_user = "\n".join([
        "--- a/src/user.py",
        "+++ b/src/user.py",
        "@@ -1,2 +1,3 @@",
        ctx("def format_user(name):"),
        "+    # format user note",
        ctx("    return f'User: {name}'"),
    ]) + "\n"
    _save_case(
        "case-05-multiple-targeted-tests",
        {
            "case_id": "case-05-multiple-targeted-tests",
            "scenario": "multiple_targeted_tests",
            "group": "execution",
            "token": "valid",
            "expected": {
                "exit": 0,
                "status": "validated",
                "tests_status": "passed",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
            },
        },
        {"src/user.py": user_py, "tests/test_user.py": test_user_1, "tests/user_test.py": test_user_2},
        diff_user,
        ["src/user.py"],
    )

    # 6. Test output requiring redaction
    auth_py = "def check():\n    return True\n"
    test_auth_py = (
        "import sys\n"
        "from src.auth import check\n"
        "def test_auth():\n"
        "    print('Simulated token: AKIAIOSFODNN7EXAMPLE')\n"
        "    assert check() is True\n"
    )
    diff_auth = "\n".join([
        "--- a/src/auth.py",
        "+++ b/src/auth.py",
        "@@ -1,2 +1,3 @@",
        ctx("def check():"),
        "+    # secure check",
        ctx("    return True"),
    ]) + "\n"
    _save_case(
        "case-06-output-redaction",
        {
            "case_id": "case-06-output-redaction",
            "scenario": "output_redaction",
            "group": "safety",
            "token": "valid",
            "expected": {
                "exit": 0,
                "status": "validated",
                "tests_status": "passed",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
                "redaction_applied": True,
            },
        },
        {"src/auth.py": auth_py, "tests/test_auth.py": test_auth_py},
        diff_auth,
        ["src/auth.py"],
    )

    # 7. Missing pytest
    _save_case(
        "case-07-missing-pytest",
        {
            "case_id": "case-07-missing-pytest",
            "scenario": "missing_pytest",
            "group": "execution",
            "token": "valid",
            "test_config": {"missing_pytest": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 8. Unsupported language
    rust_src = "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
    rust_diff = "\n".join([
        "--- a/src/main.rs",
        "+++ b/src/main.rs",
        "@@ -1,3 +1,4 @@",
        ctx("pub fn add(a: i32, b: i32) -> i32 {"),
        "+    // rust add",
        ctx("    a + b"),
        ctx("}"),
    ]) + "\n"
    _save_case(
        "case-08-unsupported-language",
        {
            "case_id": "case-08-unsupported-language",
            "scenario": "unsupported_language",
            "group": "execution",
            "token": "valid",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": None,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/main.rs": rust_src},
        rust_diff,
        ["src/main.rs"],
    )

    # 9. No reliable test target
    lonely_py = "def alone():\n    return 1\n"
    diff_lonely = "\n".join([
        "--- a/src/lonely.py",
        "+++ b/src/lonely.py",
        "@@ -1,2 +1,3 @@",
        ctx("def alone():"),
        "+    # alone",
        ctx("    return 1"),
    ]) + "\n"
    _save_case(
        "case-09-no-reliable-target",
        {
            "case_id": "case-09-no-reliable-target",
            "scenario": "no_reliable_target",
            "group": "execution",
            "token": "valid",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/lonely.py": lonely_py},
        diff_lonely,
        ["src/lonely.py"],
    )

    # 10. Network-required test
    _save_case(
        "case-10-network-required",
        {
            "case_id": "case-10-network-required",
            "scenario": "network_required",
            "group": "safety",
            "token": "valid",
            "test_config": {"network_required": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 11. Dependency-install requirement
    _save_case(
        "case-11-dependency-install-required",
        {
            "case_id": "case-11-dependency-install-required",
            "scenario": "dependency_install_required",
            "group": "safety",
            "token": "valid",
            "test_config": {"dependency_install_required": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 12. Shell operator
    _save_case(
        "case-12-shell-operator",
        {
            "case_id": "case-12-shell-operator",
            "scenario": "shell_operator",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["pytest", "tests/test_calc.py", "&&", "whoami"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 13. Command substitution
    _save_case(
        "case-13-command-substitution",
        {
            "case_id": "case-13-command-substitution",
            "scenario": "command_substitution",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["pytest", "$(whoami)"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 14. Package install command
    _save_case(
        "case-14-package-install-cmd",
        {
            "case_id": "case-14-package-install-cmd",
            "scenario": "package_install_cmd",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["pip", "install", "requests"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 15. Arbitrary script command
    _save_case(
        "case-15-arbitrary-script-cmd",
        {
            "case_id": "case-15-arbitrary-script-cmd",
            "scenario": "arbitrary_script_cmd",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["bash", "run_tests.sh"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 16. CI-derived command
    _save_case(
        "case-16-ci-derived-cmd",
        {
            "case_id": "case-16-ci-derived-cmd",
            "scenario": "ci_derived_cmd",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["make", "ci-test"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 17. README-derived command
    _save_case(
        "case-17-readme-derived-cmd",
        {
            "case_id": "case-17-readme-derived-cmd",
            "scenario": "readme_derived_cmd",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["powershell", "-c", "Invoke-Tests"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 18. Test timeout
    sleep_test = "import time\ndef test_sleep():\n    time.sleep(5)\n"
    _save_case(
        "case-18-test-timeout",
        {
            "case_id": "case-18-test-timeout",
            "scenario": "test_timeout",
            "group": "execution",
            "token": "valid",
            "test_timeout": 0.5,
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "timed_out",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": sleep_test},
        diff_pass,
        ["src/calc.py"],
    )

    # 19. Excessive output
    verbose_test = "def test_verbose():\n    for i in range(500):\n        print(f'Line {i}: ' + 'X' * 50)\n    assert True\n"
    _save_case(
        "case-19-excessive-output",
        {
            "case_id": "case-19-excessive-output",
            "scenario": "excessive_output",
            "group": "execution",
            "token": "valid",
            "max_output_bytes": 1000,
            "expected": {
                "exit": 0,
                "status": "validated",
                "tests_status": "passed",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": verbose_test},
        diff_pass,
        ["src/calc.py"],
    )

    # 20. Process-limit violation
    _save_case(
        "case-20-process-limit-violation",
        {
            "case_id": "case-20-process-limit-violation",
            "scenario": "process_limit_violation",
            "group": "safety",
            "token": "valid",
            "test_config": {"max_processes": 0},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 21. Memory-limit violation
    _save_case(
        "case-21-memory-limit-violation",
        {
            "case_id": "case-21-memory-limit-violation",
            "scenario": "memory_limit_violation",
            "group": "safety",
            "token": "valid",
            "test_config": {"max_memory_bytes": 10},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 22. Wrong working directory
    _save_case(
        "case-22-wrong-working-dir",
        {
            "case_id": "case-22-wrong-working-dir",
            "scenario": "wrong_working_dir",
            "group": "safety",
            "token": "valid",
            "test_config": {"working_directory": "../../outside"},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 23. Test target outside sandbox
    _save_case(
        "case-23-target-outside-sandbox",
        {
            "case_id": "case-23-target-outside-sandbox",
            "scenario": "target_outside_sandbox",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["pytest", "../../tests/test_calc.py"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 24. Original-worktree execution attempt
    _save_case(
        "case-24-original-worktree-attempt",
        {
            "case_id": "case-24-original-worktree-attempt",
            "scenario": "original_worktree_attempt",
            "group": "safety",
            "token": "valid",
            "test_config": {"target_original_worktree": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 25. Missing approval
    _save_case(
        "case-25-missing-approval",
        {
            "case_id": "case-25-missing-approval",
            "scenario": "missing_approval",
            "group": "approval",
            "token": "missing",
            "expected": {
                "exit": 1,
                "status": "requires_human_approval",
                "tests_status": "not_run",
                "syntax_valid": None,
                "applies_cleanly": False,
                "approval_verified": False,
                "execution_allowed": False,
                "cleanup_status": "not_applicable",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 26. Invalid approval token
    _save_case(
        "case-26-invalid-approval-token",
        {
            "case_id": "case-26-invalid-approval-token",
            "scenario": "invalid_approval_token",
            "group": "approval",
            "token": "malformed",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "not_run",
                "syntax_valid": None,
                "applies_cleanly": False,
                "approval_verified": False,
                "execution_allowed": False,
                "cleanup_status": "not_applicable",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 27. Stale proposal
    _save_case(
        "case-27-stale-proposal",
        {
            "case_id": "case-27-stale-proposal",
            "scenario": "stale_proposal",
            "group": "approval",
            "token": "valid",
            "base_commit": "stale",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "not_run",
                "syntax_valid": None,
                "applies_cleanly": False,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "not_applicable",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 28. Rejected proposal
    _save_case(
        "case-28-rejected-proposal",
        {
            "case_id": "case-28-rejected-proposal",
            "scenario": "rejected_proposal",
            "group": "approval",
            "token": "valid",
            "expected": {
                "exit": 1,
                "status": "rejected",
                "tests_status": "not_run",
                "syntax_valid": None,
                "applies_cleanly": False,
                "approval_verified": False,
                "execution_allowed": False,
                "cleanup_status": "not_applicable",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
        initial_status="rejected",
    )

    # 29. Syntax-invalid patch
    bad_syntax_diff = "\n".join([
        "--- a/src/calc.py",
        "+++ b/src/calc.py",
        "@@ -1,2 +1,3 @@",
        ctx("def add(a, b):"),
        "+    def bad(:",
        ctx("    return a + b"),
    ]) + "\n"
    _save_case(
        "case-29-syntax-invalid-patch",
        {
            "case_id": "case-29-syntax-invalid-patch",
            "scenario": "syntax_invalid_patch",
            "group": "validation",
            "token": "valid",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "not_run",
                "syntax_valid": False,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        bad_syntax_diff,
        ["src/calc.py"],
    )

    # 30. Patch conflict
    conflict_diff = "\n".join([
        "--- a/src/calc.py",
        "+++ b/src/calc.py",
        "@@ -999,2 +999,1 @@",
        "-nonexistent_one()",
        "-nonexistent_two()",
        "+replaced()",
    ]) + "\n"
    _save_case(
        "case-30-patch-conflict",
        {
            "case_id": "case-30-patch-conflict",
            "scenario": "patch_conflict",
            "group": "validation",
            "token": "valid",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "not_run",
                "syntax_valid": None,
                "applies_cleanly": False,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        conflict_diff,
        ["src/calc.py"],
    )

    # 31. Secret in test output
    test_secret_out = (
        "def test_token_echo():\n"
        "    print('Secret token: ghp_111122223333444455556666777788889999')\n"
        "    assert True\n"
    )
    _save_case(
        "case-31-secret-in-test-output",
        {
            "case_id": "case-31-secret-in-test-output",
            "scenario": "secret_in_test_output",
            "group": "safety",
            "token": "valid",
            "expected": {
                "exit": 0,
                "status": "validated",
                "tests_status": "passed",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
                "redaction_applied": True,
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_secret_out},
        diff_pass,
        ["src/calc.py"],
    )

    # 32. Cleanup failure
    _save_case(
        "case-32-cleanup-failure",
        {
            "case_id": "case-32-cleanup-failure",
            "scenario": "cleanup_failure",
            "group": "isolation",
            "token": "valid",
            "retain_sandbox_on_failure": True,
            "test_config": {"simulate_cleanup_failure": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "not_run",
                "syntax_valid": None,
                "applies_cleanly": False,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "failed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        conflict_diff,
        ["src/calc.py"],
    )

    # 33. Test command changes repository
    dirtying_test = (
        "def test_mutates():\n"
        "    with open('unexpected_mutation.txt', 'w') as f:\n"
        "        f.write('untracked file')\n"
        "    assert True\n"
    )
    _save_case(
        "case-33-test-changes-repo",
        {
            "case_id": "case-33-test-changes-repo",
            "scenario": "test_changes_repo",
            "group": "safety",
            "token": "valid",
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "error",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": True,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": dirtying_test},
        diff_pass,
        ["src/calc.py"],
    )

    # 34. Test command attempts network access
    _save_case(
        "case-34-test-network-access",
        {
            "case_id": "case-34-test-network-access",
            "scenario": "test_network_access",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["curl", "https://example.com"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 35. Test command attempts dependency installation
    _save_case(
        "case-35-test-dep-install",
        {
            "case_id": "case-35-test-dep-install",
            "scenario": "test_dep_install",
            "group": "safety",
            "token": "valid",
            "test_config": {"override_command": ["npm", "install", "lodash"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "tests_status": "blocked",
                "syntax_valid": True,
                "applies_cleanly": True,
                "approval_verified": True,
                "execution_allowed": False,
                "cleanup_status": "completed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )


if __name__ == "__main__":
    generate_all()
    print(f"Generated Phase 8A test execution cases in {CASES_DIR}")
