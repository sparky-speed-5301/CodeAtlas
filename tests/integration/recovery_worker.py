"""Controlled subprocess boundaries for persistent-service crash tests."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from codeatlas.patching.proposal import generate_validation_approval_token
from codeatlas.review.mock import MockRepairReviewer
from codeatlas.service.models import ReviewCreateRequest
from codeatlas.service.state import ReviewStateManager, ServiceStateError


def token(fix, operation="validate"):
    return generate_validation_approval_token(
        proposal_id=fix.proposal_id, finding_id=fix.finding_id, run_id=fix.run_id,
        repository_identity=fix.repository, base_commit=fix.base_commit,
        head_commit=fix.head_commit, patch_hash=fix.patch_hash,
        target_files=fix.target_files, operation=operation,
    )


def wait_review(manager, run):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = manager.get_review_status(run).status
        if status in {"completed", "failed", "cancelled"}:
            assert status == "completed", manager.get_review_status(run)
            return
        time.sleep(.01)
    raise AssertionError("Review completion deadline exceeded")


def interrupt(stage, repo, database, ready):
    from codeatlas.service import state

    manager = ReviewStateManager(persistence_path=database, max_concurrent_reviews=1)
    scope = {"stage": stage, "run_ids": []}

    def boundary(**details):
        pending = ready.with_suffix(".pending")
        pending.write_text(json.dumps({**scope, **details}), encoding="utf-8")
        pending.replace(ready)
        threading.Event().wait(120)
        raise AssertionError("Parent did not interrupt worker")

    request = ReviewCreateRequest(repo=str(repo), base="HEAD~1", head="HEAD")
    if stage == "queued":
        entered = threading.Event()
        def blocked_review(*args, **kwargs):
            entered.set()
            threading.Event().wait(120)
            raise AssertionError("Parent did not interrupt queued worker")
        state.run_review = blocked_review
        first = manager.start_review(request).run_id
        assert entered.wait(10)
        second = manager.start_review(request).run_id
        scope["run_ids"] = [first, second]
        boundary(statuses=[manager.get_review_status(run).status for run in scope["run_ids"]])
        return

    run = manager.start_review(request).run_id
    wait_review(manager, run)
    finding = manager.get_findings(run)[0].id
    scope.update(run_ids=[run], finding_id=finding)
    if stage == "provider":
        class BlockingProvider(MockRepairReviewer):
            def propose(self, context):
                boundary(provider_entered=True)
        manager.repair_provider = BlockingProvider()
        manager.request_fix_proposal(run, finding)
        return

    fix = manager.request_fix_proposal(run, finding)
    assert fix.generation_status == "draft_ready"
    scope.update(proposal_id=fix.proposal_id, patch_hash=fix.patch_hash, patch_text=fix.patch_text)
    manager.approve_fix_proposal_for_validation(run, finding, fix.proposal_id, approval_token=token(fix))
    if stage == "test":
        # The repository test signals from the actual runner subprocess.
        ready.with_suffix(".scope.json").write_text(json.dumps(scope), encoding="utf-8")
        ready.with_suffix(".gate").touch()
        manager.validate_fix_proposal(run, finding, fix.proposal_id, approval_token=token(fix), run_tests=True)
        raise AssertionError("Controlled repository test did not block")

    validated = manager.validate_fix_proposal(run, finding, fix.proposal_id, approval_token=token(fix))
    assert validated.valid
    manager.apply_fix_proposal(run, finding, fix.proposal_id, approval_token=token(fix, "apply"), confirmed_patch_hash=fix.patch_hash)
    original_restore = state.restore_worktree_files
    def interrupted_restore(*args, **kwargs):
        assert original_restore(*args, **kwargs)
        boundary(restored_before_interruption=True)
    state.restore_worktree_files = interrupted_restore
    manager.revert_fix_apply(run, finding, fix.proposal_id, confirmed_patch_hash=fix.patch_hash)


def recover(database, scope, output):
    counters = {"provider": 0, "test": 0, "git_mutation": 0, "workspace_write": 0}
    def forbidden(kind):
        def call(*args, **kwargs):
            counters[kind] += 1
            raise AssertionError("Recovery attempted " + kind)
        return call

    import subprocess
    original_run = subprocess.run
    def guarded_run(command, *args, **kwargs):
        if Path(command[0]).stem.lower() != "git":
            return forbidden("test")(command)
        if command[1] not in {"status", "rev-parse", "diff", "show", "ls-files", "log"}:
            return forbidden("git_mutation")(command)
        return original_run(command, *args, **kwargs)

    with patch("codeatlas.service.state.run_review", forbidden("provider")), \
         patch.object(MockRepairReviewer, "propose", forbidden("provider")), \
         patch.object(subprocess, "run", guarded_run), \
         patch("codeatlas.service.state.restore_worktree_files", forbidden("workspace_write")), \
         patch("codeatlas.service.state.apply_patch_to_worktree", forbidden("workspace_write")):
        manager = ReviewStateManager(persistence_path=database)
        try:
            statuses = [manager.get_review_status(run).model_dump(mode="json") for run in scope["run_ids"]]
            result = {"statuses": statuses, "counters": counters, "errors": manager._persistence_errors}
            run = scope["run_ids"][0]
            if "finding_id" in scope:
                finding = scope["finding_id"]
                result["findings"] = [item.model_dump(mode="json") for item in manager.get_findings(run)]
                result["safe_error_code"] = manager.get_review(run).safe_error_code
                operations = {"generation": lambda: manager.request_fix_proposal(run, finding)}
                if "proposal_id" in scope:
                    proposal = scope["proposal_id"]
                    fix = manager.get_fix_proposal(run, finding, proposal)
                    result["fix"] = fix.model_dump(mode="json")
                    result["history"] = manager.get_fix_apply_history(run, finding, proposal).model_dump(mode="json")
                    operations.update({
                        "approve": lambda: manager.approve_fix_proposal_for_validation(run, finding, proposal, approval_token=token(fix)),
                        "validate": lambda: manager.validate_fix_proposal(run, finding, proposal, approval_token=token(fix), run_tests=True),
                        "apply": lambda: manager.apply_fix_proposal(run, finding, proposal, approval_token=token(fix, "apply"), confirmed_patch_hash=fix.patch_hash),
                        "revert": lambda: manager.revert_fix_apply(run, finding, proposal, confirmed_patch_hash=fix.patch_hash),
                    })
                result["blocked"] = {}
                for name, operation in operations.items():
                    try:
                        operation()
                    except ServiceStateError as error:
                        result["blocked"][name] = error.reason
                    else:
                        raise AssertionError("Recovered authority was replayed: " + name)
        finally:
            manager.close()
        output.write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    mode, database, payload, output = sys.argv[1:]
    if mode == "recover":
        recover(Path(database), json.loads(Path(payload).read_text(encoding="utf-8")), Path(output))
    else:
        interrupt(mode, Path(payload), Path(database), Path(output))
