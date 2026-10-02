"""Generate Phase 7B live-patch evaluation fixtures under eval/cases/live-patches/.

Each case contains:
  - metadata.json     case description, expectations
  - before/           git base tree
  - after/            git head tree
  - response.json     fake provider output (findings + optional patch_suggestions)

Fixtures are executed by eval/eval_live_patches.py with a deterministic
FakeTransport; no live API calls are made and no patch is ever applied.
"""

from __future__ import annotations

import json
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases" / "live-patches"

BASE_APP = '''def compute(items):
    total = 0
    for i in items:
        total += i
    return total


def greet(name):
    return "Hello " + name
'''

BUGGY_APP = '''def compute(items):
    total = 0
    for i in items:
        total += i
        if total > 100:
            total = total // 0
    return total


def greet(name):
    return "Hello " + name
'''

MULTI_BUGGY_APP = '''def compute(items):
    total = 0
    for i in items:
        total += i
        if total > 100:
            total = total // 0
    return total


def greet(name):
    return "Hello " + name
    print("dead code")
'''

TEST_FILE = '''def test_ok():
    assert True
'''

TEST_FILE_AFTER = '''def test_ok():
    assert True
    assert something_else
'''

WORKFLOW_FILE = '''name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
'''

WORKFLOW_FILE_AFTER = WORKFLOW_FILE + '      - run: echo extra\n'

REQUIREMENTS_FILE = 'requests==2.0.0\n'
REQUIREMENTS_AFTER = 'requests==2.0.0\nflask==1.0.0\n'

LOCKFILE_BEFORE = '{"lockfileVersion": 1}\n'
LOCKFILE_AFTER = '{"lockfileVersion": 1}\n"touched": true\n'

PYTEST_INI = '[pytest]\n'
PYTEST_INI_AFTER = '[pytest]\n# touched\n'

TS_BEFORE = '''function load(): string {
  return "data";
}
'''

TS_AFTER = '''async function load(): Promise<string> {
  return fetch("/api").text();
}
'''

GUARD_BEFORE = '''def get(d, k):
    return d[k]
'''

GUARD_AFTER = '''def get(d, k):
    value = d[k]
    return value
'''

OTHER_HELPER = '''def helper():
    return 42
'''

SENSITIVE_SERVICE_BEFORE = '''def process():
    pass
'''

SENSITIVE_SERVICE = '''def render(user):
    print(user.password)
'''


def ctx(text: str) -> str:
    return " " + text


def patch_line(prefix: str, text: str) -> str:
    return prefix + text


FIND_TOTAL = "            total = total // 0"
FIX_TOTAL = "            total = total // 2"

SENSITIVE_REDACT_DIFF = "\n".join([
    "--- a/src/service.py",
    "+++ b/src/service.py",
    "@@ -1,2 +1,2 @@",
    ctx("def render(user):"),
    patch_line("-", "    print(user.password)"),
    patch_line("+", '    print("[REDACTED]")'),
]) + "\n"

# One-line fix: replace the division by zero (changed line 6).
FIX_TOTAL_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -5,3 +5,3 @@",
    ctx("        if total > 100:"),
    patch_line("-", FIND_TOTAL),
    patch_line("+", FIX_TOTAL),
    ctx("    return total"),
]) + "\n"

ERROR_HANDLING_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -5,3 +5,6 @@",
    ctx("        if total > 100:"),
    patch_line("-", FIND_TOTAL),
    patch_line("+", "            try:"),
    patch_line("+", "                total = total // 0"),
    patch_line("+", "            except ZeroDivisionError:"),
    patch_line("+", "                total = 0"),
    ctx("    return total"),
]) + "\n"

MULTI_HUNK_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -5,3 +5,3 @@",
    ctx("        if total > 100:"),
    patch_line("-", FIND_TOTAL),
    patch_line("+", FIX_TOTAL),
    ctx("    return total"),
    "@@ -10,3 +10,2 @@",
    ctx("def greet(name):"),
    ctx('    return "Hello " + name'),
    patch_line("-", '    print("dead code")'),
]) + "\n"

TS_AWAIT_DIFF = "\n".join([
    "--- a/src/service.ts",
    "+++ b/src/service.ts",
    "@@ -1,3 +1,3 @@",
    ctx("async function load(): Promise<string> {"),
    patch_line("-", '  return fetch("/api").text();'),
    patch_line("+", '  return (await fetch("/api")).text();'),
    ctx("}"),
]) + "\n"

