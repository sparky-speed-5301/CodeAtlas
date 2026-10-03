"""Generate Phase 9C summary-comment fixtures and Phase 9D check-run fixtures.

eval/cases/github-summary/  (20 cases)  — consolidated summary comment behavior
eval/cases/github-checks/   (20 cases)  — check-run integration behavior

E2E cases reuse the Phase 9A/9B fixture shape (local repo trees + fake GitHub
transport); probe cases exercise the pure renderers/mappers directly.
"""

from __future__ import annotations

import json
from pathlib import Path

BASE_APP = "def f(x):\n    return x\n"
HEAD_APP = "def f(x):\n    token = 'AKIAIOSFODNN7EXAMPLE'\n    return x\n"

SUMMARY_DIR = Path(__file__).resolve().parent / "cases" / "github-summary"
CHECKS_DIR = Path(__file__).resolve().parent / "cases" / "github-checks"


def _e2e(case_id, scenario, **kwargs):
    expected = {
        "raises": None,
        "write_calls": 0,
        "summary_posted": 0,
        "summary_updated": 0,
        "inline_endpoint": 0,
        "issue_endpoint": 0,
        "check_created": 0,
        "check_updated": 0,
        "worktree_unchanged": True,
        "body_secret_free": True,
        "partial_write": False,
    }
    expected.update(kwargs.pop("expected", {}))
    base = {
        "case_id": case_id,
        "scenario": scenario,
        "base_tree": {"src/app.py": BASE_APP},
        "head_tree": {"src/app.py": HEAD_APP},
        "files": [{"filename": "src/app.py", "status": "modified"}],
        "existing_comments": [],
        "existing_check_runs": [],
        "post": False,
        "summary": True,
        "check": False,
        "inline": False,
        "transport_fault": None,
        "config": {},
        "expected": expected,
    }
    base.update(kwargs)
    return base


def _probe(case_id, scenario, fn, args, expected):
    return {
        "case_id": case_id,
        "scenario": scenario,
        "kind": "probe",
        "fn": fn,
        "args": args,
        "expected": expected,
    }


