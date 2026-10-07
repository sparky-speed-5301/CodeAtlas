"""Phase 11C-F end-to-end autofix workflow verification over the real service.

One real Git repository, one real review run, and the complete explicit chain:
generate -> approve -> isolated sandbox validation -> explicit apply of the
validated proposal -> safe revert -> bounded apply history. The service runs
over real loopback HTTP exactly like the VS Code client uses it. Automatic
application must be impossible: every rejection path is exercised and the
repository metadata (HEAD, refs, index) stays untouched throughout.
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

from codeatlas.patching.proposal import generate_validation_approval_token
from codeatlas.service.models import FixProposalResponse
from codeatlas.service.server import create_server
from codeatlas.service.state import ReviewStateManager

REPO_SOURCE_BASE = "def run(x):\n    return x\n"
REPO_SOURCE_HEAD = "def render(user):\n    print(user.password)\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _create_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(REPO_SOURCE_BASE, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base commit")
    (root / "src" / "app.py").write_text(REPO_SOURCE_HEAD, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head commit with finding")


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
def service(tmp_path: Path):
    repo = tmp_path / "repository"
    _create_repo(repo)
    manager = ReviewStateManager()
    server = create_server(host="127.0.0.1", port=0, state_manager=manager)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", manager, repo
    server.shutdown()
    server.server_close()


def _run_review(base_url: str, manager: ReviewStateManager, repo: Path) -> tuple[str, str]:
    status, body = _http_request(
        f"{base_url}/reviews", "POST",
        {"repo": str(repo), "base": "HEAD~1", "head": "HEAD"},
    )
    assert status == 201, body
    run_id = body["run_id"]
    for _ in range(240):
        _, status_body = _http_request(f"{base_url}/reviews/{run_id}")
        if status_body.get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    findings = manager.get_findings(run_id)
    assert findings, "the deterministic sensitive-data analyzer must produce a finding"
    return run_id, findings[0].id


def _token(proposal: FixProposalResponse, operation: str) -> str:
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


def _generate_and_validate(base_url: str, manager: ReviewStateManager, repo: Path):
    run_id, finding_id = _run_review(base_url, manager, repo)
    status, generated = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal",
        "POST", {"run_id": run_id},
    )
    assert status == 201, generated
    proposal = FixProposalResponse.model_validate(generated)
    assert proposal.generation_status == "draft_ready"
    assert proposal.approval_required is True

    validate_token = _token(proposal, "validate")
    # Approval is a separate explicit operation; applying directly must fail.
    status, err = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id, "finding_id": finding_id, "proposal_id": proposal.proposal_id,
            "approval_token": _token(proposal, "apply"),
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 409, err
    assert err["error"] == "apply_not_validated"

    status, approval = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/approve-validation",
        "POST",
        {
            "run_id": run_id, "finding_id": finding_id,
            "proposal_id": proposal.proposal_id, "approval_token": validate_token,
        },
    )
    assert status == 200 and approval["approval_verified"] is True, approval

    status, validation = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/validate",
        "POST",
        {
            "run_id": run_id, "finding_id": finding_id,
            "proposal_id": proposal.proposal_id, "approval_token": validate_token,
        },
    )
    assert status == 200 and validation["valid"] is True, validation
    assert validation["validation_status"] == "validated"
    assert validation["cleanup_status"] == "completed"
    return run_id, finding_id, proposal, validation


def test_full_autofix_workflow_apply_then_revert(service):
    base_url, manager, repo = service
    before_head = _git(repo, "rev-parse", "HEAD")
    before_refs = _git(repo, "show-ref")
    before_index = (repo / ".git" / "index").read_bytes()

    run_id, finding_id, proposal, validation = _generate_and_validate(base_url, manager, repo)

    # A validate token never authorizes apply.
    status, applied = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id, "finding_id": finding_id, "proposal_id": proposal.proposal_id,
            "approval_token": _token(proposal, "validate"),
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 200
    assert applied["approval_verified"] is False and applied["apply_status"] == "not_applied"
    assert "TODO(codeatlas)" not in (repo / "src" / "app.py").read_text(encoding="utf-8")

    # Explicit apply with an apply-scoped token and the exact patch hash.
    status, applied = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id, "finding_id": finding_id, "proposal_id": proposal.proposal_id,
            "approval_token": _token(proposal, "apply"),
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 200, applied
    assert applied["apply_status"] == "applied" and applied["approval_verified"] is True
    assert applied["resulting_diff_hash"] == validation["resulting_diff_hash"]
    assert applied["files_changed"] == ["src/app.py"]
    assert "TODO(codeatlas)" in (repo / "src" / "app.py").read_text(encoding="utf-8")
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git(repo, "show-ref") == before_refs
    assert (repo / ".git" / "index").read_bytes() == before_index

    status, history = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply-history"
    )
    assert status == 200
    assert history["apply_status"] == "applied" and history["revert_available"] is True

    status, reverted = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/revert",
        "POST",
        {
            "run_id": run_id, "finding_id": finding_id,
            "proposal_id": proposal.proposal_id,
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 200, reverted
    assert reverted["apply_status"] == "reverted"
    assert reverted["files_restored"] == ["src/app.py"]
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == REPO_SOURCE_HEAD
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == before_head

    status, history = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply-history"
    )
    assert status == 200
    assert [event["event"] for event in history["events"]] == ["applied", "reverted"]

    # The spent proposal can be neither applied nor reverted again.
    status, err = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/revert",
        "POST",
        {
            "run_id": run_id, "finding_id": finding_id,
            "proposal_id": proposal.proposal_id,
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 409 and err["error"] == "revert_not_available"

    # Tokens and secrets never surface in any response the workflow produced.
    for body in (applied, reverted, history):
        assert _token(proposal, "apply") not in json.dumps(body)
        assert _token(proposal, "validate") not in json.dumps(body)


def test_apply_blocked_while_workspace_dirty_stays_explicit(service):
    base_url, manager, repo = service
    run_id, finding_id, proposal, _validation = _generate_and_validate(base_url, manager, repo)
    (repo / "src" / "app.py").write_text(REPO_SOURCE_HEAD + "\n# uncommitted work\n", encoding="utf-8")
    status, err = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/fix-proposal/apply",
        "POST",
        {
            "run_id": run_id, "finding_id": finding_id, "proposal_id": proposal.proposal_id,
            "approval_token": _token(proposal, "apply"),
            "confirmed_patch_hash": proposal.patch_hash,
        },
    )
    assert status == 409
    assert err["error"] == "workspace_dirty"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8").endswith("# uncommitted work\n")
    assert "TODO(codeatlas)" not in (repo / "src" / "app.py").read_text(encoding="utf-8")


def test_untyped_apply_endpoints_never_exist(service):
    base_url, _manager, _repo = service
    status, err = _http_request(f"{base_url}/proposals/prop-anything/apply", "POST", {})
    assert status == 404
    assert err["error"] == "not_found"
    status, err = _http_request(f"{base_url}/proposals/prop-anything/approve", "POST", {})
    assert status == 404
    assert err["error"] == "not_found"
