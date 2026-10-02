"""Generate Phase 8B-2 selective full-suite execution fixtures under eval/cases/full-suite/.

Each fixture contains:
  - metadata.json: scenario, token, limits, expected statuses, policy opt-in
  - base/: git tree committed as base commit
  - proposal.json: PatchProposal with base_commit placeholder
"""

from __future__ import annotations

import json
from pathlib import Path

from codeatlas.patching.proposal import create_patch_proposal

CASES_DIR = Path(__file__).resolve().parent / "cases" / "full-suite"


def ctx(text: str) -> str:
    return " " + text


def _save_case(
    case_name: str,
    metadata: dict,
    base_files: dict[str, str],
    proposal_diff: str,
    target_files: list[str],
    *,
    finding_id: str = "F-TEST-8B2",
    rationale: str = "Phase 8B-2 full-suite evaluation fixture",
    expected_behavior: str = "Full-suite evaluation check",
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
    calc_py = "def add(a, b):\n    return a + b\n"
    test_calc_py = "from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    test_suite_py = "def test_suite_extra():\n    assert 1 + 1 == 2\n"

    diff_pass = "\n".join([
        "--- a/src/calc.py",
        "+++ b/src/calc.py",
        "@@ -1,2 +1,3 @@",
        ctx("def add(a, b):"),
        "+    # audited safe add",
        ctx("    return a + b"),
    ]) + "\n"

    # 1. Disabled by default
    _save_case(
        "case-01-disabled-by-default",
        {
            "case_id": "case-01-disabled-by-default",
            "scenario": "disabled_by_default",
            "token": "valid",
            "run_full_suite": False,
            "expected": {
                "exit": 0,
                "status": "requires_human_approval",
                "full_suite_requested": False,
                "full_suite_status": "not_run",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 2. Flag without policy opt-in
    _save_case(
        "case-02-flag-without-policy-optin",
        {
            "case_id": "case-02-flag-without-policy-optin",
            "scenario": "flag_without_policy_optin",
            "token": "valid",
            "run_full_suite": True,
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "full_suite_requested": True,
                "full_suite_policy_opted_in": False,
                "full_suite_status": "blocked",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 3. Policy opt-in without approval
    _save_case(
        "case-03-policy-optin-without-approval",
        {
            "case_id": "case-03-policy-optin-without-approval",
            "scenario": "policy_optin_without_approval",
            "token": "missing",
            "run_full_suite": True,
            "test_config": {"allow_full_suite": True},
            "expected": {
                "exit": 1,
                "status": "requires_human_approval",
                "approval_verified": False,
                "full_suite_requested": True,
                "full_suite_policy_opted_in": True,
                "full_suite_status": "blocked",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 4. Valid full-suite execution
    _save_case(
        "case-04-valid-full-suite",
        {
            "case_id": "case-04-valid-full-suite",
            "scenario": "valid_full_suite",
            "token": "valid",
            "run_full_suite": True,
            "test_config": {"allow_full_suite": True},
            "expected": {
                "exit": 0,
                "status": "validated",
                "full_suite_requested": True,
                "full_suite_policy_opted_in": True,
                "full_suite_status": "passed",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py, "tests/test_extra.py": test_suite_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 5. Unsupported language
    rs_file = "fn main() {}\n"
    diff_rs = "\n".join([
        "--- a/src/main.rs",
        "+++ b/src/main.rs",
        "@@ -1 +1,2 @@",
        ctx("fn main() {}"),
        "+// audited comment",
    ]) + "\n"
    _save_case(
        "case-05-unsupported-language",
        {
            "case_id": "case-05-unsupported-language",
            "scenario": "unsupported_language",
            "token": "valid",
            "run_full_suite": True,
            "test_config": {"allow_full_suite": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "full_suite_requested": True,
                "full_suite_status": "blocked",
            },
        },
        {"src/main.rs": rs_file},
        diff_rs,
        ["src/main.rs"],
    )

    # 6. Missing runner
    _save_case(
        "case-06-missing-runner",
        {
            "case_id": "case-06-missing-runner",
            "scenario": "missing_runner",
            "token": "valid",
            "run_full_suite": True,
            "test_config": {"allow_full_suite": True, "missing_runner": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "full_suite_requested": True,
                "full_suite_status": "blocked",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 7. Dependency install command
    _save_case(
        "case-07-dependency-install-cmd",
        {
            "case_id": "case-07-dependency-install-cmd",
            "scenario": "dependency_install_cmd",
            "token": "valid",
            "run_full_suite": True,
            "test_config": {"allow_full_suite": True, "override_command": ["pip", "install", "requests"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "full_suite_requested": True,
                "full_suite_status": "blocked",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 8. Shell command
    _save_case(
        "case-08-shell-cmd",
        {
            "case_id": "case-08-shell-cmd",
            "scenario": "shell_cmd",
            "token": "valid",
            "run_full_suite": True,
            "test_config": {"allow_full_suite": True, "override_command": ["bash", "-c", "echo 1"]},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "full_suite_requested": True,
                "full_suite_status": "blocked",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 9. Timeout
    hang_test = "import time\ndef test_sleep():\n    time.sleep(2.0)\n"
    _save_case(
        "case-09-timeout",
        {
            "case_id": "case-09-timeout",
            "scenario": "timeout",
            "token": "valid",
            "run_full_suite": True,
            "test_timeout": 0.3,
            "test_config": {"allow_full_suite": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "full_suite_requested": True,
                "full_suite_status": "timed_out",
            },
        },
        {"src/calc.py": calc_py, "tests/test_hang.py": hang_test},
        diff_pass,
        ["src/calc.py"],
    )

    # 10. Excessive output
    noisy_test = "def test_noisy():\n    print('A' * 20000)\n"
    _save_case(
        "case-10-excessive-output",
        {
            "case_id": "case-10-excessive-output",
            "scenario": "excessive_output",
            "token": "valid",
            "run_full_suite": True,
            "max_output_bytes": 500,
            "test_config": {"allow_full_suite": True},
            "expected": {
                "exit": 0,
                "status": "validated",
                "full_suite_requested": True,
                "full_suite_status": "passed",
                "output_truncated": True,
            },
        },
        {"src/calc.py": calc_py, "tests/test_noisy.py": noisy_test},
        diff_pass,
        ["src/calc.py"],
    )

    # 11. Network required
    _save_case(
        "case-11-network-required",
        {
            "case_id": "case-11-network-required",
            "scenario": "network_required",
            "token": "valid",
            "run_full_suite": True,
            "test_config": {"allow_full_suite": True, "network_required": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "full_suite_requested": True,
                "full_suite_status": "blocked",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py},
        diff_pass,
        ["src/calc.py"],
    )

    # 12. Redacted diagnostics
    leak_test = "def test_leak():\n    token = 'ghp_' + '111122223333444455556666777788889999'\n    raise ValueError(f'Leaked: {token}')\n"
    _save_case(
        "case-12-redacted-diagnostics",
        {
            "case_id": "case-12-redacted-diagnostics",
            "scenario": "redacted_diagnostics",
            "token": "valid",
            "run_full_suite": True,
            "test_config": {"allow_full_suite": True},
            "expected": {
                "exit": 1,
                "status": "failed_validation",
                "full_suite_requested": True,
                "full_suite_status": "failed",
                "redaction_verified": True,
            },
        },
        {"src/calc.py": calc_py, "tests/test_leak.py": leak_test},
        diff_pass,
        ["src/calc.py"],
    )

    # 13. Phase 8A targeted test regression
    _save_case(
        "case-13-targeted-regression",
        {
            "case_id": "case-13-targeted-regression",
            "scenario": "targeted_regression",
            "token": "valid",
            "run_tests": True,
            "run_full_suite": False,
            "expected": {
                "exit": 0,
                "status": "validated",
                "tests_status": "passed",
                "full_suite_requested": False,
                "full_suite_status": "not_run",
            },
        },
        {"src/calc.py": calc_py, "tests/test_calc.py": test_calc_py, "tests/test_extra.py": test_suite_py},
        diff_pass,
        ["src/calc.py"],
    )


if __name__ == "__main__":
    generate_all()
    print(f"Generated Phase 8B-2 cases under {CASES_DIR}")
