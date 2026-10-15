"""Deterministic PR lifecycle simulation; no webhook or hosted Actions claims."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from codeatlas.github import FakeGitHubTransport, SHAMismatchError, run_github_pr_review
from codeatlas.github.adapter import classify_inline_target
from codeatlas.github.summary import parse_summary_marker
from codeatlas.orchestrator.review import run_review
from codeatlas.service.models import ReviewCreateRequest
from codeatlas.service.state import ReviewStateManager

ROOT = Path(__file__).resolve().parents[2]
SECRET = "AKIAIOSFODNN7EXAMPLE"
WRITE_METHODS = {"post_comment", "post_inline_comment", "update_comment", "create_check_run", "update_check_run"}


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def repository(repo):
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "GitHub harness")
    source = repo / "src/app.py"
    source.write_text("def f(x):\n    return x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    source.write_text(f"def f(x):\n    token = '{SECRET}'\n    return x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "opened")
    return base, git(repo, "rev-parse", "HEAD")


def transport(repo, base, head, **kwargs):
    return FakeGitHubTransport(
        metadata={"number": 42, "state": "OPEN", "base_sha": base, "head_sha": head},
        files=[{"filename": "src/app.py"}], diff_text=git(repo, "diff", base, head), **kwargs,
    )


def wait(manager, run):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = manager.get_review_status(run)
        if status.status in {"completed", "failed", "cancelled"}:
            return status
        time.sleep(.01)
    raise AssertionError("Review completion deadline exceeded")


def review(repo, remote, artifact, **kwargs):
    return run_github_pr_review(
        "owner/repo", 42, local_repo=repo, transport=remote,
        summary_comment=True, check_run=True, json_output=artifact, **kwargs,
    )


def test_opened_findings_publication_sync_cancel_and_update(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    base, head = repository(repo)
    opened = transport(repo, base, head)
    artifact = tmp_path / "opened.json"
    first = review(repo, opened, artifact, post=True, inline=True)
    assert first.findings_total > 0 and first.findings_anchored > 0
    assert first.inline_comments_posted > 0
    assert first.summary_comment_posted and first.check_run_created
    assert first.write_calls == opened.write_calls == first.inline_comments_posted + 2
    assert json.loads(artifact.read_text(encoding="utf-8")) == first.model_dump(mode="json")
    assert SECRET not in artifact.read_text(encoding="utf-8")
    assert all(SECRET not in body for body in opened.posted_bodies)
    check_id = first.check_run_id
    summary_id = first.summary_comment_id
    rerun = review(repo, opened, tmp_path / "rerun.json", post=True, inline=True)
    assert rerun.comments_skipped_duplicate > 0
    assert rerun.inline_comments_posted == 0
    assert rerun.summary_comment_updated and rerun.summary_comment_id == summary_id
    assert rerun.check_run_updated and rerun.check_run_id == check_id
    assert len(opened.check_runs) == 1

    # Hold a real completed result at the service's cooperative publication boundary.
    old_result = run_review(repo, base=base, head=head, assemble_review_packet=True)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = 0
    def controlled_review(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(20)
            finished.set()
            return old_result
        return run_review(*args, **kwargs)
    monkeypatch.setattr("codeatlas.service.state.run_review", controlled_review)
    manager = ReviewStateManager(max_concurrent_reviews=1)
    try:
        stale = manager.start_review(ReviewCreateRequest(repo=str(repo), base=base, head=head))
        assert entered.wait(10)
        source = repo / "src/app.py"
        source.write_text(source.read_text(encoding="utf-8") + "# synchronized revision\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "synchronize")
        new_head = git(repo, "rev-parse", "HEAD")
        assert manager.cancel_review(stale.run_id)
        assert manager.get_review_status(stale.run_id).status == "cancelled"
        stale_remote = transport(repo, base, head, head_sha_on_refetch=new_head)
        with pytest.raises(SHAMismatchError):
            review(repo, stale_remote, tmp_path / "stale.json", post=True, inline=True)
        assert stale_remote.write_calls == 0
        assert not (tmp_path / "stale.json").exists()
        fresh = manager.start_review(ReviewCreateRequest(repo=str(repo), base=base, head=new_head))
        release.set()
        assert wait(manager, fresh.run_id).status == "completed"
        assert finished.is_set()
        assert manager.get_review_status(stale.run_id).status == "cancelled"
        assert manager.get_findings(stale.run_id) == []
        assert manager.get_findings(fresh.run_id)
        synchronized = transport(repo, base, new_head, comments=list(opened._comments), check_runs=list(opened.check_runs))
        new = review(repo, synchronized, tmp_path / "synchronized.json", post=True)
        assert new.head_sha == new_head and new.head_sha != first.head_sha
        assert new.summary_comment_posted and new.check_run_created
        assert new.check_run_id != check_id
        assert parse_summary_marker(synchronized._comments[-1].body)["head"] == new_head
        update = review(repo, synchronized, tmp_path / "updated.json", post=True)
        assert update.summary_comment_updated and update.check_run_updated
        assert update.check_run_id == new.check_run_id
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("inline,summary,check", [(False, False, False), (True, True, True), (False, True, True)])
def test_dry_run_exact_zero_safety_counters(tmp_path: Path, monkeypatch, inline, summary, check):
    repo = tmp_path / "repo"
    base, head = repository(repo)
    remote = transport(repo, base, head)
    before = (repo / "src/app.py").read_bytes()
    status = git(repo, "status", "--porcelain")
    counters = {"github_write": 0, "provider": 0, "test": 0, "patch_apply": 0, "git_mutation": 0}
    def forbidden(kind):
        def call(*args, **kwargs):
            counters[kind] += 1
            raise AssertionError("Dry run attempted " + kind)
        return call
    for method in WRITE_METHODS:
        monkeypatch.setattr(remote, method, forbidden("github_write"))
    monkeypatch.setattr("codeatlas.review.mock.MockReviewer.review", forbidden("provider"))
    monkeypatch.setattr("codeatlas.providers.live.LiveReviewer.review", forbidden("provider"))
    monkeypatch.setattr("codeatlas.verification.executor.execute_test_command", forbidden("test"))
    monkeypatch.setattr("codeatlas.patching.apply.apply_patch_to_worktree", forbidden("patch_apply"))
    original_run = subprocess.run
    def guarded_run(command, *args, **kwargs):
        if Path(command[0]).stem.lower() == "git":
            if command[1] in {"apply", "commit", "push", "merge", "reset", "clean", "add", "checkout", "switch", "stash"}:
                return forbidden("git_mutation")(command)
        else:
            return forbidden("test")(command)
        return original_run(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", guarded_run)
    artifact = tmp_path / "dry-run.json"
    report = run_github_pr_review(
        "owner/repo", 42, local_repo=repo, transport=remote, inline=inline,
        summary_comment=summary, check_run=check, json_output=artifact,
    )
    assert report.findings_total > 0 and report.errors == []
    assert counters == {"github_write": 0, "provider": 0, "test": 0, "patch_apply": 0, "git_mutation": 0}
    assert remote.write_calls == report.write_calls == report.comments_posted == 0
    assert report.inline_comments_posted == report.issue_comments_posted == 0
    assert report.posted_comment_ids == [] and report.partial_write is False
    assert report.summary_comment_write_attempted is False
    assert report.check_run_write_attempted is False
    assert {
        "writes": remote.write_calls,
        "posted_comments": report.comments_posted,
        "posted_check_runs": int(report.check_run_created or report.check_run_updated),
        "posted_ids": report.posted_comment_ids,
        "partial_writes": int(report.partial_write),
    } == {
        "writes": 0,
        "posted_comments": 0,
        "posted_check_runs": 0,
        "posted_ids": [],
        "partial_writes": 0,
    }
    assert json.loads(artifact.read_text(encoding="utf-8")) == report.model_dump(mode="json")
    assert SECRET not in artifact.read_text(encoding="utf-8")
    assert (repo / "src/app.py").read_bytes() == before
    assert git(repo, "status", "--porcelain") == status
    assert git(repo, "rev-parse", "HEAD") == head


def test_sha_change_between_write_groups_stops_remaining_publication(tmp_path: Path):
    repo = tmp_path / "repo"
    base, head = repository(repo)
    remote = transport(repo, base, head, head_sha_after_writes=1)
    report = review(repo, remote, tmp_path / "partial.json", post=True, inline=True)
    counts = Counter(method for method, _ in remote.calls if method in WRITE_METHODS)
    assert counts == {"post_comment": 1}
    assert report.partial_write is True and report.errors
    assert report.inline_comments_posted == 0 and report.check_run_created is False
    assert remote.write_calls == 1


@pytest.mark.parametrize("finding,decision", [
    ({"file": "src/app.py", "start_line": 2, "end_line": 1}, "suppress"),
    ({"file": "missing.py", "start_line": 2, "end_line": 2}, "suppress"),
    ({"file": "src/app.py", "start_line": 1, "end_line": 1}, "fallback"),
    ({"file": "src/app.py", "start_line": 2, "end_line": 2}, "inline"),
])
def test_inline_regressions_do_not_guess_locations(finding, decision):
    line_map = {"src/app.py": {"added": {2}, "context": {1}, "deleted": set()}}
    assert classify_inline_target(finding, line_map)[0] == decision


def test_seeded_fake_transport_preserves_unique_ids_and_updated_state():
    remote = FakeGitHubTransport(
        metadata={"number": 42, "base_sha": "a" * 40, "head_sha": "b" * 40},
        comments=[{"comment_id": 1007, "body": "old"}],
        inline_comments=[{"comment_id": 1009, "body": "inline"}],
        check_runs=[{"id": 5010, "head_sha": "b" * 40}],
    )
    updated = remote.update_comment("owner/repo", 42, 1007, "updated")
    assert updated.body == remote.list_comments("owner/repo", 42)[0].body == "updated"
    assert remote.post_comment("owner/repo", 42, "new").comment_id == 1010
    created = remote.create_check_run(
        "owner/repo", "b" * 40, name="CodeAtlas review", title="review",
        summary="bounded", conclusion="neutral", external_id="review-42",
    )
    assert created["id"] == 5011


def test_actions_contract_is_read_only_and_not_a_hosted_execution_pass():
    workflow = (ROOT / ".github/workflows/codeatlas-review.yml").read_text(encoding="utf-8")
    assert "types: [opened, synchronize, reopened]" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "pull_request_target" not in workflow
    assert "contents: read" in workflow and "pull-requests: read" in workflow
    assert workflow.count("persist-credentials: false") == 2
    assert "--dry-run" in workflow
    assert "--post" not in workflow
    assert "upload-artifact" not in workflow
