"""Phase 11C-D/E FixProposal apply, revert, and apply-history service tests.

Every apply path is exercised through the complete explicit chain: validated
proposal, identity-matched evidence, clean rechecked workspace, apply-scoped
approval token, and the confirmed exact patch hash. Nothing may apply
automatically, a validate token must never authorize an apply, and a revert
must restore the exact pre-apply bytes.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from codeatlas.patching.proposal import (
    compute_patch_hash,
    generate_validation_approval_token,
)
from codeatlas.service.models import FixProposalResponse
from codeatlas.service.state import ReviewStateManager, ServiceStateError

REPO_SOURCE_BASE = "def run(x):\n    return x\n"
REPO_SOURCE_HEAD = "def render(user):\n    print(user.password)\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _create_repo(root: Path, head_source: str) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(REPO_SOURCE_BASE, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base commit")
    (root / "src" / "app.py").write_text(head_source, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head commit with finding")


def _request(repo: str):
    from codeatlas.service.models import ReviewCreateRequest

    return ReviewCreateRequest(repo=repo, base="HEAD~1", head="HEAD")


def _wait_for_run(manager: ReviewStateManager, run_id: str) -> None:
    for _ in range(240):
        status = manager.get_review_status(run_id)
        if status.status in {"completed", "failed", "cancelled"}:
            return
        time.sleep(0.25)
    raise AssertionError("review run did not complete in time")


def _operation_token(proposal, operation: str) -> str:
    return generate_validation_approval_token(
        proposal_id=proposal.proposal_id,
        finding_id=proposal.finding_id,
        run_id=proposal.run_id,
        repository_identity=proposal.repository,
        base_commit=proposal.base_commit,
        head_commit=proposal.head_commit,
        patch_hash=proposal.patch_hash,
        target_files=proposal.target_files,
        operation=operation,
    )


@pytest.fixture
def validated_run(tmp_path: Path):
    """A review run whose fix proposal has completed isolated validation."""
    repo = tmp_path / "repository"
    _create_repo(repo, REPO_SOURCE_HEAD)
    manager = ReviewStateManager()
    run_id = manager.start_review(_request(str(repo))).run_id
    _wait_for_run(manager, run_id)
    finding_id = manager.get_findings(run_id)[0].id
    proposal = manager.request_fix_proposal(run_id, finding_id)
    assert proposal.generation_status == "draft_ready"
    token = _operation_token(proposal, "validate")
    manager.approve_fix_proposal_for_validation(run_id, finding_id, proposal.proposal_id, approval_token=token)
    validation = manager.validate_fix_proposal(run_id, finding_id, proposal.proposal_id, approval_token=token)
    assert validation.valid is True and validation.validation_status == "validated"
    return manager, run_id, finding_id, proposal, repo


def test_apply_requires_validated_proposal(validated_run):
    manager, run_id, finding_id, _proposal, _repo = validated_run
    # Move the stored proposal back to the pre-validation state to prove the
    # apply gate rejects anything that is not validated.
    record = manager.get_review(run_id)
    proposal_id = record.fix_states[finding_id]["proposal_id"]
    fix_record = manager._fix_proposals[proposal_id]
    fix_record.response = fix_record.response.model_copy(update={"validation_status": "approved_for_validation"})
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal_id,
            approval_token=_operation_token(fix_record.response, "apply"),
            confirmed_patch_hash=fix_record.response.patch_hash,
        )
    assert err.value.reason == "apply_not_validated"
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, "prop-missing",
            approval_token=_operation_token(fix_record.response, "apply"),
            confirmed_patch_hash=fix_record.response.patch_hash,
        )
    assert err.value.reason == "proposal_not_found"


def test_apply_happy_path_matches_validated_diff_and_binds_workspace(validated_run):
    manager, run_id, finding_id, proposal, repo = validated_run
    before = {
        "head": _git(repo, "rev-parse", "HEAD"),
        "refs": _git(repo, "show-ref"),
        "index": (repo / ".git" / "index").read_bytes(),
        "cached": _git(repo, "diff", "--cached", "--binary"),
    }
    applied = manager.apply_fix_proposal(
        run_id, finding_id, proposal.proposal_id,
        approval_token=_operation_token(proposal, "apply"),
        confirmed_patch_hash=proposal.patch_hash,
    )
    assert applied.apply_status == "applied"
    assert applied.approval_verified is True
    assert applied.operation == "apply"
    assert applied.files_changed == ["src/app.py"]
    assert applied.validated_resulting_diff_hash == applied.resulting_diff_hash
    assert applied.resulting_diff_hash
    assert _operation_token(proposal, "apply") not in applied.model_dump_json()
    # The workspace now carries the fix as a plain unstaged change.
    assert "TODO(codeatlas)" in (repo / "src" / "app.py").read_text(encoding="utf-8")
    status = _git(repo, "status", "--porcelain")
    assert "M src/app.py" in status
    # HEAD, refs, and the index are untouched; only the target file changed.
    assert _git(repo, "rev-parse", "HEAD") == before["head"]
    assert _git(repo, "show-ref") == before["refs"]
    assert (repo / ".git" / "index").read_bytes() == before["index"]
    assert _git(repo, "diff", "--cached", "--binary") == before["cached"]
    # Service state moved to the applied terminal chain.
    latest = manager.get_fix_proposal(run_id, finding_id)
    assert latest.generation_status == "applied"
    assert latest.validation_status == "applied"
    assert latest.validation_history[-2:] == ["validated", "applied"]
    history = manager.get_fix_apply_history(run_id, finding_id)
    assert history.apply_status == "applied"
    assert history.revert_available is True
    assert [event.event for event in history.events] == ["applied"]
    assert history.events[0].head_commit == before["head"]


def test_apply_rejects_validate_token_and_wrong_confirmation(validated_run):
    manager, run_id, finding_id, proposal, repo = validated_run
    response = manager.apply_fix_proposal(
        run_id, finding_id, proposal.proposal_id,
        approval_token=_operation_token(proposal, "validate"),
        confirmed_patch_hash=proposal.patch_hash,
    )
    assert response.approval_verified is False
    assert response.apply_status == "not_applied"
    assert response.errors
    assert manager.get_fix_proposal(run_id, finding_id).validation_status == "validated"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == REPO_SOURCE_HEAD

    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=_operation_token(proposal, "apply"),
            confirmed_patch_hash="0" * 64,
        )
    assert err.value.reason == "apply_confirmation_invalid"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == REPO_SOURCE_HEAD

    # A token minted for a different proposal scope is rejected as well.
    forged = generate_validation_approval_token(
        proposal_id=proposal.proposal_id,
        finding_id="CA-OTHER",
        run_id=proposal.run_id,
        repository_identity=proposal.repository,
        base_commit=proposal.base_commit,
        head_commit=proposal.head_commit,
        patch_hash=proposal.patch_hash,
        target_files=proposal.target_files,
        operation="apply",
    )
    response = manager.apply_fix_proposal(
        run_id, finding_id, proposal.proposal_id,
        approval_token=forged, confirmed_patch_hash=proposal.patch_hash,
    )
    assert response.approval_verified is False


def test_apply_blocked_by_dirty_workspace(validated_run):
    manager, run_id, finding_id, proposal, repo = validated_run
    scratch = repo / "src" / "notes.txt"
    scratch.write_text("intentional uncommitted user work\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text(REPO_SOURCE_HEAD + "\n# uncommitted edit\n", encoding="utf-8")
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=_operation_token(proposal, "apply"),
            confirmed_patch_hash=proposal.patch_hash,
        )
    assert err.value.reason == "workspace_dirty"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8").endswith("# uncommitted edit\n")
    assert scratch.read_text(encoding="utf-8") == "intentional uncommitted user work\n"
    assert "TODO(codeatlas)" not in (repo / "src" / "app.py").read_text(encoding="utf-8")


def test_apply_blocked_when_head_moved(validated_run):
    manager, run_id, finding_id, proposal, repo = validated_run
    _git(repo, "commit", "--allow-empty", "-qm", "move head")
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=_operation_token(proposal, "apply"),
            confirmed_patch_hash=proposal.patch_hash,
        )
    assert err.value.reason == "repository_state_stale"


def test_apply_blocked_when_validation_evidence_identity_mismatches(validated_run):
    manager, run_id, finding_id, proposal, _repo = validated_run
    with manager._lock:
        tampered = manager._validations[proposal.proposal_id]
        tampered.patch_hash = "f" * 64
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=_operation_token(proposal, "apply"),
            confirmed_patch_hash=proposal.patch_hash,
        )
    assert err.value.reason == "apply_evidence_identity_mismatch"
    with manager._lock:
        manager._validations.pop(proposal.proposal_id)
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=_operation_token(proposal, "apply"),
            confirmed_patch_hash=proposal.patch_hash,
        )
    assert err.value.reason == "apply_evidence_missing"


def test_apply_blocked_when_policy_version_incompatible(validated_run):
    manager, run_id, finding_id, proposal, repo = validated_run
    record = manager.get_review(run_id)
    fix_record = manager._fix_proposals[proposal.proposal_id]
    patch = fix_record.patch_proposal
    patch.provenance = dict(patch.provenance)
    patch.provenance["repair_policy_version"] = "0.0.0-incompatible"
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=_operation_token(proposal, "apply"),
            confirmed_patch_hash=proposal.patch_hash,
        )
    assert err.value.reason == "apply_policy_version_incompatible"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == REPO_SOURCE_HEAD
    assert record is not None


def test_apply_blocked_when_redaction_fails(validated_run):
    manager, run_id, finding_id, proposal, repo = validated_run
    secret_diff = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def render(user):\n"
        "-    print(user.password)\n"
        "+    token = 'AKIAIOSFODNN7EXAMPLE'\n"
    )
    new_hash = compute_patch_hash(secret_diff)
    fix_record = manager._fix_proposals[proposal.proposal_id]
    patch = fix_record.patch_proposal
    patch.unified_diff = secret_diff
    patch.patch_hash = new_hash
    fix_record.response = fix_record.response.model_copy(update={
        "patch_text": secret_diff,
        "patch_hash": new_hash,
    })
    with manager._lock:
        manager._validations[proposal.proposal_id].patch_hash = new_hash
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=_operation_token(fix_record.response, "apply"),
            confirmed_patch_hash=new_hash,
        )
    assert err.value.reason == "apply_redaction_failed"
    assert "AKIAIOSFODNN7EXAMPLE" not in (repo / "src" / "app.py").read_text(encoding="utf-8")


def test_already_applied_proposal_is_rejected(validated_run):
    manager, run_id, finding_id, proposal, _repo = validated_run
    token = _operation_token(proposal, "apply")
    manager.apply_fix_proposal(
        run_id, finding_id, proposal.proposal_id,
        approval_token=token, confirmed_patch_hash=proposal.patch_hash,
    )
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=token, confirmed_patch_hash=proposal.patch_hash,
        )
    assert err.value.reason == "already_applied"
    # Applied proposals cannot be rejected or regenerated either.
    with pytest.raises(ServiceStateError) as err:
        manager.reject_fix_proposal(run_id, finding_id, proposal.proposal_id)
    assert err.value.reason == "not_rejectable"


def test_revert_restores_exact_bytes_once(validated_run):
    manager, run_id, finding_id, proposal, repo = validated_run
    token = _operation_token(proposal, "apply")
    manager.apply_fix_proposal(
        run_id, finding_id, proposal.proposal_id,
        approval_token=token, confirmed_patch_hash=proposal.patch_hash,
    )
    with pytest.raises(ServiceStateError) as err:
        manager.revert_fix_apply(run_id, finding_id, proposal.proposal_id, confirmed_patch_hash="0" * 64)
    assert err.value.reason == "revert_confirmation_invalid"

    reverted = manager.revert_fix_apply(
        run_id, finding_id, proposal.proposal_id, confirmed_patch_hash=proposal.patch_hash,
    )
    assert reverted.apply_status == "reverted"
    assert reverted.operation == "revert"
    assert reverted.files_restored == ["src/app.py"]
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == REPO_SOURCE_HEAD
    assert _git(repo, "status", "--porcelain") == ""
    latest = manager.get_fix_proposal(run_id, finding_id)
    assert latest.validation_status == "reverted"
    assert latest.validation_history[-2:] == ["applied", "reverted"]
    history = manager.get_fix_apply_history(run_id, finding_id)
    assert history.apply_status == "reverted"
    assert history.revert_available is False
    assert [event.event for event in history.events] == ["applied", "reverted"]

    with pytest.raises(ServiceStateError) as err:
        manager.revert_fix_apply(run_id, finding_id, proposal.proposal_id, confirmed_patch_hash=proposal.patch_hash)
    assert err.value.reason == "revert_not_available"
    with pytest.raises(ServiceStateError) as err:
        manager.apply_fix_proposal(
            run_id, finding_id, proposal.proposal_id,
            approval_token=token, confirmed_patch_hash=proposal.patch_hash,
        )
    assert err.value.reason == "apply_not_available"


def test_revert_blocked_when_files_changed_after_apply(validated_run):
    manager, run_id, finding_id, proposal, repo = validated_run
    token = _operation_token(proposal, "apply")
    manager.apply_fix_proposal(
        run_id, finding_id, proposal.proposal_id,
        approval_token=token, confirmed_patch_hash=proposal.patch_hash,
    )
    user_edit = (repo / "src" / "app.py").read_text(encoding="utf-8") + "\n# user touched the applied file\n"
    (repo / "src" / "app.py").write_text(user_edit, encoding="utf-8")
    with pytest.raises(ServiceStateError) as err:
        manager.revert_fix_apply(run_id, finding_id, proposal.proposal_id, confirmed_patch_hash=proposal.patch_hash)
    assert err.value.reason == "revert_state_changed"
    # The user's edit is preserved; revert never clobbers newer changes.
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == user_edit


# ---------------------------------------------------------------------------
# HTTP endpoint coverage
# ---------------------------------------------------------------------------


def _http_request(url: str, method: str = "GET", data: dict | None = None) -> tuple[int, dict]:
    body_bytes = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body_bytes, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as err:
        content = err.read().decode("utf-8")
        return err.code, json.loads(content) if content else {}


@pytest.fixture
def running_service(tmp_path: Path):
    from codeatlas.service import create_server

    repo = tmp_path / "repository"
    _create_repo(repo, REPO_SOURCE_HEAD)
    manager = ReviewStateManager()
    server = create_server(host="127.0.0.1", port=0, state_manager=manager)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", manager, str(repo)
    server.shutdown()
    server.server_close()


def _validated_proposal_via_http(base_url: str, manager: ReviewStateManager, repo: str):
    status, body = _http_request(f"{base_url}/reviews", "POST", {"repo": repo, "base": "HEAD~1", "head": "HEAD"})
    assert status == 201, body
    run_id = body["run_id"]
    for _ in range(240):
        _, status_body = _http_request(f"{base_url}/reviews/{run_id}")
        if status_body.get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    findings = manager.get_findings(run_id)
    assert findings
    finding_id = findings[0].id
    status, generated = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal", "POST", {"run_id": run_id}
    )
    assert status == 201
    proposal = FixProposalResponse.model_validate(generated)
    token = _operation_token(proposal, "validate")
    status, _ = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/approve-validation",
        "POST",
        {"run_id": run_id, "finding_id": finding_id, "proposal_id": proposal.proposal_id, "approval_token": token},
    )
    assert status == 200
    status, validation = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/validate",
        "POST",
        {"run_id": run_id, "finding_id": finding_id, "proposal_id": proposal.proposal_id, "approval_token": token},
    )
    assert status == 200 and validation["valid"] is True
    return run_id, finding_id, proposal


def test_http_apply_revert_and_history_endpoints(running_service):
    base_url, manager, repo = running_service
    run_id, finding_id, proposal = _validated_proposal_via_http(base_url, manager, repo)
    apply_token = _operation_token(proposal, "apply")

    status, history = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply-history"
    )
    assert status == 200
    assert history["apply_status"] == "not_applied"
    assert history["revert_available"] is False

    status, applied = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id,
            "finding_id": finding_id,
            "proposal_id": proposal.proposal_id,
            "approval_token": apply_token,
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 200, applied
    assert applied["apply_status"] == "applied"
    assert applied["approval_verified"] is True
    assert apply_token not in json.dumps(applied)

    status, history = _http_request(
        f"{base_url}/fix-proposals/{proposal.proposal_id}/apply-history?run_id={run_id}&finding_id={finding_id}"
    )
    assert status == 200
    assert history["apply_status"] == "applied"
    assert history["revert_available"] is True
    assert [event["event"] for event in history["events"]] == ["applied"]

    status, conflict = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id,
            "finding_id": finding_id,
            "proposal_id": proposal.proposal_id,
            "approval_token": apply_token,
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 409
    assert conflict["error"] == "already_applied"

    status, reverted = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/revert",
        "POST",
        {
            "run_id": run_id,
            "finding_id": finding_id,
            "proposal_id": proposal.proposal_id,
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 200, reverted
    assert reverted["apply_status"] == "reverted"
    import pathlib as _p

    assert "TODO(codeatlas)" not in (_p.Path(repo) / "src" / "app.py").read_text(encoding="utf-8")

    # After a revert, the proposal is spent: applying again fails closed.
    status, err = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id,
            "finding_id": finding_id,
            "proposal_id": proposal.proposal_id,
            "approval_token": apply_token,
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 409
    assert err["error"] == "apply_not_available"

    # The legacy untyped per-proposal apply endpoint must stay absent.
    status, legacy = _http_request(f"{base_url}/proposals/{proposal.proposal_id}/apply", "POST", {})
    assert status == 404
    assert legacy["error"] == "not_found"


def test_http_apply_requires_complete_typed_request(running_service):
    base_url, manager, repo = running_service
    run_id, finding_id, proposal = _validated_proposal_via_http(base_url, manager, repo)
    # A short confirmation hash fails closed at the typed request boundary.
    status, err = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id,
            "finding_id": finding_id,
            "proposal_id": proposal.proposal_id,
            "approval_token": "CAT-APP-short",
            "confirmed_patch_hash": "abcd",
        },
    )
    assert status == 400
    assert err["error"] == "bad_request"
    status, err = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id,
            "finding_id": finding_id,
            "proposal_id": proposal.proposal_id,
            "approval_token": "CAT-APP-short",
            "confirmed_patch_hash": "a" * 64,
            "unexpected_extra": True,
        },
    )
    assert status == 400
    assert err["error"] == "bad_request"