NULL_CHECK_DIFF = "\n".join([
    "--- a/src/guard.py",
    "+++ b/src/guard.py",
    "@@ -1,3 +1,3 @@",
    ctx("def get(d, k):"),
    patch_line("-", "    value = d[k]"),
    patch_line("+", "    value = d.get(k, 0)"),
    ctx("    return value"),
]) + "\n"

UNCHANGED_LINE_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -50,2 +50,2 @@",
    ctx("def greet(name):"),
    patch_line("-", '    return "Hello " + name'),
    patch_line("+", '    return "Hi " + name'),
]) + "\n"

OUTSIDE_SNAPSHOT_DIFF = "\n".join([
    "--- a/src/other.py",
    "+++ b/src/other.py",
    "@@ -2 +2 @@",
    patch_line("-", "    return 42"),
    patch_line("+", "    return 43"),
]) + "\n"

ABSOLUTE_PATH_DIFF = "\n".join([
    "--- /etc/passwd",
    "+++ b/src/app.py",
    "@@ -5,3 +5,3 @@",
    ctx("        if total > 100:"),
    patch_line("-", FIND_TOTAL),
    patch_line("+", FIX_TOTAL),
    ctx("    return total"),
]) + "\n"

TRAVERSAL_DIFF = "\n".join([
    "--- ../../etc/passwd",
    "+++ b/src/app.py",
    "@@ -5,3 +5,3 @@",
    ctx("        if total > 100:"),
    patch_line("-", FIND_TOTAL),
    patch_line("+", FIX_TOTAL),
    ctx("    return total"),
]) + "\n"

HUNK_COUNT_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -5,3 +5,3 @@",
    ctx("        if total > 100:"),
    patch_line("-", FIND_TOTAL),
    patch_line("+", FIX_TOTAL),
]) + "\n"

MALFORMED_HUNK_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -5,3 +5,3 @@",
    ctx("        if total > 100:"),
    "garbage hunk line without prefix",
    patch_line("-", FIND_TOTAL),
    patch_line("+", FIX_TOTAL),
    ctx("    return total"),
]) + "\n"

CONFLICT_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -5,3 +5,3 @@",
    ctx("        if total > 100:"),
    patch_line("-", "            total = total // 999"),
    patch_line("+", FIX_TOTAL),
    ctx("    return total"),
]) + "\n"

STALE_BASE_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -4,3 +4,3 @@",
    ctx("        total += i"),
    patch_line("-", "            total = total // 0"),
    patch_line("+", FIX_TOTAL),
    ctx("        if total > 100:"),
]) + "\n"

SECRET_INTRO_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -6,2 +6,3 @@",
    patch_line("-", FIND_TOTAL),
    patch_line("+", "            api_key = 'supersecretkey123456'"),
    patch_line("+", "            total = total // 2"),
    ctx("    return total"),
]) + "\n"

TOO_MANY_LINES_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -5,3 +5,4 @@",
    patch_line("-", "        if total > 100:"),
    patch_line("-", FIND_TOTAL),
    patch_line("-", "    return total"),
    patch_line("+", "        if total > 100:"),
    patch_line("+", FIX_TOTAL),
    patch_line("+", '            print("checked")'),
    patch_line("+", "    return total"),
]) + "\n"

BINARY_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "GIT binary patch",
    "literal 10",
]) + "\n"

DELETE_DIFF = (
    "--- a/src/app.py\n+++ /dev/null\n@@ -1,11 +0,0 @@\n"
    + "".join("-" + line + "\n" for line in BUGGY_APP.splitlines())
)

RENAME_DIFF = "rename from src/app.py\nrename to src/app2.py\n"

TEST_MOD_DIFF = "\n".join([
    "--- a/tests/test_app.py",
    "+++ b/tests/test_app.py",
    "@@ -3 +3,2 @@",
    ctx("    assert something_else"),
    patch_line("+", "    assert another"),
]) + "\n"

WORKFLOW_MOD_DIFF = "\n".join([
    "--- a/.github/workflows/ci.yml",
    "+++ b/.github/workflows/ci.yml",
    "@@ -8 +8,2 @@",
    ctx("      - run: echo extra"),
    patch_line("+", "      - run: echo patched"),
]) + "\n"

