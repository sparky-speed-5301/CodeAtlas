"""Tests for Phase 9C (summary comment) and 9D (check run) — fake transport only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.github import (
    CHECK_NAME,
    FakeGitHubTransport,
    SHAMismatchError,
    parse_summary_marker,
    render_summary_comment,
    run_github_pr_review,
)
from codeatlas.github.checks import check_external_id, map_check_conclusion
from codeatlas.github.summary import summary_marker

cli_runner = CliRunner()

BASE_APP = "def f(x):\n    return x\n"
HEAD_APP = "def f(x):\n    token = 'AKIAIOSFODNN7EXAMPLE'\n    return x\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _build_pr_repo(root: Path) -> tuple[str, str]:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(BASE_APP, encoding="utf-8")

    def commit_all(msg: str) -> str:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", msg)
        return _git(root, "rev-parse", "HEAD")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.invalid")
    _git(root, "config", "user.name", "t")
    base_sha = commit_all("base")
    (root / "src" / "app.py").write_text(HEAD_APP, encoding="utf-8")
    return base_sha, commit_all("head")


def _transport(root: Path, base_sha: str, head_sha: str, **kwargs) -> FakeGitHubTransport:
    defaults: dict = {
        "metadata": {"number": 42, "base_sha": base_sha, "head_sha": head_sha},
        "files": [{"filename": "src/app.py"}],
        "diff_text": "--- a/src/app.py\n+++ b/src/app.py\n",
        "comments": [],
    }
    defaults.update(kwargs)
    return FakeGitHubTransport(**defaults)


def _tree_hash(work: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for p in sorted(work.rglob("*")):
        if ".git" in p.relative_to(work).parts or "__pycache__" in p.parts:
            continue
        if p.is_file():
            digest.update(str(p.relative_to(work)).encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Summary rendering (pure functions)
# ---------------------------------------------------------------------------

class TestSummaryRendering:
    def test_deterministic_rendering(self):
        kwargs = dict(
            repo_slug="o/r", pr_number=1, head_sha="a" * 40, base_sha="b" * 40, run_id="run-1",
            review_mode="dry_run", policy_decision="requires_human_approval",
            findings=[{"id": "CA-1", "severity": "high", "category": "SEC", "claim": "c",
                       "file": "a.py", "start_line": 1, "end_line": 2, "status": "detected",
                       "provenance": {"origin": "deterministic"}}],
        )
        body1, t1 = render_summary_comment(**kwargs)
        body2, t2 = render_summary_comment(**kwargs)
        assert body1 == body2 and not t1 and not t2

    def test_marker_present_and_parseable(self):
        body, _ = render_summary_comment(
            repo_slug="o/r", pr_number=7, head_sha="a" * 40, base_sha="b" * 40, run_id="run-1",
            review_mode="post", policy_decision="allowed", findings=[],
        )
        marker = parse_summary_marker(body)
        assert marker is not None
        assert marker["pr"] == "7" and marker["head"] == "a" * 40 and marker["mode"] == "summary"
        assert summary_marker(7, "a" * 40) in body

    def test_bounded_and_truncation_recorded(self):
        findings = [
            {"id": f"CA-{i}", "severity": "low", "category": "SEC", "claim": "x" * 2000,
             "file": "a.py", "start_line": i, "end_line": i, "status": "detected",
             "provenance": {"origin": "reviewer"}}
            for i in range(200)
        ]
        body, truncated = render_summary_comment(
            repo_slug="o/r", pr_number=1, head_sha="a" * 40, base_sha="b" * 40, run_id="run-1",
            review_mode="dry_run", policy_decision="requires_human_approval", findings=findings,
        )
        assert len(body.encode("utf-8")) <= 50_000
        assert truncated
        assert "truncated deterministically" in body

    def test_no_secrets_or_paths_in_body(self):
        body, _ = render_summary_comment(
            repo_slug="o/r", pr_number=1, head_sha="a" * 40, base_sha="b" * 40, run_id="run-1",
            review_mode="dry_run", policy_decision="allowed", findings=[],
            evidence_limitations=["local path C:\\Users\\secret\\proj was scanned"],
        )
        assert "AKIA" not in body
        assert "C:\\Users" not in body  # private local paths are excluded
        assert "not proven" in body  # no correctness claims

    def test_test_evidence_rendered(self):
        body, _ = render_summary_comment(
            repo_slug="o/r", pr_number=1, head_sha="a" * 40, base_sha="b" * 40, run_id="run-1",
            review_mode="dry_run", policy_decision="allowed", findings=[],
            tests_status="passed", full_suite_status="failed",
            test_pass_count=12, test_failure_count=2, failed_test_names=["test_a", "test_b"],
        )
        assert "Targeted tests: passed" in body
        assert "Full suite: failed" in body
        assert "12 passed, 2 failed" in body
        assert "test_a" in body


# ---------------------------------------------------------------------------
# Check conclusion mapping (pure function)
# ---------------------------------------------------------------------------

class TestConclusionMapping:
    def test_clean_with_tests_passed_is_success(self):
        assert map_check_conclusion(policy_decision="allowed", findings=[], tests_status="passed") == "success"

    def test_clean_without_tests_is_neutral(self):
        assert map_check_conclusion(policy_decision="allowed", findings=[], tests_status=None) == "neutral"

    def test_findings_are_neutral(self):
        assert map_check_conclusion(
            policy_decision="requires_human_approval",
            findings=[{"severity": "medium"}],
        ) == "neutral"

    def test_blocker_is_action_required(self):
        assert map_check_conclusion(
            policy_decision="requires_human_approval",
            findings=[{"severity": "blocker"}],
        ) == "action_required"

    def test_policy_blocked_is_failure(self):
        assert map_check_conclusion(policy_decision="blocked", findings=[]) == "failure"

    def test_test_failure_is_failure(self):
        assert map_check_conclusion(policy_decision="allowed", findings=[], tests_status="failed") == "failure"

    def test_abstention_is_neutral(self):
        assert map_check_conclusion(policy_decision="abstain", findings=[]) == "neutral"

    def test_run_errors_are_failure(self):
        assert map_check_conclusion(policy_decision="allowed", findings=[], run_errors=["boom"]) == "failure"


# ---------------------------------------------------------------------------
# End-to-end summary comment behavior
# ---------------------------------------------------------------------------

def test_summary_dry_run_zero_writes(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, summary_comment=True
    )
    assert transport.write_calls == 0
    assert report.summary_comment_requested is True
    assert report.summary_comment_write_attempted is False
    assert report.mode.endswith("+summary")


def test_summary_posted_when_new(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, summary_comment=True, post=True
    )
    assert transport.write_calls == 1
    assert report.summary_comment_posted is True
    assert report.summary_comment_updated is False
    assert report.summary_comment_write_succeeded is True
    body = transport._comments[0].body
    assert parse_summary_marker(body) is not None
    assert "AKIAIOSFODNN7EXAMPLE" not in body


def test_summary_updated_in_place_same_head(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    first = _transport(tmp_path / "work", base_sha, head_sha)
    run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=first, summary_comment=True, post=True)

    second = _transport(
        tmp_path / "work", base_sha, head_sha,
        comments=[{"comment_id": 10, "body": first._comments[0].body}],
    )
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=second, summary_comment=True, post=True
    )
    assert second.write_calls == 1
    assert second.calls[-1][0] == "update_comment"
    assert report.summary_comment_updated is True
    assert report.summary_comment_posted is False


def test_summary_new_head_creates_new_comment(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    other_head = "c" * 40
    transport = _transport(
        tmp_path / "work", base_sha, head_sha,
        comments=[{"comment_id": 10, "body": summary_marker(42, other_head)}],
    )
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, summary_comment=True, post=True
    )
    # The other head's summary is never overwritten.
    assert report.summary_comment_posted is True
    assert transport._comments[-1].body != transport._comments[0].body


def test_summary_duplicate_selects_lowest_id(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    marker_body = "x\n" + summary_marker(42, head_sha)
    transport = _transport(
        tmp_path / "work", base_sha, head_sha,
        comments=[
            {"comment_id": 30, "body": marker_body},
            {"comment_id": 11, "body": marker_body},
        ],
    )
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, summary_comment=True, post=True
    )
    assert report.summary_comment_updated is True
    assert report.summary_comment_skipped_duplicate is True
    # Lowest comment ID (11) was updated, not 30.
    updated_id = [c for (m, c) in transport.calls if m == "update_comment"][0][2]
    assert updated_id == 11


def test_summary_redaction_failure_blocks_write(tmp_path: Path, monkeypatch):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    import codeatlas.github.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "_contains_raw_secret", lambda text: "CodeAtlas Review" in text)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, summary_comment=True, post=True
    )
    assert transport.write_calls == 0
    assert report.summary_comment_write_succeeded is False
    assert report.summary_comment_redaction_safe is False
    assert "no write performed" in report.summary_comment_failure_reason


# ---------------------------------------------------------------------------
# End-to-end check-run behavior
# ---------------------------------------------------------------------------

def test_check_run_dry_run_reports_computed_status_only(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, check_run=True
    )
    assert transport.write_calls == 0
    assert report.check_run_requested is True
    assert report.check_run_conclusion == "neutral"  # findings present, tests not run
    assert report.check_run_created is False


def test_check_run_created_when_new(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, check_run=True, post=True
    )
    assert transport.write_calls == 1
    assert transport.calls[-1][0] == "create_check_run"
    assert report.check_run_created is True
    assert report.check_run_conclusion == "neutral"
    assert report.check_run_head_sha == head_sha
    # Bounded, redacted, no correctness claims.
    summary = transport.check_runs[0]["summary"]
    assert len(summary.encode("utf-8")) <= 50_000
    assert "never approves" in summary
    assert "AKIAIOSFODNN7EXAMPLE" not in summary
    # External identity binds PR and head SHA.
    assert transport.check_runs[0]["external_id"] == check_external_id(42, head_sha)


def test_check_run_updated_same_head(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(
        tmp_path / "work", base_sha, head_sha,
        check_runs=[{
            "id": 501, "name": CHECK_NAME, "head_sha": head_sha, "status": "completed",
            "conclusion": "neutral", "external_id": check_external_id(42, head_sha),
        }],
    )
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport, check_run=True, post=True
    )
    assert transport.calls[-1][0] == "update_check_run"
    assert report.check_run_updated is True
    assert report.check_run_created is False
    assert report.check_run_id == 501


def test_check_run_no_verdict_submission(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    run_github_pr_review("o/r", 42, local_repo=tmp_path / "work", transport=transport, check_run=True, post=True)
    # No review-submission or approval call exists anywhere.
    assert all("review" not in m or m == "get_pr_metadata" for m, _ in transport.calls if m.startswith(("submit", "approve")))
    assert all(m in {"get_pr_metadata", "get_pr_files", "get_pr_diff", "list_comments",
                     "list_inline_comments", "list_check_runs", "create_check_run", "post_comment",
                     "post_inline_comment", "update_comment", "update_check_run"}
               for m, _ in transport.calls)


def test_check_run_sha_change_before_write(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha, head_sha_on_write_check="d" * 40)
    with pytest.raises(SHAMismatchError):
        run_github_pr_review(
            "o/r", 42, local_repo=tmp_path / "work", transport=transport, check_run=True, post=True
        )
    assert transport.write_calls == 0


# ---------------------------------------------------------------------------
# Combined modes and partial writes
# ---------------------------------------------------------------------------

def test_combined_summary_inline_check_write_order(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport,
        summary_comment=True, inline=True, check_run=True, post=True,
    )
    order = [m for m, _ in transport.calls]
    # summary -> inline comment -> check run
    assert order.index("update_comment") < order.index("post_inline_comment") < order.index("create_check_run") \
        if "update_comment" in order else order.index("post_comment") < order.index("post_inline_comment") < order.index("create_check_run")
    assert report.write_calls == 3
    assert transport.write_calls == 3


def test_partial_write_reported_when_check_fails(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha, check_run_error=SHAMismatchError("forced"))
    # Use a non-SHA transport error for the check group; SHAMismatchError is a GitHubError subclass.
    transport = _transport(
        tmp_path / "work", base_sha, head_sha,
        check_run_error=Exception.__new__(SHAMismatchError) if False else __import__(
            "codeatlas.github.errors", fromlist=["GitHubNetworkError"]
        ).GitHubNetworkError("check API down"),
    )
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport,
        summary_comment=True, check_run=True, post=True,
    )
    # Summary succeeded; check failed; partial success reported honestly.
    assert report.summary_comment_write_succeeded is True
    assert report.check_run_created is False
    assert any("Check-run write failed" in e for e in report.errors)
    assert transport.write_calls == 1
    # No rollback of the successful comment.
    assert len(transport._comments) == 1


def test_sha_change_between_groups_stops_remaining_writes(tmp_path: Path):
    """A SHA change after the first write group leaves a partial-write report."""
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    # The SHA flips after the first write (the summary), so the check-run
    # group's gate stops the remaining writes with a partial report.
    transport = _transport(tmp_path / "work", base_sha, head_sha, head_sha_after_writes=1)
    report = run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport,
        summary_comment=True, check_run=True, post=True,
    )
    # Summary wrote first; SHA changed before the check-run group gate.
    assert report.summary_comment_write_succeeded is True
    assert report.check_run_write_attempted is False
    assert report.partial_write is True
    assert transport.write_calls == 1


def test_config_gate_blocks_post(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    (tmp_path / "work" / ".codeatlas.yml").write_text("github:\n  allow_post: false\n", encoding="utf-8")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    from codeatlas.github import GitHubError

    with pytest.raises(GitHubError, match="allow_post"):
        run_github_pr_review(
            "o/r", 42, local_repo=tmp_path / "work", transport=transport, summary_comment=True, post=True
        )
    assert transport.write_calls == 0


def test_original_repository_unchanged_combined(tmp_path: Path):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    tree_before = _tree_hash(tmp_path / "work")
    run_github_pr_review(
        "o/r", 42, local_repo=tmp_path / "work", transport=transport,
        summary_comment=True, inline=True, check_run=True, post=True,
    )
    assert _tree_hash(tmp_path / "work") == tree_before
    assert _git(tmp_path / "work", "status", "--porcelain") == ""


def test_cli_summary_and_check_run(tmp_path: Path, monkeypatch):
    base_sha, head_sha = _build_pr_repo(tmp_path / "work")
    transport = _transport(tmp_path / "work", base_sha, head_sha)
    monkeypatch.setattr("codeatlas.github.adapter.GhCliTransport", lambda: transport)
    result = cli_runner.invoke(app, [
        "github", "review", "--repo", "o/r", "--pr", "42",
        "--local-repo", str(tmp_path / "work"), "--summary-comment", "--check-run", "--post",
    ])
    assert result.exit_code == 0, result.output
    assert "Check run:        neutral" in result.output
    assert "Summary comment:  posted" in result.output
    assert transport.write_calls == 2


def test_cli_post_dry_run_error_still_applies():
    result = cli_runner.invoke(app, [
        "github", "review", "--repo", "o/r", "--pr", "1", "--summary-comment", "--post", "--dry-run",
    ])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
