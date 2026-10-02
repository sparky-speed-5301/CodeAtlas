"""Generate Phase 7C isolated-validation fixtures under eval/cases/isolated-validation/.

Each case contains:
  - metadata.json   token scenario, expectations (exit, lifecycle, approval, cleanup)
  - base/           git tree committed as the base (single commit)
  - proposal.json   a PatchProposal with base_commit placeholder "BASE_SHA"

Fixtures are executed by eval/eval_isolated_validation.py through the real CLI
(``codeatlas patch apply-isolated``) with computed approval tokens; no network
calls are made and the original repository is never modified.
"""

from __future__ import annotations

import json
from pathlib import Path

from codeatlas.patching.proposal import create_patch_proposal

CASES_DIR = Path(__file__).resolve().parent / "cases" / "isolated-validation"

APP_PY = "def run():\n    return 42\n"
UTIL_PY = "def helper():\n    return 7\n"
SERVICE_TS = 'export function load(): string {\n  return "data";\n}\n'
REQUIREMENTS = "requests==2.0.0\n"

BASE_TREE = {
    "src/app.py": APP_PY,
    "src/util.py": UTIL_PY,
    "src/service.ts": SERVICE_TS,
    "requirements.txt": REQUIREMENTS,
}


def ctx(text: str) -> str:
    return " " + text


SAFE_COMMENT_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -1,2 +1,3 @@",
    ctx("def run():"),
    "+    # guarded return",
    ctx("    return 42"),
]) + "\n"

MULTI_HUNK_BASE_APP = (
    "def run():\n"
    "    a = 1\n"
    "    b = 2\n"
    "    c = 3\n"
    "    d = 4\n"
    "    return 42\n"
)

MULTI_HUNK_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -1,3 +1,4 @@",
    ctx("def run():"),
    "+    # head comment",
    ctx("    a = 1"),
    ctx("    b = 2"),
    "@@ -4,3 +5,4 @@",
    ctx("    c = 3"),
    ctx("    d = 4"),
    "+    # tail comment",
    ctx("    return 42"),
]) + "\n"

EXTRA_CONTEXT_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -1,2 +1,3 @@",
    ctx("def run():"),
    "+    # guarded return",
    ctx("    return 42"),
]) + "\n"

TWO_FILE_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -1,2 +1,3 @@",
    ctx("def run():"),
    "+    # guarded return",
    ctx("    return 42"),
    "--- a/src/util.py",
    "+++ b/src/util.py",
    "@@ -1,2 +1,3 @@",
    ctx("def helper():"),
    "+    # helper note",
    ctx("    return 7"),
]) + "\n"

TS_DIFF = "\n".join([
    "--- a/src/service.ts",
    "+++ b/src/service.ts",
    "@@ -1,3 +1,4 @@",
    ctx("export function load(): string {"),
    "+  // guarded load",
    ctx('  return "data";'),
    ctx("}"),
]) + "\n"

CONFLICT_DIFF = (
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -999,2 +999,1 @@\n"
    "-nonexistent_one()\n-nonexistent_two()\n+replaced()\n"
)

SYNTAX_BREAK_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -1,2 +1,3 @@",
    ctx("def run():"),
    "+    def broken(:",
    ctx("    return 42"),
]) + "\n"

REQUIREMENTS_DIFF = "\n".join([
    "--- a/requirements.txt",
    "+++ b/requirements.txt",
    "@@ -1 +1,2 @@",
    ctx("requests==2.0.0"),
    "+flask==1.0.0",
]) + "\n"

SECRET_DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -1,2 +1,3 @@",
    ctx("def run():"),
    "+    api_key = 'supersecretkey123456'",
    ctx("    return 42"),
]) + "\n"

TRAVERSAL_DIFF = "\n".join([
    "--- ../../etc/passwd",
    "+++ b/src/app.py",
    "@@ -1,2 +1,3 @@",
    ctx("def run():"),
    "+    # guarded",
    ctx("    return 42"),
]) + "\n"

ABSOLUTE_DIFF = "\n".join([
    "--- /etc/passwd",
    "+++ b/src/app.py",
    "@@ -1,2 +1,3 @@",
    ctx("def run():"),
    "+    # guarded",
    ctx("    return 42"),
]) + "\n"