DEPENDENCY_MOD_DIFF = "\n".join([
    "--- a/requirements.txt",
    "+++ b/requirements.txt",
    "@@ -2 +2,2 @@",
    ctx("flask==1.0.0"),
    patch_line("+", "django==1.0.0"),
]) + "\n"

LOCKFILE_MOD_DIFF = "\n".join([
    "--- a/package-lock.json",
    "+++ b/package-lock.json",
    "@@ -2 +2,2 @@",
    ctx('"touched": true'),
    patch_line("+", '"patched": true'),
]) + "\n"

CONFIG_MOD_DIFF = "\n".join([
    "--- a/pytest.ini",
    "+++ b/pytest.ini",
    "@@ -2 +2,2 @@",
    ctx("# touched"),
    patch_line("+", "# patched"),
]) + "\n"


def _finding(**overrides):
    base = {
        "id": "CA-REV-001",
        "file": "src/app.py",
        "start_line": 5,
        "end_line": 6,
        "severity": "medium",
        "category": "LOGIC_BUG",
        "claim": "The accumulator is divided by zero once the total exceeds 100",
        "impact": "Calling compute with a large total raises ZeroDivisionError",
        "evidence": ["changed lines add 'total = total // 0'"],
        "evidence_strength": "supported",
        "confidence": 0.9,
        "limitations": [],
        "status": "detected",
        "fixability": "review_required",
        "provenance": {"origin": "reviewer"},
    }
    base.update(overrides)
    return base


def _suggestion(**overrides):
    base = {
        "suggestion_id": "CA-SUG-001",
        "finding_id": "CA-REV-001",
        "unified_diff": FIX_TOTAL_DIFF,
        "rationale": "Replace division by zero with a safe divisor",
        "expected_behavior": "compute no longer raises ZeroDivisionError for large totals",
        "target_files": ["src/app.py"],
        "risk_level": "low",
        "limitations": [],
        "provider_provenance": {"origin": "provider"},
    }
    base.update(overrides)
    return base


def _response(findings=None, suggestions=None, **extra):
    payload = {
        "summary": "Provider review with draft patch suggestions.",
        "findings": findings if findings is not None else [_finding()],
        "limitations": [],
        "abstentions": [],
    }
    if suggestions is not None:
        payload["patch_suggestions"] = suggestions
    payload.update(extra)
    return payload


def _valid_case(case_id, scenario, diff=FIX_TOTAL_DIFF, before=None, after=None, findings=None, suggestion_overrides=None, config=None):
    suggestion = _suggestion(unified_diff=diff)
    if suggestion_overrides:
        suggestion.update(suggestion_overrides)
    return {
        "case_id": case_id,
        "scenario": scenario,
        "test_group": "valid",
        "description": scenario,
        "before": before if before is not None else {"src/app.py": BASE_APP, "tests/test_app.py": TEST_FILE},
        "after": after if after is not None else {"src/app.py": BUGGY_APP, "tests/test_app.py": TEST_FILE},
        "config": config or {},
        "response": _response(findings=findings, suggestions=[suggestion]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 1,
            "suggestions_rejected": 0,
            "proposals_created": 1,
            "expected_proposal_status": "requires_human_approval",
            "rejection_reasons": [],
        },
    }


