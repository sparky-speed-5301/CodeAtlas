"""Script to generate controlled benchmark fixtures for Phase 6 patch validation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from codeatlas.patching import (
    compute_patch_hash,
    create_patch_proposal,
    generate_approval_token,
)

CASES_DIR = Path(__file__).resolve().parent / "cases" / "patching"


def create_fixture(
    case_id: str,
    title: str,
    base_files: dict[str, str],
    diff_text: str,
    target_files: list[str],
    *,
    expected_status: str,
    expected_policy_decision: str,
    expected_valid: bool,
    expected_applies_cleanly: bool,
    expected_syntax_valid: bool | None = True,
    expected_rejection_reason: str | None = None,
    tamper_hash: bool = False,
    wrong_base_commit: bool = False,
    invalid_token: bool = False,
    no_token: bool = False,
    risk_level: str = "low",
) -> None:
    case_path = CASES_DIR / case_id
    if case_path.exists():
        shutil.rmtree(case_path)
    case_path.mkdir(parents=True, exist_ok=True)

    # Write base repo files
    base_dir = case_path / "base"
    base_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in base_files.items():
        fp = base_dir / rel_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    # Generate proposal
    prop = create_patch_proposal(
        finding_id=f"FIND-{case_id}",
        provider_name="eval-fixer",
        provider_version="1.0.0",
        base_commit="HEAD" if not wrong_base_commit else "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        target_files=target_files,
        unified_diff=diff_text,
        rationale=title,
        expected_behavior="Resolve finding",
        risk_level=risk_level,
    )

    if tamper_hash:
        prop.patch_hash = "0000000000000000000000000000000000000000000000000000000000000000"

    (case_path / "proposal.json").write_text(prop.model_dump_json(indent=2), encoding="utf-8")

    if expected_syntax_valid is True and not (expected_valid and expected_applies_cleanly):
        syntax_val = None
    else:
        syntax_val = expected_syntax_valid

    meta = {
        "case_id": case_id,
        "title": title,
        "target_files": target_files,
        "expected_status": expected_status,
        "expected_policy_decision": expected_policy_decision,
        "expected_valid": expected_valid,
        "expected_applies_cleanly": expected_applies_cleanly,
        "expected_syntax_valid": syntax_val,
        "expected_rejection_reason": expected_rejection_reason,
        "application_allowed": expected_valid and expected_applies_cleanly,
        "original_worktree_must_remain_unchanged": True,
        "tamper_hash": tamper_hash,
        "wrong_base_commit": wrong_base_commit,
        "invalid_token": invalid_token,
        "no_token": no_token,
    }
    (case_path / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def generate_all_cases() -> None:
    CASES_DIR.mkdir(parents=True, exist_ok=True)

    # Case 1: One-line Python fix
    create_fixture(
        case_id="case-01-one-line-python-fix",
        title="One line Python fix",
        base_files={"src/calc.py": "def divide(a, b):\n    return a / b\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,3 @@\n def divide(a, b):\n+    if b == 0: return 0\n     return a / b\n",
        target_files=["src/calc.py"],
        expected_status="validated",
        expected_policy_decision="requires_human_approval",
        expected_valid=True,
        expected_applies_cleanly=True,
    )

    # Case 2: One-line TypeScript fix
    create_fixture(
        case_id="case-02-one-line-ts-fix",
        title="One line TypeScript fix",
        base_files={"src/util.ts": "export function getLength(str: string | null): number {\n  return str.length;\n}\n"},
        diff_text="--- a/src/util.ts\n+++ b/src/util.ts\n@@ -1,3 +1,3 @@\n export function getLength(str: string | null): number {\n-  return str.length;\n+  return str ? str.length : 0;\n }\n",
        target_files=["src/util.ts"],
        expected_status="validated",
        expected_policy_decision="requires_human_approval",
        expected_valid=True,
        expected_applies_cleanly=True,
    )

    # Case 3: Multi-hunk patch
    create_fixture(
        case_id="case-03-multi-hunk-patch",
        title="Multi-hunk Python patch",
        base_files={"src/service.py": "def start():\n    init()\n\ndef process():\n    step1()\n    step2()\n\ndef stop():\n    cleanup()\n"},
        diff_text=(
            "--- a/src/service.py\n"
            "+++ b/src/service.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def start():\n"
            "+    log_start()\n"
            "     init()\n"
            "@@ -7,2 +8,3 @@\n"
            " def stop():\n"
            "+    log_stop()\n"
            "     cleanup()\n"
        ),
        target_files=["src/service.py"],
        expected_status="validated",
        expected_policy_decision="requires_human_approval",
        expected_valid=True,
        expected_applies_cleanly=True,
    )

    # Case 4: Patch with unchanged context
    create_fixture(
        case_id="case-04-patch-with-unchanged-context",
        title="Patch with unchanged context lines",
        base_files={"src/parser.py": "import os\n\ndef parse_data(raw):\n    header = raw[:10]\n    payload = raw[10:]\n    return payload\n"},
        diff_text=(
            "--- a/src/parser.py\n"
            "+++ b/src/parser.py\n"
            "@@ -3,4 +3,5 @@\n"
            " def parse_data(raw):\n"
            "+    if not raw: return {}\n"
            "     header = raw[:10]\n"
            "     payload = raw[10:]\n"
            "     return payload\n"
        ),
        target_files=["src/parser.py"],
        expected_status="validated",
        expected_policy_decision="requires_human_approval",
        expected_valid=True,
        expected_applies_cleanly=True,
    )

    # Case 5: Patch adding safe null check
    create_fixture(
        case_id="case-05-patch-adding-safe-null-check",
        title="Patch adding safe null check",
        base_files={"src/auth.py": "def authenticate(user):\n    role = user.role\n    return role == 'admin'\n"},
        diff_text=(
            "--- a/src/auth.py\n"
            "+++ b/src/auth.py\n"
            "@@ -1,3 +1,4 @@\n"
            " def authenticate(user):\n"
            "+    if not user: return False\n"
            "     role = user.role\n"
            "     return role == 'admin'\n"
        ),
        target_files=["src/auth.py"],
        expected_status="validated",
        expected_policy_decision="requires_human_approval",
        expected_valid=True,
        expected_applies_cleanly=True,
    )

    # Case 6: Malformed hunk
    create_fixture(
        case_id="case-06-malformed-hunk",
        title="Malformed hunk line count mismatch",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,10 +1,10 @@\n def run():\n",
        target_files=["src/calc.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Hunk line count mismatch",
    )

    # Case 7: Absolute path
    create_fixture(
        case_id="case-07-absolute-path",
        title="Absolute path rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a//etc/passwd\n+++ b//etc/passwd\n@@ -1,1 +1,1 @@\n-a\n+b\n",
        target_files=["/etc/passwd"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Absolute path detected",
    )

    # Case 8: Path traversal
    create_fixture(
        case_id="case-08-path-traversal",
        title="Path traversal rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1,1 +1,1 @@\n-a\n+b\n",
        target_files=["../../etc/passwd"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Path traversal detected",
    )

    # Case 9: Outside snapshot path
    create_fixture(
        case_id="case-09-outside-snapshot-path",
        title="Outside snapshot path rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/outside/missing.py\n+++ b/outside/missing.py\n@@ -1,1 +1,1 @@\n-a\n+b\n",
        target_files=["outside/missing.py"],
        expected_status="failed_validation",
        expected_policy_decision="requires_human_approval",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Patch conflict / apply error",
    )

    # Case 10: Patch conflict
    create_fixture(
        case_id="case-10-patch-conflict",
        title="Patch conflict on stale lines",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -999,1 +999,1 @@\n-def obsolete():\n+def fixed():\n",
        target_files=["src/calc.py"],
        expected_status="failed_validation",
        expected_policy_decision="requires_human_approval",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Patch conflict / apply error",
    )

    # Case 11: Test modification
    create_fixture(
        case_id="case-11-test-modification",
        title="Test file modification policy rejection",
        base_files={"tests/test_calc.py": "def test_calc():\n    assert True\n"},
        diff_text="--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@ -1,2 +1,2 @@\n def test_calc():\n-    assert True\n+    assert False\n",
        target_files=["tests/test_calc.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Modifying test files is prohibited",
    )

    # Case 12: Workflow modification
    create_fixture(
        case_id="case-12-workflow-modification",
        title="Workflow file modification policy rejection",
        base_files={".github/workflows/ci.yml": "name: CI\non: [push]\n"},
        diff_text="--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1,2 +1,2 @@\n-name: CI\n+name: CI Hacked\n on: [push]\n",
        target_files=[".github/workflows/ci.yml"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Modifying CI/CD or workflow files is prohibited",
    )

    # Case 13: Dependency modification
    create_fixture(
        case_id="case-13-dependency-modification",
        title="Dependency manifest modification policy rejection",
        base_files={"requirements.txt": "requests==2.28.0\n"},
        diff_text="--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1,1 +1,1 @@\n-requests==2.28.0\n+requests==2.31.0\n",
        target_files=["requirements.txt"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Modifying dependency manifest files is prohibited",
    )

    # Case 14: Lockfile modification
    create_fixture(
        case_id="case-14-lockfile-modification",
        title="Lockfile modification policy rejection",
        base_files={"package-lock.json": "{\n  \"version\": \"1.0.0\"\n}\n"},
        diff_text="--- a/package-lock.json\n+++ b/package-lock.json\n@@ -1,3 +1,3 @@\n {\n-  \"version\": \"1.0.0\"\n+  \"version\": \"1.0.1\"\n }\n",
        target_files=["package-lock.json"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Modifying dependency lockfiles is prohibited",
    )

    # Case 15: Configuration modification
    create_fixture(
        case_id="case-15-configuration-modification",
        title="Configuration file modification policy rejection",
        base_files={".codeatlas.yml": "review:\n  max_findings: 5\n"},
        diff_text="--- a/.codeatlas.yml\n+++ b/.codeatlas.yml\n@@ -1,2 +1,2 @@\n review:\n-  max_findings: 5\n+  max_findings: 100\n",
        target_files=[".codeatlas.yml"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Modifying configuration files is prohibited",
    )

    # Case 16: Secret-introducing patch
    create_fixture(
        case_id="case-16-secret-introducing-patch",
        title="Secret introducing patch redaction failure",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,3 @@\n def run():\n+    KEY = 'AKIA1234567890ABCDEF'\n     return 42\n",
        target_files=["src/calc.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Patch contains potentially sensitive tokens",
    )

    # Case 17: Oversized patch (exceeding byte limit)
    oversized_comment = "# " + ("A" * 600000) + "\n"
    create_fixture(
        case_id="case-17-oversized-patch",
        title="Oversized byte patch rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text=f"--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,1 +1,2 @@\n def run():\n+{oversized_comment}",
        target_files=["src/calc.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Patch exceeds maximum byte size",
    )

    # Case 18: Too many files (6 files > max_files 5)
    six_files_base = {f"src/mod_{i}.py": f"def fn_{i}(): pass\n" for i in range(6)}
    six_diff = "\n".join(
        f"--- a/src/mod_{i}.py\n+++ b/src/mod_{i}.py\n@@ -1,1 +1,2 @@\n def fn_{i}(): pass\n+# touch\n"
        for i in range(6)
    )
    create_fixture(
        case_id="case-18-too-many-files",
        title="Too many files modified policy rejection",
        base_files=six_files_base,
        diff_text=six_diff,
        target_files=[f"src/mod_{i}.py" for i in range(6)],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Patch modifies 6 files",
    )

    # Case 19: Too many changed lines (> 150 lines)
    added_160 = "\n".join(f"+line_{i} = {i}" for i in range(160))
    create_fixture(
        case_id="case-19-too-many-changed-lines",
        title="Too many changed lines policy rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text=f"--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,1 +1,161 @@\n def run():\n{added_160}\n",
        target_files=["src/calc.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="exceeding allowed maximum of 150",
    )

    # Case 20: Binary patch
    create_fixture(
        case_id="case-20-binary-patch",
        title="Binary patch content rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="GIT binary patch\nliteral 10\nzc$@*N\n",
        target_files=["src/calc.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Binary patch content is unsupported",
    )

    # Case 21: Delete file proposal
    create_fixture(
        case_id="case-21-delete-file-proposal",
        title="Delete file operation policy rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def run():\n-    return 42\n",
        target_files=["src/calc.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Deleting files is prohibited",
    )

    # Case 22: Rename proposal
    create_fixture(
        case_id="case-22-rename-proposal",
        title="Rename file operation policy rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="rename from src/calc.py\nrename to src/calc_new.py\n--- a/src/calc.py\n+++ b/src/calc_new.py\n@@ -1,2 +1,2 @@\n def run():\n     return 42\n",
        target_files=["src/calc_new.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Renaming files is prohibited",
    )

    # Case 23: Empty patch
    create_fixture(
        case_id="case-23-empty-patch",
        title="Empty patch content rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="",
        target_files=["src/calc.py"],
        expected_status="rejected",
        expected_policy_decision="rejected",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Unified diff is empty",
    )

    # Case 24: Wrong base commit
    create_fixture(
        case_id="case-24-wrong-base-commit",
        title="Wrong base commit resolution failure",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,3 @@\n def run():\n+    # safe\n     return 42\n",
        target_files=["src/calc.py"],
        wrong_base_commit=True,
        expected_status="failed_validation",
        expected_policy_decision="requires_human_approval",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="could not be resolved",
    )

    # Case 25: Stale patch hash
    create_fixture(
        case_id="case-25-stale-patch-hash",
        title="Stale or tampered patch hash mismatch",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,3 @@\n def run():\n+    # safe\n     return 42\n",
        target_files=["src/calc.py"],
        tamper_hash=True,
        expected_status="failed_validation",
        expected_policy_decision="requires_human_approval",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="Patch hash integrity check failed",
    )

    # Case 26: Unapproved apply
    create_fixture(
        case_id="case-26-unapproved-apply",
        title="Unapproved patch application blocked",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,3 @@\n def run():\n+    # safe\n     return 42\n",
        target_files=["src/calc.py"],
        no_token=True,
        expected_status="requires_human_approval",
        expected_policy_decision="requires_human_approval",
        expected_valid=True,
        expected_applies_cleanly=False,
        expected_rejection_reason="requires human approval token",
    )

    # Case 27: Invalid approval token
    create_fixture(
        case_id="case-27-invalid-approval-token",
        title="Invalid approval token rejection",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,3 @@\n def run():\n+    # safe\n     return 42\n",
        target_files=["src/calc.py"],
        invalid_token=True,
        expected_status="failed_validation",
        expected_policy_decision="requires_human_approval",
        expected_valid=False,
        expected_applies_cleanly=False,
        expected_rejection_reason="does not match expected token",
    )

    # Case 28: Original worktree mutation attempt (zero mutation verified)
    create_fixture(
        case_id="case-28-original-worktree-mutation-attempt",
        title="Zero original worktree mutation verification",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,3 @@\n def run():\n+    # safe comment in sandbox\n     return 42\n",
        target_files=["src/calc.py"],
        expected_status="validated",
        expected_policy_decision="requires_human_approval",
        expected_valid=True,
        expected_applies_cleanly=True,
    )

    # Case 29: Syntax breaking patch
    create_fixture(
        case_id="case-29-syntax-breaking-patch",
        title="Syntax breaking patch rejected by validator",
        base_files={"src/calc.py": "def run():\n    return 42\n"},
        diff_text="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,3 @@\n def run():\n+    def broken(:\n     return 42\n",
        target_files=["src/calc.py"],
        expected_status="failed_validation",
        expected_policy_decision="requires_human_approval",
        expected_valid=False,
        expected_applies_cleanly=True,
        expected_syntax_valid=False,
        expected_rejection_reason="Syntax error",
    )


if __name__ == "__main__":
    generate_all_cases()
    print(f"Generated {len(list(CASES_DIR.iterdir()))} cases in {CASES_DIR}")
