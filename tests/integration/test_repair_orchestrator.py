"""Actual dirty Git worktree preservation and existing validator hand-off."""

from __future__ import annotations

import subprocess
from pathlib import Path

from codeatlas.git.snapshot import temporary_snapshot
from codeatlas.orchestrator import RepairOrchestrator, RepairRepositoryState
from codeatlas.patching import generate_approval_token, validate_patch_proposal
from eval.eval_repair import OfflineRepairReviewer, SOURCES, repair_request


def test_repair_preserves_dirty_worktree_and_uses_existing_approval_validator(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "src").mkdir()
    target = repo / "src/calc.py"

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-q", "-b", "main")
    target.write_text(SOURCES["python"].replace("// 0", "// 2"), encoding="utf-8")
    git("add", "src/calc.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    target.write_text(SOURCES["python"], encoding="utf-8")
    git("add", "src/calc.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    target.write_text(SOURCES["python"] + "# intentional staged user work\n", encoding="utf-8")
    git("add", "src/calc.py")
    target.write_text(target.read_text() + "# intentional unstaged user work\n", encoding="utf-8")
    untracked = repo / "notes.txt"
    untracked.write_text("user-owned notes", encoding="utf-8")
    before = (git("rev-parse", "HEAD"), git("status", "--porcelain"), git("diff", "--cached"), target.read_bytes(), untracked.read_bytes())
    finding, request = repair_request(tmp_path / "fixtures")
    request["state"] = RepairRepositoryState(run_id="run-repair", repository=str(repo), base_commit=base, head_commit=head)
    for model in (request["manifest"], request["packet"]):
        model.repository, model.base_commit, model.head_commit = str(repo), base, head

    with temporary_snapshot(repo, head) as snapshot:
        request["snapshot"] = snapshot
        with monkeypatch.context() as guard:
            def no_process(*args, **kwargs):
                raise AssertionError("Repair generation must not run any process")

            guard.setattr(subprocess, "run", no_process)
            guard.setattr(subprocess, "Popen", no_process)
            result = RepairOrchestrator(OfflineRepairReviewer()).plan(finding, **request)
        assert result.status == "proposed", result.reason
        assert (snapshot.path / "src/calc.py").read_text() == SOURCES["python"]

    # The returned proposal enters the existing validator, which blocks isolated
    # application until the operator supplies the existing scoped approval token.
    proposal = result.proposal
    blocked = validate_patch_proposal(proposal, repo, run_id=request["state"].run_id)
    assert blocked.valid and blocked.approval_verified is False and blocked.sandbox_id is None
    token = generate_approval_token(proposal.proposal_id, proposal.base_commit, proposal.patch_hash,
                                    proposal.target_files, request["state"].run_id)
    validated = validate_patch_proposal(proposal, repo, approval_token=token, run_id=request["state"].run_id)
    assert validated.valid and validated.approval_verified and validated.applies_cleanly
    assert validated.syntax_valid and validated.cleanup_status == "completed"
    assert not validated.execution_allowed and validated.tests_status == "not_run"
    after = (git("rev-parse", "HEAD"), git("status", "--porcelain"), git("diff", "--cached"), target.read_bytes(), untracked.read_bytes())
    assert after == before