CASES = [
    # ------------------------------------------------------------------
    # Valid draft suggestions -> PatchProposal created, human approval.
    # ------------------------------------------------------------------
    _valid_case("case-01-valid-one-line-python-fix", "valid_one_line_python_fix"),
    _valid_case(
        "case-02-valid-one-line-ts-fix",
        "valid_one_line_ts_fix",
        diff=TS_AWAIT_DIFF,
        before={"src/service.ts": TS_BEFORE},
        after={"src/service.ts": TS_AFTER},
        findings=[_finding(id="CA-REV-TS1", file="src/service.ts", start_line=2, end_line=2,
                           claim="fetch promise is not awaited", impact="response text is lost")],
        suggestion_overrides={"finding_id": "CA-REV-TS1", "target_files": ["src/service.ts"]},
    ),
    _valid_case(
        "case-03-valid-null-check-patch",
        "valid_null_check_patch",
        diff=NULL_CHECK_DIFF,
        before={"src/guard.py": GUARD_BEFORE},
        after={"src/guard.py": GUARD_AFTER},
        findings=[_finding(id="CA-REV-GUARD", file="src/guard.py", start_line=2, end_line=3,
                           claim="dict access may raise KeyError", impact="lookup failure")],
        suggestion_overrides={"finding_id": "CA-REV-GUARD", "target_files": ["src/guard.py"]},
    ),
    _valid_case(
        "case-04-valid-missing-await-patch",
        "valid_missing_await_patch",
        diff=TS_AWAIT_DIFF,
        before={"src/service.ts": TS_BEFORE},
        after={"src/service.ts": TS_AFTER},
        findings=[_finding(id="CA-REV-TS1", file="src/service.ts", start_line=2, end_line=2,
                           claim="missing await on fetch call", impact="unhandled promise")],
        suggestion_overrides={"finding_id": "CA-REV-TS1", "target_files": ["src/service.ts"]},
    ),
    _valid_case("case-05-valid-error-handling-patch", "valid_error_handling_patch", diff=ERROR_HANDLING_DIFF),
    _valid_case(
        "case-06-valid-multi-hunk-patch",
        "valid_multi_hunk_patch",
        diff=MULTI_HUNK_DIFF,
        before={"src/app.py": BASE_APP},
        after={"src/app.py": MULTI_BUGGY_APP},
        config={"patch": {"max_changed_lines": 20}},
    ),
    _valid_case(
        "case-07-valid-suggestion-with-limitations",
        "valid_suggestion_with_limitations",
        suggestion_overrides={"limitations": ["requires runtime confirmation"]},
    ),
    {
        "case_id": "case-08-valid-multiple-suggestions",
        "scenario": "valid_multiple_suggestions",
        "test_group": "valid",
        "description": "Two independent valid suggestions for two findings",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "config": {},
        "response": _response(
            findings=[
                _finding(),
                _finding(id="CA-REV-002", category="CODE_QUALITY",
                         claim="Boundary comparison may be off by one", impact="edge case"),
            ],
            suggestions=[
                _suggestion(),
                _suggestion(
                    suggestion_id="CA-SUG-002",
                    finding_id="CA-REV-002",
                    unified_diff="\n".join([
                        "--- a/src/app.py",
                        "+++ b/src/app.py",
                        "@@ -5,3 +5,3 @@",
                        patch_line("-", "        if total > 100:"),
                        patch_line("+", "        if total >= 100:"),
                        ctx(FIND_TOTAL),
                        ctx("    return total"),
                    ]) + "\n",
                    rationale="Include the boundary value",
                ),
            ],
        ),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 2,
            "suggestions_accepted": 2,
            "suggestions_rejected": 0,
            "proposals_created": 2,
            "expected_proposal_status": "requires_human_approval",
            "rejection_reasons": [],
        },
    },
    {
        "case_id": "case-09-valid-clean-review-no-suggestions",
        "scenario": "valid_clean_review_no_suggestions",
        "test_group": "valid",
        "description": "Clean review with no suggestions creates no proposals",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "config": {},
        "response": _response(findings=[], suggestions=[]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
        },
    },

    # ------------------------------------------------------------------
    # Invalid suggestions -> rejected, review preserved.
    # ------------------------------------------------------------------
    {
        "case_id": "case-10-invalid-malformed-json",
        "scenario": "invalid_malformed_json",
        "test_group": "invalid",
        "description": "Unparseable provider response yields no suggestions",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "response_text": "definitely not json {{{",
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "provider_output_valid": False,
        },
    },
    _valid_case(
        "case-11-invalid-malformed-unified-diff",
        "invalid_malformed_unified_diff",
        diff=MALFORMED_HUNK_DIFF,
        suggestion_overrides={},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["malformed unified diff"],
        },
    },
    {
        "case_id": "case-12-invalid-absolute-path",
        "scenario": "invalid_absolute_path",
        "test_group": "invalid",
        "description": "Diff with an absolute path header is rejected by the parser",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "config": {},
        "response": _response(suggestions=[_suggestion(unified_diff=ABSOLUTE_PATH_DIFF)]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["Absolute path detected"],
        },
    },
    {
        "case_id": "case-13-invalid-traversal-path",
        "scenario": "invalid_traversal_path",
        "test_group": "invalid",
        "description": "Diff with path traversal is rejected by the parser",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "config": {},
        "response": _response(suggestions=[_suggestion(unified_diff=TRAVERSAL_DIFF)]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["Path traversal detected"],
        },
    },
    {
        "case_id": "case-14-invalid-outside-snapshot-path",
        "scenario": "invalid_outside_snapshot_path",
        "test_group": "invalid",
        "description": "Patch targeting a file outside the review packet is rejected",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "config": {},
        "response": _response(suggestions=[_suggestion(
            unified_diff=OUTSIDE_SNAPSHOT_DIFF.replace("src/other.py", "src/ghost.py"),
            target_files=["src/ghost.py"],
        )]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["outside review packet"],
        },
    },
    _valid_case(
        "case-15-invalid-hunk-counts",
        "invalid_hunk_counts",
        diff=HUNK_COUNT_DIFF,
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["Hunk line count mismatch"],
        },
    },
    _valid_case(
        "case-16-invalid-patch-conflict",
        "invalid_patch_conflict",
        diff=CONFLICT_DIFF,
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["stale base commit or patch conflict"],
        },
    },
    _valid_case(
        "case-17-invalid-stale-base-commit",
        "invalid_stale_base_commit",
        diff=STALE_BASE_DIFF,
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["stale base commit or patch conflict"],
        },
    },
    {
        "case_id": "case-18-invalid-test-modification",
        "scenario": "invalid_test_modification",
        "test_group": "invalid",
        "description": "Provider patch modifying a test file is rejected by patch policy",
        "before": {"src/app.py": BASE_APP, "tests/test_app.py": TEST_FILE},
        "after": {"src/app.py": BUGGY_APP, "tests/test_app.py": TEST_FILE_AFTER},
        "config": {},
        "response": _response(suggestions=[_suggestion(
            unified_diff=TEST_MOD_DIFF,
            target_files=["tests/test_app.py"],
        )]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["test files is prohibited"],
        },
    },
    {
        "case_id": "case-19-invalid-workflow-modification",
        "scenario": "invalid_workflow_modification",
        "test_group": "invalid",
        "description": "Provider patch modifying CI workflow is rejected by patch policy",
        "before": {"src/app.py": BASE_APP, ".github/workflows/ci.yml": WORKFLOW_FILE},
        "after": {"src/app.py": BUGGY_APP, ".github/workflows/ci.yml": WORKFLOW_FILE_AFTER},
        "config": {},
        "response": _response(suggestions=[_suggestion(
            unified_diff=WORKFLOW_MOD_DIFF,
            target_files=[".github/workflows/ci.yml"],
        )]),
        "expected": {
            "request_made": True,
            "expected_policy": "requires_human_approval",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["workflow files is prohibited"],
        },
    },
    {
        "case_id": "case-20-invalid-dependency-modification",
        "scenario": "invalid_dependency_modification",
        "test_group": "invalid",
        "description": "Provider patch modifying dependency manifest is rejected",
        "before": {"src/app.py": BASE_APP, "requirements.txt": REQUIREMENTS_FILE},
        "after": {"src/app.py": BUGGY_APP, "requirements.txt": REQUIREMENTS_AFTER},
        "config": {},
        "response": _response(suggestions=[_suggestion(
            unified_diff=DEPENDENCY_MOD_DIFF,
            target_files=["requirements.txt"],
        )]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["dependency manifest files is prohibited"],
        },
    },
    {
        "case_id": "case-21-invalid-lockfile-modification",
        "scenario": "invalid_lockfile_modification",
        "test_group": "invalid",
        "description": "Provider patch modifying a lockfile is rejected",
        "before": {"src/app.py": BASE_APP, "package-lock.json": LOCKFILE_BEFORE},
        "after": {"src/app.py": BUGGY_APP, "package-lock.json": LOCKFILE_AFTER},
        "config": {},
        "response": _response(suggestions=[_suggestion(
            unified_diff=LOCKFILE_MOD_DIFF,
            target_files=["package-lock.json"],
        )]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["lockfiles is prohibited"],
        },
    },
    {
        "case_id": "case-22-invalid-protected-configuration",
        "scenario": "invalid_protected_configuration",
        "test_group": "invalid",
        "description": "Provider patch modifying configuration is rejected",
        "before": {"src/app.py": BASE_APP, "pytest.ini": PYTEST_INI},
        "after": {"src/app.py": BUGGY_APP, "pytest.ini": PYTEST_INI_AFTER},
        "config": {},
        "response": _response(suggestions=[_suggestion(
            unified_diff=CONFIG_MOD_DIFF,
            target_files=["pytest.ini"],
        )]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["configuration files is prohibited"],
        },
    },
    _valid_case(
        "case-23-invalid-secret-introducing-patch",
        "invalid_secret_introducing_patch",
        diff=SECRET_INTRO_DIFF,
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            # The sanitizer's per-suggestion secret scan rejects the patch
            # before materialization, so it is never received downstream.
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "sanitizer_rejected": True,
        },
    },
    _valid_case(
        "case-24-invalid-oversized-patch",
        "invalid_oversized_patch",
        diff="\n".join([
            "--- a/src/app.py",
            "+++ b/src/app.py",
            "@@ -5,3 +5,43 @@",
            ctx("        if total > 100:"),
            patch_line("-", FIND_TOTAL),
            *[patch_line("+", "            total = total // 2  # padding padding padding padding") for _ in range(40)],
            ctx("    return total"),
        ]) + "\n",
        config={"patch": {"max_patch_bytes": 1200}},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["exceeds maximum byte size"],
        },
    },
    _valid_case(
        "case-25-invalid-too-many-files",
        "invalid_too_many_files",
        before={"src/app.py": BASE_APP, **{f"src/mod_{i}.py": f"def f{i}():\n    return {i}\n" for i in range(6)}},
        after={"src/app.py": BUGGY_APP, **{f"src/mod_{i}.py": f"def f{i}():\n    return {i} + 1\n" for i in range(6)}},
        diff="\n".join(
            "\n".join([
                f"--- a/src/mod_{i}.py",
                f"+++ b/src/mod_{i}.py",
                "@@ -2 +2 @@",
                patch_line("-", f"    return {i}"),
                patch_line("+", f"    return {i} + 1"),
            ]) for i in range(6)
        ) + "\n",
        suggestion_overrides={"target_files": [f"src/mod_{i}.py" for i in range(6)]},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["exceeding limit of 5"],
        },
    },
    _valid_case(
        "case-26-invalid-too-many-changed-lines",
        "invalid_too_many_changed_lines",
        diff=TOO_MANY_LINES_DIFF,
        config={"patch": {"max_changed_lines": 5}},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["exceeding limit of 5"],
        },
    },
    _valid_case(
        "case-27-invalid-binary-patch",
        "invalid_binary_patch",
        diff=BINARY_DIFF,
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["Binary patch content is unsupported"],
        },
    },
    _valid_case(
        "case-28-invalid-delete-file-suggestion",
        "invalid_delete_file_suggestion",
        diff=DELETE_DIFF,
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["file operation prohibited by policy: delete"],
        },
    },
    _valid_case(
        "case-29-invalid-rename-suggestion",
        "invalid_rename_suggestion",
        diff=RENAME_DIFF,
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["file operation prohibited by policy: rename"],
        },
    },
    {
        "case_id": "case-30-invalid-empty-patch",
        "scenario": "invalid_empty_patch",
        "test_group": "invalid",
        "description": "Empty unified diff is rejected at sanitization",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "config": {},
        "response": _response(suggestions=[_suggestion(unified_diff="")]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "sanitizer_rejected": True,
        },
    },
    _valid_case(
        "case-31-invalid-provider-approval-token",
        "invalid_provider_approval_token",
        suggestion_overrides={"approval_token": "CAT-APP-forgedtoken123"},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "sanitizer_rejected": True,
        },
    },
    _valid_case(
        "case-32-invalid-provider-claims-validated",
        "invalid_provider_claims_validated",
        suggestion_overrides={"status": "validated"},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "sanitizer_rejected": True,
        },
    },
    _valid_case(
        "case-33-invalid-provider-claims-tests-passed",
        "invalid_provider_claims_tests_passed",
        suggestion_overrides={"tests_passed": True, "test_results": ["all green"]},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "sanitizer_rejected": True,
        },
    },
    _valid_case(
        "case-34-invalid-provider-shell-command",
        "invalid_provider_shell_command",
        suggestion_overrides={"command": "rm -rf /"},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "sanitizer_rejected": True,
        },
    },
    _valid_case(
        "case-35-invalid-provider-raw-secret",
        "invalid_provider_raw_secret",
        suggestion_overrides={"rationale": "uses the stored key AKIA1234567890EXAMPLE"},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "sanitizer_rejected": True,
        },
    },
    _valid_case(
        "case-36-invalid-patch-targets-unchanged-line",
        "invalid_patch_targets_unchanged_line",
        diff=UNCHANGED_LINE_DIFF,
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["targets unchanged or deleted lines"],
        },
    },
    _valid_case(
        "case-37-invalid-patch-no-source-finding",
        "invalid_patch_no_source_finding",
        suggestion_overrides={"finding_id": "CA-REV-NONE"},
    ) | {
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 1,
            "suggestions_accepted": 0,
            "suggestions_rejected": 1,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": ["no source finding with id CA-REV-NONE"],
        },
    },
    {
        "case_id": "case-38-suggestions-disabled-by-default",
        "scenario": "suggestions_disabled_without_opt_in",
        "test_group": "invalid",
        "description": "Without the opt-in flag, provider suggestions are ignored entirely",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "config": {},
        "allow_patch_suggestions": False,
        "response": _response(suggestions=[_suggestion()]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 0,
            "suggestions_accepted": 0,
            "suggestions_rejected": 0,
            "proposals_created": 0,
            "expected_proposal_status": None,
            "rejection_reasons": [],
            "expect_suggestions_ignored": True,
        },
    },
    {
        "case_id": "case-39-invalid-duplicate-suggestion",
        "scenario": "invalid_duplicate_suggestion",
        "test_group": "invalid",
        "description": "Second identical suggestion for the same finding is rejected as duplicate",
        "before": {"src/app.py": BASE_APP},
        "after": {"src/app.py": BUGGY_APP},
        "config": {},
        "response": _response(suggestions=[
            _suggestion(suggestion_id="CA-SUG-001"),
            _suggestion(suggestion_id="CA-SUG-002"),
        ]),
        "expected": {
            "request_made": True,
            "expected_policy": "allowed",
            "suggestions_received": 2,
            "suggestions_accepted": 1,
            "suggestions_rejected": 1,
            "proposals_created": 1,
            "expected_proposal_status": "requires_human_approval",
            "rejection_reasons": ["duplicate suggestion"],
        },
    },
    {
        "case_id": "case-40-valid-patch-linked-to-deterministic-finding",
        "scenario": "valid_patch_linked_to_deterministic_finding",
        "test_group": "valid",
        "description": "Provider finding merges with deterministic sensitive-data finding; patch links through merged provenance",
        "before": {"src/service.py": SENSITIVE_SERVICE_BEFORE},
        "after": {"src/service.py": SENSITIVE_SERVICE},
        "config": {},
        "response": _response(
            findings=[_finding(
                id="CA-REV-SENSITIVE",
                file="src/service.py",
                start_line=2,
                end_line=2,
                severity="high",
                category="SENSITIVE_DATA_EXPOSURE",
                claim="A sensitive attribute flows into a print sink",
                impact="Sensitive value may be disclosed through logs",
                evidence=["print(user.password) on a changed line"],
            )],
            suggestions=[_suggestion(
                suggestion_id="CA-SUG-SENS",
                finding_id="CA-REV-SENSITIVE",
                unified_diff=SENSITIVE_REDACT_DIFF,
                target_files=["src/service.py"],
                rationale="Replace the print sink with a redacted placeholder",
            )],
        ),
        "expected": {
            "request_made": True,
            "expected_policy": "requires_human_approval",
            "suggestions_received": 1,
            "suggestions_accepted": 1,
            "suggestions_rejected": 0,
            "proposals_created": 1,
            "expected_proposal_status": "requires_human_approval",
            "rejection_reasons": [],
            "expect_merged_origin": True,
        },
    },
]


def write_cases() -> None:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        case_dir = CASES_DIR / case["case_id"]
        for sub in ("before", "after"):
            (case_dir / sub).mkdir(parents=True, exist_ok=True)
        for rel_path, content in case["before"].items():
            target = case_dir / "before" / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        for rel_path, content in case["after"].items():
            target = case_dir / "after" / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

        metadata = {
            "case_id": case["case_id"],
            "category": "live_patch",
            "scenario": case["scenario"],
            "test_group": "invalid" if case["scenario"].startswith("invalid_") else case["test_group"],
            "description": case["description"],
            "config": case.get("config", {}),
            "allow_patch_suggestions": case.get("allow_patch_suggestions", True),
            "expected": case["expected"],
        }
        (case_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )

        if case.get("response_text") is not None:
            (case_dir / "response.txt").write_text(case["response_text"], encoding="utf-8", newline="\n")
        elif "response" in case:
            (case_dir / "response.json").write_text(
                json.dumps(case["response"], indent=2) + "\n", encoding="utf-8", newline="\n"
            )


if __name__ == "__main__":
    write_cases()
    print(f"Wrote {len(CASES)} live-patch cases to {CASES_DIR}")