def _proposal(diff: str, target_files: list[str], *, finding_id: str = "F-7C", status: str = "proposed",
              provenance: dict | None = None, base_commit: str = "BASE_SHA") -> dict:
    prop = create_patch_proposal(
        finding_id=finding_id,
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_commit,
        target_files=target_files,
        unified_diff=diff,
        rationale="Operator-approved isolated validation fixture",
        expected_behavior="Applies cleanly with valid syntax",
    )
    prop.status = status
    if provenance:
        prop.provenance.update(provenance)
    return prop.model_dump(mode="json")


def _case(case_id, scenario, proposal, *, token="valid", run_id="default", base_commit="real",
          proposal_file="normal", dirty_worktree=False, retain=False, base_tree=None, **expected):
    base_tree = base_tree if base_tree is not None else BASE_TREE
    base_expected = {
        "exit": 0 if expected.get("valid", False) else 1,
        "worktree_unchanged": True,
        "tests_not_run": True,
        "builds_not_run": True,
    }
    base_expected.update(expected)
    return {
        "case_id": case_id,
        "scenario": scenario,
        "title": scenario,
        "token": token,
        "run_id": run_id,
        "base_commit": base_commit,
        "proposal_file": proposal_file,
        "dirty_worktree": dirty_worktree,
        "retain_sandbox_on_failure": retain,
        "proposal": proposal,
        "expected": base_expected,
        "base_tree": base_tree,
    }


PROVIDER_FORGED_TOKEN = "CAT-APP-providerforgedtoken0000000000"

