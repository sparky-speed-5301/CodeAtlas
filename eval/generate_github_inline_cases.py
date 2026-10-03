"""Generate Phase 9B inline-comment evaluation fixtures under eval/cases/github-inline/.

E2E cases mirror the Phase 9A fixture shape plus ``inline: true``; probe cases
exercise the pure line-mapping/classification function directly against a
fixture diff.  No network calls and no real GitHub writes occur.
"""

from __future__ import annotations

import json
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases" / "github-inline"

BASE_APP = "def f(x):\n    return x\n"
HEAD_APP = "def f(x):\n    token = 'AKIAIOSFODNN7EXAMPLE'\n    return x\n"

MULTI_LINE_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def f(x):\n"
    "+    a = 1\n"
    "+    b = 2\n"
    "+    c = 3\n"
    "     return x\n"
)

CONTEXT_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def f(x):\n"
    "+    a = 1\n"
    "     return x\n"
)

DELETED_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-def gone():\n"
    "+def here():\n"
    "     return 1\n"
)


def _e2e(case_id, scenario, **kwargs):
    expected = {
        "raises": None,
        "write_calls": 0,
        "comments_planned": 1,
        "comments_posted": 0,
        "inline_endpoint": 0,
        "issue_endpoint": 0,
        "duplicate_suppressed": 0,
        "worktree_unchanged": True,
        "body_secret_free": True,
    }
    expected.update(kwargs.pop("expected", {}))
    base = {
        "case_id": case_id,
        "scenario": scenario,
        "kind": "e2e",
        "base_tree": {"src/app.py": BASE_APP},
        "head_tree": {"src/app.py": HEAD_APP},
        "files": [{"filename": "src/app.py", "status": "modified"}],
        "existing_comments": [],
        "existing_inline_comments": [],
        "post": True,
        "inline": True,
        "transport_fault": None,
        "expected": expected,
    }
    base.update(kwargs)
    return base


def _probe(case_id, scenario, diff, finding, decision, reason=None, inline_details=None):
    return {
        "case_id": case_id,
        "scenario": scenario,
        "kind": "probe",
        "diff": diff,
        "finding": finding,
        "expected": {"decision": decision, "reason": reason, "inline_details": inline_details},
    }


CASES = [
    _e2e("case-01-inline-valid-finding-posted", "inline_valid_finding_posted",
         expected={"write_calls": 1, "comments_posted": 1, "inline_endpoint": 1, "issue_endpoint": 0}),
    _e2e("case-02-inline-dry-run-zero-writes", "inline_dry_run_zero_writes",
         post=False,
         expected={"write_calls": 0, "comments_planned": 1}),
    _e2e("case-03-inline-without-post-no-writes", "inline_without_post_no_writes",
         post=False,
         expected={"write_calls": 0}),
    _e2e("case-04-inline-duplicate-suppressed", "inline_duplicate_from_inline_suppressed",
         existing_inline_comments=["__POSTED_MARKER__"],
         expected={"write_calls": 0, "comments_planned": 0, "duplicate_suppressed": 1}),
    _e2e("case-05-cross-mode-duplicate-suppressed", "inline_duplicate_from_issue_suppressed",
         post=False,
         existing_comments=["__POSTED_MARKER__"],
         expected={"write_calls": 0, "comments_planned": 0, "duplicate_suppressed": 1}),
    _e2e("case-06-inline-sha-change-before-write", "inline_sha_change_before_write_aborts",
         transport_fault="sha_changed_write_check",
         expected={"raises": "SHAMismatchError", "write_calls": 0, "comments_planned": 0}),
    _e2e("case-07-inline-redaction", "inline_bodies_redacted",
         expected={"write_calls": 1, "comments_posted": 1, "inline_endpoint": 1, "body_secret_free": True}),
    _e2e("case-08-inline-repo-unchanged", "inline_original_repository_unchanged",
         expected={"write_calls": 1, "comments_posted": 1, "inline_endpoint": 1, "worktree_unchanged": True}),
    _e2e("case-09-issue-mode-unchanged", "issue_mode_unchanged_without_inline",
         inline=False,
         expected={"write_calls": 1, "comments_posted": 1, "inline_endpoint": 0, "issue_endpoint": 1}),
    _probe("case-10-probe-multi-line", "multi_line_finding_inline",
           MULTI_LINE_DIFF,
           {"file": "src/app.py", "start_line": 2, "end_line": 4},
           "inline", inline_details={"path": "src/app.py", "line": 4, "start_line": 2}),
    _probe("case-11-probe-unchanged-line", "unchanged_line_fallback",
           CONTEXT_DIFF,
           {"file": "src/app.py", "start_line": 1, "end_line": 1},
           "fallback", reason="unchanged_context_line"),
    _probe("case-12-probe-deleted-line", "deleted_line_suppressed",
           DELETED_DIFF,
           {"file": "src/app.py", "start_line": 99, "end_line": 99},
           "suppress", reason="line_outside_diff"),
    _probe("case-13-probe-ambiguous-mapping", "ambiguous_mapping_fallback",
           CONTEXT_DIFF,
           {"file": "src/app.py", "start_line": 1, "end_line": 2},
           "fallback", reason="ambiguous_line_mapping"),
    _probe("case-14-probe-file-not-in-diff", "unsupported_position_suppressed",
           CONTEXT_DIFF,
           {"file": "src/other.py", "start_line": 1, "end_line": 1},
           "suppress", reason="file_not_in_diff"),
]


def write_cases() -> None:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        path = CASES_DIR / f"{case['case_id']}.json"
        path.write_text(json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    write_cases()
    print(f"Wrote {len(CASES)} github-inline cases to {CASES_DIR}")
