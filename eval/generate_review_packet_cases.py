"""Generator for Phase 5 review-packet evaluation cases."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

BASE_DIR = Path("eval/cases/review-packets")
if BASE_DIR.exists():
    shutil.rmtree(BASE_DIR)
BASE_DIR.mkdir(parents=True, exist_ok=True)

CASES = [
    # 1. small changed function
    {
        "case_id": "case-01-small-changed-func",
        "scenario": "small_changed_function",
        "description": "Small function modification in clean repository",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {"src/math_util.py": "def add(a: int, b: int) -> int:\n    return a + b\n"},
        "after": {"src/math_util.py": "def add(a: int, b: int) -> int:\n    return a + b + 0\n"},
    },
    # 2. changed function with related test
    {
        "case_id": "case-02-changed-func-test",
        "scenario": "changed_function_with_test",
        "description": "Modified function with indexed unit test",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {
            "src/calc.py": "def compute(x: int) -> int:\n    return x * 2\n",
            "tests/test_calc.py": "from src.calc import compute\ndef test_compute():\n    assert compute(2) == 4\n",
        },
        "after": {
            "src/calc.py": "def compute(x: int) -> int:\n    return x * 4\n",
            "tests/test_calc.py": "from src.calc import compute\ndef test_compute():\n    assert compute(2) == 4\n",
        },
    },
    # 3. changed function with caller
    {
        "case_id": "case-03-changed-func-caller",
        "scenario": "changed_function_with_caller",
        "description": "Changed callee function referenced by caller module",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {
            "src/helper.py": "def format_text(s: str) -> str:\n    return s.strip()\n",
            "src/caller.py": "from src.helper import format_text\ndef run(val: str):\n    return format_text(val)\n",
        },
        "after": {
            "src/helper.py": "def format_text(s: str) -> str:\n    return s.strip().lower()\n",
            "src/caller.py": "from src.helper import format_text\ndef run(val: str):\n    return format_text(val)\n",
        },
    },
    # 4. changed function with callee
    {
        "case_id": "case-04-changed-func-callee",
        "scenario": "changed_function_with_callee",
        "description": "Changed caller invoking downstream database callee",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {
            "src/db.py": "def query(sql: str):\n    return []\n",
            "src/main.py": "from src.db import query\ndef handle():\n    return query('SELECT 1')\n",
        },
        "after": {
            "src/db.py": "def query(sql: str):\n    return []\n",
            "src/main.py": "from src.db import query\ndef handle():\n    return query('SELECT 2')\n",
        },
    },
    # 5. deterministic secret finding
    {
        "case_id": "case-05-deterministic-secret",
        "scenario": "deterministic_secret_finding",
        "description": "Hardcoded secret detected deterministically",
        "expected_packet_success": True,
        "expected_policy": "requires_human_approval",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {"src/auth.py": "def get_key():\n    return None\n"},
        "after": {"src/auth.py": "def get_key():\n    return 'AKIAIOSFODNN7EXAMPLE'\n"},
    },
    # 6. deterministic sensitive-data finding
    {
        "case_id": "case-06-deterministic-sensitive-data",
        "scenario": "deterministic_sensitive_data_finding",
        "description": "Sensitive data flow into print sink detected deterministically",
        "expected_packet_success": True,
        "expected_policy": "requires_human_approval",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {"src/service.py": "def process():\n    pass\n"},
        "after": {"src/service.py": "def process():\n    password = 'user_password'\n    print(password)\n"},
    },
    # 7. high-severity finding requiring human review
    {
        "case_id": "case-07-high-severity-human-review",
        "scenario": "high_severity_policy_gate",
        "description": "High severity security issue requires human approval",
        "expected_packet_success": True,
        "expected_policy": "requires_human_approval",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {"src/api.py": "def check(): pass\n"},
        "after": {"src/api.py": "def check():\n    token = 'ghp_' + 'A' * 36\n"},
    },
    # 8. blocker finding requiring human review
    {
        "case_id": "case-08-blocker-human-review",
        "scenario": "blocker_severity_policy_gate",
        "description": "Blocker severity issue requires human approval gate",
        "expected_packet_success": True,
        "expected_policy": "requires_human_approval",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {"src/gateway.py": "def route(): pass\n"},
        "after": {"src/gateway.py": "def route():\n    key = 'AKIA1234567890ABCDEF'\n"},
    },
    # 9. low-confidence reviewer output
    {
        "case_id": "case-09-low-confidence-output",
        "scenario": "low_confidence_reviewer_output",
        "description": "Reviewer output below confidence threshold marked review-only",
        "expected_packet_success": True,
        "expected_policy": "review_only",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:low_confidence",
        "before": {"src/style.py": "def style(): return 1\n"},
        "after": {"src/style.py": "def style(): return 2\n"},
    },
    # 10. packet truncation
    {
        "case_id": "case-10-packet-truncation",
        "scenario": "packet_truncation_limits",
        "description": "Review packet bounded by max_lines_per_file constraint",
        "expected_packet_success": True,
        "expected_policy": "abstain",
        "expected_provider_valid": True,
        "expected_truncated": True,
        "provider": "mock:clean",
        "config": {
            "review": {"max_lines_per_file": 30, "max_total_context_lines": 50},
            "policy": {"abstain_on_truncated_changed_code": True},
        },
        "before": {"src/large.py": "def large_function():\n    pass\n"},
        "after": {"src/large.py": "def large_function():\n" + "\n".join(f"    val_{i} = {i}" for i in range(1, 150)) + "\n"},
    },
    # 11. redaction success
    {
        "case_id": "case-11-redaction-success",
        "scenario": "packet_redaction_success",
        "description": "Secrets in source sanitized prior to packet assembly",
        "expected_packet_success": True,
        "expected_policy": "requires_human_approval",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "expected_redacted": True,
        "provider": "mock:clean",
        "before": {"src/config.py": "AWS_KEY = None\n"},
        "after": {"src/config.py": "AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"},
    },
    # 12. redaction failure
    {
        "case_id": "case-12-redaction-failure",
        "scenario": "reviewer_secret_leak_rejected",
        "description": "Reviewer provider attempts to leak raw secret; output rejected",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": False,
        "expected_truncated": False,
        "provider": "mock:secret_leak",
        "before": {"src/leak.py": "def run(): pass\n"},
        "after": {"src/leak.py": "def run(): return 42\n"},
    },
    # 13. invalid provider JSON
    {
        "case_id": "case-13-invalid-provider-json",
        "scenario": "invalid_provider_schema",
        "description": "Malformed provider output failing Pydantic schema validation",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": False,
        "expected_truncated": False,
        "provider": "mock:invalid_json",
        "before": {"src/json_test.py": "val = 1\n"},
        "after": {"src/json_test.py": "val = 2\n"},
    },
    # 14. invalid path
    {
        "case_id": "case-14-invalid-path",
        "scenario": "invalid_provider_path",
        "description": "Provider refers to file outside the repository snapshot",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": False,
        "expected_truncated": False,
        "provider": "mock:invalid_path",
        "before": {"src/path_test.py": "def p(): pass\n"},
        "after": {"src/path_test.py": "def p(): return True\n"},
    },
    # 15. invalid line range
    {
        "case_id": "case-15-invalid-line-range",
        "scenario": "invalid_provider_line_range",
        "description": "Provider produces inverted line range (end < start)",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": False,
        "expected_truncated": False,
        "provider": "mock:invalid_line_range",
        "before": {"src/line_test.py": "line = 1\n"},
        "after": {"src/line_test.py": "line = 2\n"},
    },
    # 16. duplicate findings
    {
        "case_id": "case-16-duplicate-findings",
        "scenario": "duplicate_finding_merge",
        "description": "Reviewer findings overlapping deterministic findings merged",
        "expected_packet_success": True,
        "expected_policy": "requires_human_approval",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "expected_merged_count": 1,
        "provider": "mock:duplicate_finding",
        "before": {"src/dup_test.py": "KEY = None\n"},
        "after": {"src/dup_test.py": "KEY = 'AKIAIOSFODNN7EXAMPLE'\n"},
    },
    # 17. unsupported category / claim
    {
        "case_id": "case-17-unsupported-category",
        "scenario": "unsupported_validated_fix_claim",
        "description": "Provider claims fixability='validated' without test proof",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": False,
        "expected_truncated": False,
        "provider": "mock:unsupported_validated",
        "before": {"src/claim_test.py": "def c(): pass\n"},
        "after": {"src/claim_test.py": "def c(): return 100\n"},
    },
    # 18. fabricated test result
    {
        "case_id": "case-18-fabricated-test-result",
        "scenario": "fabricated_test_result",
        "description": "Provider claims execution of non-existent test file",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": False,
        "expected_truncated": False,
        "provider": "mock:fabricated_test",
        "before": {"src/fab_test.py": "def f(): pass\n"},
        "after": {"src/fab_test.py": "def f(): return 'ok'\n"},
    },
    # 19. unresolved dynamic import
    {
        "case_id": "case-19-unresolved-dynamic-import",
        "scenario": "unresolved_dynamic_import",
        "description": "Dynamic import recorded in index diagnostics and packet limitations",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {"src/plugin.py": "mod = None\n"},
        "after": {"src/plugin.py": "import importlib\nmod = importlib.import_module('dyn')\n"},
    },
    # 20. malformed source context
    {
        "case_id": "case-20-malformed-source-context",
        "scenario": "malformed_source_context",
        "description": "Malformed syntax safely bounded without packet failure",
        "expected_packet_success": True,
        "expected_policy": "allowed",
        "expected_provider_valid": True,
        "expected_truncated": False,
        "provider": "mock:clean",
        "before": {"src/valid.ts": "export function ok(): number { return 1; }\n"},
        "after": {"src/broken.ts": "export class BrokenClass {\n  // missing closing brace\n"},
    },
]

for case in CASES:
    c_dir = BASE_DIR / case["case_id"]
    c_dir.mkdir(parents=True, exist_ok=True)
    before_dir = c_dir / "before"
    after_dir = c_dir / "after"
    before_dir.mkdir(parents=True, exist_ok=True)
    after_dir.mkdir(parents=True, exist_ok=True)

    for path_str, content in case["before"].items():
        p = before_dir / path_str
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    for path_str, content in case["after"].items():
        p = after_dir / path_str
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    meta = {
        "case_id": case["case_id"],
        "category": "review_packet",
        "scenario": case["scenario"],
        "description": case["description"],
        "expected_packet_success": case["expected_packet_success"],
        "expected_policy": case["expected_policy"],
        "expected_provider_valid": case["expected_provider_valid"],
        "expected_truncated": case["expected_truncated"],
        "expected_redacted": case.get("expected_redacted", True),
        "expected_merged_count": case.get("expected_merged_count"),
        "provider": case.get("provider", "mock:clean"),
        "config": case.get("config", {}),
    }
    (c_dir / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

print(f"Generated {len(CASES)} review packet evaluation cases in {BASE_DIR}")
