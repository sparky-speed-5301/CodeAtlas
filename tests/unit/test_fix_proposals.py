"""Phase 11C-B FixProposal service integration: bounded generation, preview-only lifecycle.

These tests exercise the service operations over a real repository through
RepairOrchestrator and the existing patch pipeline. Nothing is ever applied,
approved, or committed; the original worktree must remain untouched.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema
import pytest

from codeatlas.orchestrator.repair_models import repair_payload_is_safe
from codeatlas.patching.proposal import generate_approval_token, generate_validation_approval_token
from codeatlas.service.models import FixProposalResponse
from codeatlas.service.state import ReviewStateManager, ServiceStateError

REPO_SOURCE_BASE = "def run(x):\n    return x\n"
REPO_SOURCE_HEAD = "def render(user):\n    print(user.password)\n"
SECRET_SOURCE_HEAD = "def run(x):\n    token = 'AKIAIOSFODNN7EXAMPLE'\n    return x\n"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _create_repo(root: Path, head_source: str) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(REPO_SOURCE_BASE, encoding="utf-8")

    def commit(msg: str) -> None:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", msg)

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")
    commit("base commit")
    (root / "src" / "app.py").write_text(head_source, encoding="utf-8")
    commit("head commit with finding")


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


@pytest.fixture
def reviewed_run_fixed(tmp_path: Path):
    repo = tmp_path / "repository"
    _create_repo(repo, REPO_SOURCE_HEAD)
    manager = ReviewStateManager()
    run_id = manager.start_review(_request(str(repo))).run_id
    _wait_for_run(manager, run_id)
    findings = manager.get_findings(run_id)
    assert findings, "expected at least one deterministic finding"
    return manager, run_id, findings[0].id, repo


def _assert_contract(response: FixProposalResponse) -> None:
    assert response.proposal_id.startswith("prop-")
    assert response.approval_required is True
    assert response.generation_status == "draft_ready"
    assert response.patch_text.startswith("--- a/")
    assert response.patch_hash
    assert response.target_files == ["src/app.py"]
    assert response.risk_level in {"low", "medium", "high"}
    assert 0.0 <= response.confidence <= 1.0
    assert response.quality_decision in {"report", "report_with_uncertainty", "review_only"}
    assert response.policy_decision.get("allowed") is True
    assert response.policy_decision.get("decision") == "requires_human_approval"
    assert response.limitations
    assert response.schema_version == "11C-B.1"
    assert "\\" not in response.repository and "/" not in response.repository
    assert repair_payload_is_safe(response.model_dump(mode="json"))


def test_fix_proposal_happy_path_contract(reviewed_run_fixed):
    manager, run_id, finding_id, _repo = reviewed_run_fixed
    response = manager.request_fix_proposal(run_id, finding_id)
    _assert_contract(response)
    # The draft is anchored to the finding line and requires human review.
    assert "-    print(user.password)" in response.patch_text
    assert "requires human review" in response.patch_text
    assert response.evidence_sources


def test_fix_proposal_validates_against_json_schema(reviewed_run_fixed):
    from pathlib import Path as _P

    schema = json.loads(
        (_P(__file__).resolve().parents[2] / "schemas" / "fix-proposal.schema.json").read_text(encoding="utf-8")
    )
    manager, run_id, finding_id, _repo = reviewed_run_fixed
    response = manager.request_fix_proposal(run_id, finding_id)
    jsonschema.validate(response.model_dump(mode="json"), schema)
    non_draft = manager._non_draft_fix_response(run_id, finding_id, "low_evidence")
    jsonschema.validate(non_draft.model_dump(mode="json"), schema)


def test_fix_proposal_request_is_idempotent_and_scoped(reviewed_run_fixed):
    manager, run_id, finding_id, _repo = reviewed_run_fixed
    first = manager.request_fix_proposal(run_id, finding_id)
    again = manager.request_fix_proposal(run_id, finding_id)
    assert again.proposal_id == first.proposal_id
    latest = manager.get_fix_proposal(run_id, finding_id)
    assert latest.proposal_id == first.proposal_id
    by_id = manager.get_fix_proposal(run_id, finding_id, first.proposal_id)
    assert by_id.proposal_id == first.proposal_id
    with pytest.raises(KeyError):
        manager.get_fix_proposal("rev-does-not-exist", finding_id, first.proposal_id)
    with pytest.raises(ServiceStateError) as err:
        manager.get_fix_proposal(run_id, finding_id, "prop-does-not-exist")
    assert err.value.reason == "proposal_not_found"
    with pytest.raises(KeyError):
        manager.request_fix_proposal(run_id, "CA-DOES-NOT-EXIST")


def test_fix_proposal_cross_run_scope_mismatch(tmp_path: Path):
    repo = tmp_path / "repository"
    _create_repo(repo, REPO_SOURCE_HEAD)
    manager = ReviewStateManager()
    run_a = manager.start_review(_request(str(repo))).run_id
    _wait_for_run(manager, run_a)
    finding_a = manager.get_findings(run_a)[0].id
    proposal = manager.request_fix_proposal(run_a, finding_a)
    run_b = manager.start_review(_request(str(repo))).run_id
    _wait_for_run(manager, run_b)
    finding_b = manager.get_findings(run_b)[0].id
    with pytest.raises(ServiceStateError) as err:
        manager.get_fix_proposal(run_b, finding_b, proposal.proposal_id)
    assert err.value.reason == "scope_mismatch"
    with pytest.raises(ServiceStateError) as err:
        manager.reject_fix_proposal(run_b, finding_b, proposal.proposal_id)
    assert err.value.reason == "scope_mismatch"


def test_fix_proposal_reject_lifecycle(reviewed_run_fixed):
    manager, run_id, finding_id, _repo = reviewed_run_fixed
    response = manager.request_fix_proposal(run_id, finding_id)
    rejected = manager.reject_fix_proposal(run_id, finding_id, response.proposal_id)
    assert rejected.generation_status == "rejected"
    assert rejected.rejection_reason == "rejected_by_user"
    assert rejected.approval_required is True
    latest = manager.get_fix_proposal(run_id, finding_id)
    assert latest.generation_status == "rejected"
    with pytest.raises(ServiceStateError) as err:
        manager.reject_fix_proposal(run_id, finding_id, response.proposal_id)
    assert err.value.reason == "not_rejectable"


def test_fix_proposal_regenerate_supersedes_previous_draft(tmp_path: Path):
    from codeatlas.review.mock import MockRepairReviewer
    from codeatlas.review.provider import ReviewerResult

    class CountingRepairReviewer(MockRepairReviewer):
        """Deterministic provider whose draft changes on every call."""

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def propose(self, context):
            self.calls += 1
            result = super().propose(context)
            suggestion = dict(result.patch_suggestions[0])
            suggestion["unified_diff"] = suggestion["unified_diff"].replace(
                "# TODO(codeatlas)", f"# TODO(codeatlas) rev {self.calls}"
            )
            return ReviewerResult(
                provider_name=result.provider_name,
                provider_version=result.provider_version,
                patch_suggestions=[suggestion],
            )

    repo = tmp_path / "repository"
    _create_repo(repo, REPO_SOURCE_HEAD)
    manager = ReviewStateManager(repair_provider=CountingRepairReviewer())
    run_id = manager.start_review(_request(str(repo))).run_id
    _wait_for_run(manager, run_id)
    finding_id = manager.get_findings(run_id)[0].id

    first = manager.request_fix_proposal(run_id, finding_id)
    assert first.generation_status == "draft_ready"
    second = manager.regenerate_fix_proposal(run_id, finding_id)
    assert second.generation_status == "draft_ready"
    assert second.proposal_id != first.proposal_id
    superseded = manager.get_fix_proposal(run_id, finding_id, first.proposal_id)
    assert superseded.generation_status == "rejected"
    assert superseded.rejection_reason == "superseded_by_regeneration"
    latest = manager.get_fix_proposal(run_id, finding_id)
    assert latest.proposal_id == second.proposal_id


def test_fix_proposal_stale_repository_fails_closed(reviewed_run_fixed):
    manager, run_id, finding_id, repo = reviewed_run_fixed
    _git(repo, "commit", "--allow-empty", "-qm", "move head after review")
    eligibility = manager.get_fix_eligibility(run_id, finding_id)
    assert eligibility.eligible is False
    assert "repository_state_stale" in eligibility.reasons
    assert eligibility.explanations
    response = manager.request_fix_proposal(run_id, finding_id)
    assert response.generation_status == "not_eligible"
    assert response.rejection_reason == "repository_state_stale"
    assert response.rejection_explanation == (
        "The repository has changed since the review run; start a new review."
    )


def test_fix_proposal_ineligible_finding_shows_safe_explanation(reviewed_run_fixed):
    manager, run_id, finding_id, _repo = reviewed_run_fixed
    record = manager.get_review(run_id)
    record.findings[0]["evidence_strength"] = "weak"
    response = manager.request_fix_proposal(run_id, finding_id)
    assert response.generation_status == "not_eligible"
    assert response.rejection_reason == "low_evidence"
    assert response.rejection_explanation == (
        "This finding lacks sufficient deterministic evidence for a fix proposal."
    )
    assert response.proposal_id == ""
    eligibility = manager.get_fix_eligibility(run_id, finding_id)
    assert eligibility.eligible is False
    assert eligibility.reasons == ["low_evidence"]


def test_fix_proposal_secret_finding_fails_closed_without_leaking(tmp_path: Path):
    """A secret-bearing flagged line can never produce an applicable patch."""
    repo = tmp_path / "repository"
    _create_repo(repo, SECRET_SOURCE_HEAD)
    manager = ReviewStateManager()
    run_id = manager.start_review(_request(str(repo))).run_id
    _wait_for_run(manager, run_id)
    finding_id = manager.get_findings(run_id)[0].id
    response = manager.request_fix_proposal(run_id, finding_id)
    assert response.generation_status in {"generation_failed", "rejected_by_policy"}
    assert response.approval_required is True
    assert response.patch_text == ""
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(response.model_dump(mode="json"))
    assert response.rejection_explanation


def test_fix_proposal_generation_never_touches_the_worktree(reviewed_run_fixed):
    manager, run_id, finding_id, repo = reviewed_run_fixed
    scratch = repo / "src" / "draft_notes.txt"
    scratch.write_text("Intentional uncommitted user work", encoding="utf-8")
    (repo / "src" / "app.py").write_text(
        REPO_SOURCE_HEAD + "\n# intentional uncommitted edit\n", encoding="utf-8"
    )
    before_index = _git(repo, "rev-parse", "HEAD")
    response = manager.request_fix_proposal(run_id, finding_id)
    assert response.generation_status == "draft_ready"
    assert _git(repo, "rev-parse", "HEAD") == before_index
    assert scratch.read_text(encoding="utf-8") == "Intentional uncommitted user work"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8").endswith("# intentional uncommitted edit\n")
    staged = _git(repo, "status", "--porcelain")
    assert "src/app.py" in staged  # the working tree edit is still there, unstaged
    _git(repo, "worktree", "prune")


def _fix_validation_token(proposal: FixProposalResponse) -> str:
    return generate_validation_approval_token(
        proposal_id=proposal.proposal_id,
        finding_id=proposal.finding_id,
        run_id=proposal.run_id,
        repository_identity=proposal.repository,
        base_commit=proposal.base_commit,
        head_commit=proposal.head_commit,
        patch_hash=proposal.patch_hash,
        target_files=proposal.target_files,
        operation="validate",
    )


def test_fix_proposal_requires_explicit_validation_approval(reviewed_run_fixed):
    manager, run_id, finding_id, _repo = reviewed_run_fixed
    proposal = manager.request_fix_proposal(run_id, finding_id)
    token = _fix_validation_token(proposal)

    with pytest.raises(ServiceStateError) as err:
        manager.validate_fix_proposal(
            run_id, finding_id, proposal.proposal_id, approval_token=token,
        )
    assert err.value.reason == "validation_not_approved"

    rejected = manager.approve_fix_proposal_for_validation(
        run_id, finding_id, proposal.proposal_id, approval_token="CAT-APP-provider-forged",
    )
    assert rejected.approval_verified is False
    assert rejected.validation_status == "approval_required"
    assert "CAT-APP-provider-forged" not in rejected.model_dump_json()
    assert manager.get_fix_proposal(run_id, finding_id).validation_status == "approval_required"

    approved = manager.approve_fix_proposal_for_validation(
        run_id, finding_id, proposal.proposal_id, approval_token=token,
    )
    assert approved.approval_verified is True
    assert approved.validation_status == "approved_for_validation"
    assert token not in approved.model_dump_json()


def test_fix_validation_token_binds_all_new_scope_fields(reviewed_run_fixed):
    manager, run_id, finding_id, _repo = reviewed_run_fixed
    proposal = manager.request_fix_proposal(run_id, finding_id)
    token = _fix_validation_token(proposal)

    for field in (
        "proposal_id", "finding_id", "run_id", "repository", "base_commit", "head_commit", "patch_hash", "target_files",
    ):
        mutated = proposal.model_copy(update={field: "different"})
        # The manager obtains trusted scope from its run record, never from a
        # client-supplied proposal body. A token minted for any altered scope
        # must therefore fail the approval gate.
        target_files = mutated.target_files if isinstance(mutated.target_files, list) else [mutated.target_files]
        forged = generate_validation_approval_token(
            proposal_id=mutated.proposal_id,
            finding_id=mutated.finding_id,
            run_id=mutated.run_id,
            repository_identity=mutated.repository,
            base_commit=mutated.base_commit,
            head_commit=mutated.head_commit,
            patch_hash=mutated.patch_hash,
            target_files=target_files,
            operation="validate",
        )
        assert forged != token
        rejected = manager.approve_fix_proposal_for_validation(
            run_id, finding_id, proposal.proposal_id, approval_token=forged,
        )
        assert rejected.approval_verified is False
        assert manager.get_fix_proposal(run_id, finding_id).validation_status == "approval_required"

    # The older PatchProposal token format is not an approval for the new
    # validate operation, even when its proposal/base/hash/files match.
    legacy = generate_approval_token(
        proposal.proposal_id, proposal.head_commit, proposal.patch_hash, proposal.target_files, run_id=run_id,
    )
    rejected = manager.approve_fix_proposal_for_validation(
        run_id, finding_id, proposal.proposal_id, approval_token=legacy,
    )
    assert rejected.approval_verified is False
    with pytest.raises(ValueError):
        generate_validation_approval_token(
            proposal_id=proposal.proposal_id,
            finding_id=proposal.finding_id,
            run_id=proposal.run_id,
            repository_identity=proposal.repository,
            base_commit=proposal.base_commit,
            head_commit=proposal.head_commit,
            patch_hash=proposal.patch_hash,
            target_files=proposal.target_files,
            operation="apply",
        )


def test_fix_validation_reuses_isolated_evidence_and_never_returns_token(reviewed_run_fixed):
    manager, run_id, finding_id, repo = reviewed_run_fixed
    proposal = manager.request_fix_proposal(run_id, finding_id)
    token = _fix_validation_token(proposal)
    before_git = {
        "head": _git(repo, "rev-parse", "HEAD"),
        "refs": _git(repo, "show-ref"),
        "status": _git(repo, "status", "--porcelain=v1", "-z"),
        "diff": _git(repo, "diff", "--binary"),
        "cached": _git(repo, "diff", "--cached", "--binary"),
        "worktrees": _git(repo, "worktree", "list", "--porcelain"),
        "index": (repo / ".git" / "index").read_bytes(),
    }
    manager.approve_fix_proposal_for_validation(
        run_id, finding_id, proposal.proposal_id, approval_token=token,
    )
    result = manager.validate_fix_proposal(
        run_id, finding_id, proposal.proposal_id, approval_token=token,
    )

    assert result.valid is True
    assert result.approval_verified is True
    assert result.validation_status == "validated"
    assert result.cleanup_status == "completed"
    assert result.sandbox_id
    assert result.resulting_diff_hash
    assert result.tests_status == "not_run"
    assert result.test_result is None
    assert all(command.startswith("git ") for command in result.commands_run)
    assert result.validation_history[:2] == ["draft_ready", "approval_required"]
    assert result.validation_history[-3:] == [
        "validating", "applied_in_isolated_worktree", "validated",
    ]
    assert token not in result.model_dump_json()
    assert token not in manager.get_fix_proposal(run_id, finding_id).model_dump_json()
    assert "TODO(codeatlas)" not in (repo / "src" / "app.py").read_text(encoding="utf-8")
    after_git = {
        "head": _git(repo, "rev-parse", "HEAD"),
        "refs": _git(repo, "show-ref"),
        "status": _git(repo, "status", "--porcelain=v1", "-z"),
        "diff": _git(repo, "diff", "--binary"),
        "cached": _git(repo, "diff", "--cached", "--binary"),
        "worktrees": _git(repo, "worktree", "list", "--porcelain"),
        "index": (repo / ".git" / "index").read_bytes(),
    }
    assert after_git == before_git


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


def _start_review_and_wait(base_url: str, manager: ReviewStateManager, repo: str) -> tuple[str, str]:
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
    return run_id, findings[0].id


def test_http_fix_proposal_endpoints(running_service):
    base_url, manager, repo = running_service
    run_id, finding_id = _start_review_and_wait(base_url, manager, repo)

    status, eligibility = _http_request(f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-eligibility")
    assert status == 200
    assert eligibility["eligible"] is True
    assert eligibility["reasons"] == []

    status, generated = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal", "POST", {"run_id": run_id}
    )
    assert status == 201
    assert generated["generation_status"] == "draft_ready"
    assert generated["approval_required"] is True
    proposal_id = generated["proposal_id"]

    status, latest = _http_request(f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal")
    assert status == 200
    assert latest["proposal_id"] == proposal_id

    status, by_id = _http_request(
        f"{base_url}/fix-proposals/{proposal_id}?run_id={run_id}&finding_id={finding_id}"
    )
    assert status == 200
    assert by_id["proposal_id"] == proposal_id

    status, mismatch = _http_request(
        f"{base_url}/fix-proposals/{proposal_id}?run_id=rev-other&finding_id={finding_id}"
    )
    assert status == 404  # unknown run fails closed as not found
    assert mismatch["error"] == "not_found"

    status, missing = _http_request(f"{base_url}/fix-proposals/{proposal_id}")
    assert status == 400

    status, rejected = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/reject",
        "POST",
        {"run_id": run_id, "proposal_id": proposal_id},
    )
    assert status == 200
    assert rejected["generation_status"] == "rejected"

    status, regenerated = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/regenerate", "POST", {"run_id": run_id}
    )
    assert status == 201
    assert regenerated["generation_status"] == "draft_ready"

    status, err = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal", "POST", {"run_id": "rev-other"}
    )
    assert status == 400
    assert err["error"] == "scope_mismatch"

    status, _ = _http_request(
        f"{base_url}/reviews/rev-missing/findings/{finding_id}/fix-proposal", "POST", {"run_id": "rev-missing"}
    )
    assert status == 404


def test_http_fix_proposal_rejects_mismatched_proposal_id(running_service):
    base_url, manager, repo = running_service
    run_id, finding_id = _start_review_and_wait(base_url, manager, repo)
    status, err = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/reject",
        "POST",
        {"run_id": run_id, "proposal_id": "prop-unknown"},
    )
    assert status == 400
    assert err["error"] == "proposal_not_found"


def test_http_fix_proposal_approval_and_validation_are_separate_operations(running_service):
    base_url, manager, repo = running_service
    run_id, finding_id = _start_review_and_wait(base_url, manager, repo)
    status, generated = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal",
        "POST",
        {"run_id": run_id},
    )
    assert status == 201
    token = _fix_validation_token(FixProposalResponse.model_validate(generated))
    proposal_id = generated["proposal_id"]

    status, before_approval = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/validate",
        "POST",
        {
            "run_id": run_id,
            "finding_id": finding_id,
            "proposal_id": proposal_id,
            "approval_token": token,
        },
    )
    assert status == 409
    assert before_approval["error"] == "validation_not_approved"

    status, approval = _http_request(
        f"{base_url}/fix-proposals/{proposal_id}/approve-validation",
        "POST",
        {"run_id": run_id, "finding_id": finding_id, "approval_token": token},
    )
    assert status == 200
    assert approval["approval_verified"] is True
    assert approval["validation_status"] == "approved_for_validation"
    assert token not in json.dumps(approval)

    status, validated = _http_request(
        f"{base_url}/fix-proposals/{proposal_id}/validate",
        "POST",
        {
            "run_id": run_id,
            "finding_id": finding_id,
            "proposal_id": proposal_id,
            "approval_token": token,
        },
    )
    assert status == 200
    assert validated["valid"] is True
    assert validated["approval_verified"] is True
    assert validated["validation_status"] == "validated"
    assert validated["cleanup_status"] == "completed"
    assert validated["sandbox_id"]
    assert token not in json.dumps(validated)

    status, stored = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal"
    )
    assert status == 200
    assert stored["validation_status"] == "validated"
    assert stored["validation_result"]["valid"] is True
    assert token not in json.dumps(stored)