SUMMARY_CASES = [
    _e2e("case-01-clean-review-summary", "clean_review_summary_dry_run",
         head_tree={"src/app.py": "def f(x):\n    return x + 1\n"},
         expected={"comments_planned": 1}),
    _e2e("case-02-findings-by-severity", "findings_by_severity_summary",
         expected={"comments_planned": 1}),
    _e2e("case-03-no-findings-summary", "no_findings_summary",
         head_tree={"src/app.py": "def f(x):\n    return x + 1\n"},
         expected={"comments_planned": 1}),
    _e2e("case-04-same-head-update", "same_head_summary_updated_in_place",
         post=True,
         existing_comments=["__SUMMARY_MARKER__"],
         expected={"write_calls": 1, "summary_updated": 1, "summary_posted": 0}),
    _e2e("case-05-new-head-creates-new", "new_head_summary_created_not_overwritten",
         post=True,
         existing_comments=["__OTHER_HEAD_SUMMARY__"],
         expected={"write_calls": 1, "summary_posted": 1, "summary_updated": 0}),
    _e2e("case-06-duplicate-summaries", "duplicate_summaries_lowest_id_updated",
         post=True,
         existing_comments=["__SUMMARY_MARKER__", "__SUMMARY_MARKER__"],
         expected={"write_calls": 1, "summary_updated": 1}),
    _e2e("case-07-dry-run-zero-writes", "summary_dry_run_zero_writes",
         expected={"write_calls": 0, "comments_planned": 1}),
    _e2e("case-08-post-required", "summary_without_post_never_writes",
         expected={"write_calls": 0}),
    _e2e("case-09-sha-change", "summary_sha_change_before_write_aborts",
         post=True,
         transport_fault="sha_changed_write_check",
         expected={"raises": "SHAMismatchError", "write_calls": 0, "comments_planned": 0}),
    _e2e("case-10-redaction-failure", "summary_redaction_failure_blocks_write",
         post=True,
         transport_fault="summary_redaction",
         expected={"write_calls": 0, "summary_posted": 0, "comments_planned": 0}),
    _e2e("case-11-truncation", "summary_truncation_deterministic",
         config={"github": {"max_summary_bytes": 900}},
         post=True,
         expected={"write_calls": 1, "summary_posted": 1}),
    _e2e("case-12-mixed-inline-fallback-counts", "summary_planned_counts_accurate",
         inline=True,
         expected={"comments_planned": 2}),
    _probe("case-13-test-evidence", "summary_test_evidence_rendered",
           "render_summary_comment",
           {"repo_slug": "o/r", "pr_number": 1, "head_sha": "a" * 40, "base_sha": "b" * 40,
            "run_id": "run-1", "review_mode": "post", "policy_decision": "allowed", "findings": [],
            "tests_status": "passed", "test_pass_count": 12, "test_failure_count": 0},
           {"contains": ["Targeted tests: passed", "12 passed, 0 failed"]}),
    _probe("case-14-failed-tests", "summary_failed_tests_rendered",
           "render_summary_comment",
           {"repo_slug": "o/r", "pr_number": 1, "head_sha": "a" * 40, "base_sha": "b" * 40,
            "run_id": "run-1", "review_mode": "post", "policy_decision": "allowed", "findings": [],
            "tests_status": "failed", "test_failure_count": 3, "failed_test_names": ["test_a", "test_b"]},
           {"contains": ["Targeted tests: failed", "test_a", "test_b"]}),
    _probe("case-15-blocked-tests", "summary_blocked_tests_rendered",
           "render_summary_comment",
           {"repo_slug": "o/r", "pr_number": 1, "head_sha": "a" * 40, "base_sha": "b" * 40,
            "run_id": "run-1", "review_mode": "post", "policy_decision": "allowed", "findings": [],
            "full_suite_status": "blocked", "test_execution_attempted": False},
           {"contains": ["Full suite: blocked", "not proven"]}),
    _e2e("case-16-malformed-comments", "malformed_existing_comments_ignored",
         post=True,
         existing_comments=["not a marker", "", "codeatlas:summary broken"],
         expected={"write_calls": 1, "summary_posted": 1}),
    _e2e("case-17-rate-limit", "summary_rate_limit_write_fails",
         post=True,
         transport_fault="write_rate_limit",
         expected={"write_calls": 1, "summary_posted": 0}),
    _e2e("case-18-auth-failure", "summary_auth_failure_fails_closed",
         transport_fault="metadata_auth",
         expected={"raises": "GitHubAuthError", "write_calls": 0, "comments_planned": 0}),
    _e2e("case-19-original-repo-unchanged", "summary_original_repository_unchanged",
         post=True,
         expected={"write_calls": 1, "summary_posted": 1, "worktree_unchanged": True}),
    _e2e("case-20-combined-modes", "combined_summary_inline_post",
         post=True, inline=True,
         existing_comments=["__SUMMARY_MARKER__"],
         expected={"write_calls": 2, "summary_updated": 1, "inline_endpoint": 1, "comments_planned": 2}),
]

