"""Generate Phase 9A GitHub-integration evaluation fixtures under eval/cases/github/.

Each case is a single JSON file describing: local repo trees (base/head),
GitHub transport responses (metadata placeholder SHAs, PR files, existing
comments), optional transport faults, mode, and expectations.  The runner in
eval/eval_github.py builds an isolated local Git repository per case and
drives run_github_pr_review with a FakeGitHubTransport.  No network calls and
no real GitHub writes occur.
"""

from __future__ import annotations

import json
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases" / "github"

BASE_APP = "def f(x):\n    return x\n"
HEAD_APP = "def f(x):\n    token = 'AKIAIOSFODNN7EXAMPLE'\n    return x\n"


def _case(case_id, scenario, **kwargs):
    base = {
        "case_id": case_id,
        "scenario": scenario,
        "base_tree": {"src/app.py": BASE_APP},
        "head_tree": {"src/app.py": HEAD_APP},
        "files": [{"filename": "src/app.py", "status": "modified"}],
        "existing_comments": [],
        "transport_fault": None,
        "post": False,
        "parse_raw": None,
        "expected": {
            "raises": None,
            "write_calls": 0,
            "comments_planned": 1,
            "comments_posted": 0,
            "duplicate_suppressed": 0,
            "worktree_unchanged": True,
            "body_secret_free": True,
            "diff_fetched": False,
            "metadata_fetched": False,
        },
    }
    base["expected"].update(kwargs.pop("expected", {}))
    base.update(kwargs)
    return base


CASES = [
    _case("case-01-pr-metadata-fetch", "pr_metadata_fetch",
          expected={"metadata_fetched": True}),
    _case("case-02-diff-fetch", "diff_fetch",
          expected={"diff_fetched": True}),
    _case("case-03-sha-mismatch", "sha_mismatch_fails_closed",
          transport_fault="sha_mismatch",
          expected={"raises": "SHAMismatchError", "comments_planned": 0}),
    _case("case-04-malformed-response", "malformed_response_rejected",
          parse_raw="this is definitely not JSON {{{",
          expected={"raises": "GitHubResponseError", "comments_planned": 0}),
    _case("case-05-missing-authentication", "missing_authentication_fails_closed",
          transport_fault="missing_auth",
          expected={"raises": "GitHubAuthError", "comments_planned": 0}),
    _case("case-06-dry-run-zero-writes", "dry_run_zero_write_calls",
          expected={"write_calls": 0, "comments_planned": 1}),
    _case("case-07-valid-anchored-finding", "valid_anchored_finding_posted",
          post=True,
          expected={"write_calls": 1, "comments_posted": 1}),
    _case("case-08-invalid-file-line-finding", "invalid_file_line_not_anchored",
          expected={"comments_planned": 0, "raises": None},
          anchor_probe={"file": "src/app.py", "start_line": 50, "end_line": 51, "ranges_file": "src/app.py",
                        "ranges": [[2, 3]]}),
    _case("case-09-duplicate-comment-suppression", "duplicate_comment_suppressed",
          post=True,
          existing_comments=["__POSTED_MARKER__"],
          expected={"write_calls": 0, "comments_planned": 0, "duplicate_suppressed": 1,
                    "comments_posted": 0}),
    _case("case-10-redaction", "comment_bodies_redacted",
          post=True,
          expected={"write_calls": 1, "comments_posted": 1, "body_secret_free": True}),
    _case("case-11-network-failure", "network_failure_fails_closed",
          transport_fault="network",
          expected={"raises": "GitHubNetworkError", "comments_planned": 0}),
    _case("case-12-provider-failure", "transport_failure_fails_closed",
          transport_fault="diff",
          expected={"raises": "GitHubNetworkError", "comments_planned": 0}),
    _case("case-13-original-repo-unchanged", "original_repository_unchanged",
          post=True,
          expected={"write_calls": 1, "comments_posted": 1, "worktree_unchanged": True}),
    _case("case-14-sha-changed-mid-run", "sha_change_during_run_fails_closed",
          transport_fault="sha_changed",
          post=True,
          expected={"raises": "SHAMismatchError", "write_calls": 0, "comments_planned": 0}),
]


def write_cases() -> None:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        path = CASES_DIR / f"{case['case_id']}.json"
        path.write_text(json.dumps(case, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    write_cases()
    print(f"Wrote {len(CASES)} github cases to {CASES_DIR}")