CASES = [
    # ---- Valid isolated validations ----
    _case("case-01-valid-python-patch", "valid_python_patch",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          valid=True, status="validated", approval_verified=True, applies_cleanly=True,
          syntax_valid=True, cleanup_status="completed"),
    _case("case-02-valid-typescript-patch", "valid_typescript_patch",
          _proposal(TS_DIFF, ["src/service.ts"]),
          valid=True, status="validated", approval_verified=True, applies_cleanly=True,
          syntax_valid=True, cleanup_status="completed"),
    _case("case-03-valid-multi-hunk", "valid_multi_hunk",
          _proposal(MULTI_HUNK_DIFF, ["src/app.py"]),
          base_tree={**BASE_TREE, "src/app.py": MULTI_HUNK_BASE_APP},
          valid=True, status="validated", approval_verified=True, applies_cleanly=True,
          syntax_valid=True, cleanup_status="completed"),
    _case("case-04-valid-unchanged-context", "valid_unchanged_context",
          _proposal(EXTRA_CONTEXT_DIFF, ["src/app.py"]),
          valid=True, status="validated", approval_verified=True, applies_cleanly=True,
          syntax_valid=True, cleanup_status="completed"),
    _case("case-05-valid-two-file-patch", "valid_two_file_patch",
          _proposal(TWO_FILE_DIFF, ["src/app.py", "src/util.py"]),
          valid=True, status="validated", approval_verified=True, applies_cleanly=True,
          syntax_valid=True, cleanup_status="completed"),
    _case("case-06-valid-scoped-run-id", "valid_scoped_run_id",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          token="valid", run_id="run-custom",
          valid=True, status="validated", approval_verified=True, applies_cleanly=True,
          syntax_valid=True, cleanup_status="completed"),
    _case("case-24-dirty-original-worktree", "dirty_original_worktree_containment",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          dirty_worktree=True,
          valid=True, status="validated", approval_verified=True, applies_cleanly=True,
          syntax_valid=True, cleanup_status="completed"),

    # ---- Approval failures ----
    _case("case-07-missing-token", "missing_approval_token",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          token="missing",
          valid=False, status="requires_human_approval", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-08-malformed-token", "malformed_approval_token",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          token="malformed",
          valid=False, status="failed_validation", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-09-token-other-proposal", "cross_proposal_token_rejected",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          token="other_proposal",
          valid=False, status="failed_validation", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-10-token-other-base-commit", "cross_commit_token_rejected",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          token="other_commit",
          valid=False, status="failed_validation", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-11-token-other-patch-hash", "cross_hash_token_rejected",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          token="other_hash",
          valid=False, status="failed_validation", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-12-token-other-target-files", "cross_target_token_rejected",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"]),
          token="other_files",
          valid=False, status="failed_validation", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-25-provider-token-in-provenance", "provider_token_never_accepted",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"],
                    provenance={"provider_approval_token": PROVIDER_FORGED_TOKEN}),
          token="missing",
          valid=False, status="requires_human_approval", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-26-provider-token-as-cli-token", "provider_forged_token_rejected",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"],
                    provenance={"provider_approval_token": PROVIDER_FORGED_TOKEN}),
          token="provider_embedded_used",
          valid=False, status="failed_validation", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),

    # ---- Lifecycle state guards ----
    _case("case-13-rejected-proposal", "rejected_proposal_blocked",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"], status="rejected"),
          valid=False, status="rejected", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-14-already-validated-proposal", "already_validated_blocked",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"], status="validated"),
          valid=False, status="validated", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-15-already-applied-proposal", "already_applied_blocked",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"], status="applied_in_isolated_worktree"),
          valid=False, status="applied_in_isolated_worktree", approval_verified=False,
          applies_cleanly=False, syntax_valid=None, cleanup_status="not_applicable"),

    # ---- Structural and content failures ----
    _case("case-16-stale-base-commit", "stale_base_commit_rejected",
          _proposal(SAFE_COMMENT_DIFF, ["src/app.py"], base_commit="0000000000000000000000000000000000000000"),
          base_commit="stale",
          valid=False, status="failed_validation", approval_verified=True, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-17-patch-conflict", "patch_conflict_rejected",
          _proposal(CONFLICT_DIFF, ["src/app.py"]),
          valid=False, status="failed_validation", approval_verified=True, applies_cleanly=False,
          syntax_valid=None, cleanup_status="completed"),
    _case("case-18-syntax-breaking-python", "syntax_breaking_patch_rejected",
          _proposal(SYNTAX_BREAK_DIFF, ["src/app.py"]),
          valid=False, status="failed_validation", approval_verified=True, applies_cleanly=True,
          syntax_valid=False, cleanup_status="completed"),
    _case("case-19-protected-path", "protected_path_rejected",
          _proposal(REQUIREMENTS_DIFF, ["requirements.txt"]),
          valid=False, status="rejected", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-20-secret-introducing-patch", "secret_introducing_patch_rejected",
          _proposal(SECRET_DIFF, ["src/app.py"]),
          valid=False, status="rejected", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-21-path-traversal", "path_traversal_rejected",
          _proposal(TRAVERSAL_DIFF, ["src/app.py"]),
          valid=False, status="rejected", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-22-absolute-path", "absolute_path_rejected",
          _proposal(ABSOLUTE_DIFF, ["src/app.py"]),
          valid=False, status="rejected", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),
    _case("case-23-oversized-patch", "oversized_patch_rejected",
          _proposal(
              "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,203 @@\n"
              + ctx("def run():")
              + "\n" + ("+    # " + "x" * 3000 + "\n") * 201
              + ctx("    return 42") + "\n",
              ["src/app.py"]),
          valid=False, status="rejected", approval_verified=False, applies_cleanly=False,
          syntax_valid=None, cleanup_status="not_applicable"),

    # ---- Retention and CLI-level failures ----
    _case("case-29-retain-sandbox-on-failure", "retain_sandbox_on_failure",
          _proposal(SYNTAX_BREAK_DIFF, ["src/app.py"]),
          retain=True,
          valid=False, status="failed_validation", approval_verified=True, applies_cleanly=True,
          syntax_valid=False, cleanup_status="retained"),
    _case("case-27-missing-proposal-file", "missing_proposal_file",
          None,
          proposal_file="missing",
          valid=False, status=None, approval_verified=None, applies_cleanly=None,
          syntax_valid=None, cleanup_status=None),
    _case("case-28-malformed-proposal-file", "malformed_proposal_file",
          None,
          proposal_file="malformed",
          valid=False, status=None, approval_verified=None, applies_cleanly=None,
          syntax_valid=None, cleanup_status=None),
]


def write_cases() -> None:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        case_dir = CASES_DIR / case["case_id"]
        (case_dir / "base" / "src").mkdir(parents=True, exist_ok=True)
        for rel_path, content in case["base_tree"].items():
            (case_dir / "base" / rel_path).write_text(content, encoding="utf-8", newline="\n")

        metadata = {
            "case_id": case["case_id"],
            "category": "isolated_validation",
            "scenario": case["scenario"],
            "title": case["title"],
            "token": case["token"],
            "run_id": case["run_id"],
            "base_commit": case["base_commit"],
            "proposal_file": case["proposal_file"],
            "dirty_worktree": case["dirty_worktree"],
            "retain_sandbox_on_failure": case["retain_sandbox_on_failure"],
            "expected": case["expected"],
        }
        (case_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        if case["proposal"] is not None:
            (case_dir / "proposal.json").write_text(
                json.dumps(case["proposal"], indent=2) + "\n", encoding="utf-8", newline="\n"
            )


if __name__ == "__main__":
    write_cases()
    print(f"Wrote {len(CASES)} isolated-validation cases to {CASES_DIR}")