CHECK_CASES = [
    _probe("case-01-clean-success", "check_clean_review_success",
           "map_check_conclusion",
           {"policy_decision": "allowed", "findings": [], "tests_status": "passed",
            "full_suite_status": "passed"},
           {"conclusion": "success"}),
    _probe("case-02-findings-neutral", "check_findings_neutral",
           "map_check_conclusion",
           {"policy_decision": "requires_human_approval", "findings": [{"severity": "medium"}],
            "tests_status": None},
           {"conclusion": "neutral"}),
    _probe("case-03-blocker-action-required", "check_blocker_action_required",
           "map_check_conclusion",
           {"policy_decision": "requires_human_approval", "findings": [{"severity": "blocker"}]},
           {"conclusion": "action_required"}),
    _probe("case-04-test-failure", "check_test_failure_conclusion",
           "map_check_conclusion",
           {"policy_decision": "allowed", "findings": [], "tests_status": "failed"},
           {"conclusion": "failure"}),
    _probe("case-05-tests-not-run", "check_tests_not_run_neutral",
           "map_check_conclusion",
           {"policy_decision": "allowed", "findings": [], "tests_status": None},
           {"conclusion": "neutral"}),
    _probe("case-06-abstention", "check_abstention_neutral",
           "map_check_conclusion",
           {"policy_decision": "abstain", "findings": []},
           {"conclusion": "neutral"}),
    _e2e("case-07-provider-failure", "check_provider_failure_fails_closed",
         transport_fault="network",
         expected={"raises": "GitHubNetworkError", "write_calls": 0, "comments_planned": 0}),
    _e2e("case-08-same-head-update", "check_same_head_updated",
         post=True, check=True,
         existing_check_runs=[{"id": 77, "name": "CodeAtlas review", "head_sha": "__HEAD__",
                               "conclusion": "neutral", "external_id": "__EXTERNAL_ID__"}],
         expected={"write_calls": 1, "check_updated": 1, "check_created": 0}),
    _e2e("case-09-new-head-new-check", "check_new_head_created",
         post=True, check=True,
         existing_check_runs=[{"id": 77, "name": "CodeAtlas review", "head_sha": "c" * 40,
                               "conclusion": "neutral", "external_id": "codeatlas-check:pr=42:head=" + "c" * 40}],
         expected={"write_calls": 1, "check_created": 1, "check_updated": 0}),
    _e2e("case-10-dry-run", "check_dry_run_zero_writes",
         check=True,
         expected={"write_calls": 0, "comments_planned": 1}),
    _e2e("case-11-missing-permission", "check_auth_failure_fails_closed",
         transport_fault="metadata_auth",
         expected={"raises": "GitHubAuthError", "write_calls": 0, "comments_planned": 0}),
    _e2e("case-12-malformed-response", "check_malformed_metadata_rejected",
         transport_fault="metadata_malformed",
         expected={"raises": "GitHubResponseError", "write_calls": 0, "comments_planned": 0}),
    _e2e("case-13-rate-limit", "check_rate_limit_write_fails",
         post=True, check=True,
         transport_fault="write_rate_limit",
         expected={"write_calls": 0, "check_created": 0, "check_updated": 0, "comments_planned": 1}),
    _e2e("case-14-sha-change", "check_sha_change_before_write_aborts",
         post=True, check=True,
         transport_fault="sha_changed_write_check",
         expected={"raises": "SHAMismatchError", "write_calls": 0, "comments_planned": 0}),
    _e2e("case-15-redaction-failure", "check_redaction_failure_blocks_write",
         post=True, check=True,
         transport_fault="check_redaction",
         expected={"write_calls": 0, "check_created": 0, "comments_planned": 0}),
    _e2e("case-16-duplicate-checks", "check_duplicate_runs_lowest_id_updated",
         post=True, check=True,
         existing_check_runs=[
             {"id": 90, "name": "CodeAtlas review", "head_sha": "__HEAD__",
              "conclusion": "neutral", "external_id": "__EXTERNAL_ID__"},
             {"id": 80, "name": "CodeAtlas review", "head_sha": "__HEAD__",
              "conclusion": "neutral", "external_id": "__EXTERNAL_ID__"},
         ],
         expected={"write_calls": 1, "check_updated": 1}),
    _e2e("case-17-bounded-output", "check_summary_bounded",
         post=True, check=True,
         expected={"write_calls": 1, "check_created": 1}),
    _e2e("case-18-network-isolation-unverified", "check_network_isolation_reported_unverified",
         post=True, check=True,
         expected={"write_calls": 1, "check_created": 1}),
    _e2e("case-19-original-repo-unchanged", "check_original_repository_unchanged",
         post=True, check=True,
         expected={"write_calls": 1, "check_created": 1, "worktree_unchanged": True}),
    _e2e("case-20-combined-modes", "combined_summary_check_inline_write_order",
         post=True, check=True, inline=True, summary=True,
         expected={"write_calls": 3, "check_created": 1, "inline_endpoint": 1, "summary_posted": 1, "comments_planned": 3}),
]


def write_cases() -> None:
    for case_dir, cases in ((SUMMARY_DIR, SUMMARY_CASES), (CHECKS_DIR, CHECK_CASES)):
        case_dir.mkdir(parents=True, exist_ok=True)
        for case in cases:
            if case_dir is CHECKS_DIR and case["case_id"] != "case-20-combined-modes":
                # Check-run cases never include a summary comment; comments
                # and checks are separate integrations by design.
                case["summary"] = False
            path = case_dir / f"{case['case_id']}.json"
            path.write_text(json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    write_cases()
    print(f"Wrote {len(SUMMARY_CASES)} summary cases and {len(CHECK_CASES)} check cases")
