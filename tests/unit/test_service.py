"""Unit tests for Phase 10A local HTTP service and ReviewStateManager."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.patching.proposal import generate_approval_token
from codeatlas.service import (
    CodeAtlasServer,
    ReviewStateManager,
    create_server,
    validate_service_repo_path,
)

runner = CliRunner()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _create_test_repo(root: Path) -> tuple[str, str]:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("def run(x):\n    return x\n", encoding="utf-8")

    def commit(msg: str) -> str:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", msg)
        return _git(root, "rev-parse", "HEAD")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")
    base_sha = commit("base commit")

    # Add secret finding in head
    (root / "src" / "app.py").write_text("def run(x):\n    token = 'AKIAIOSFODNN7EXAMPLE'\n    return x\n", encoding="utf-8")
    head_sha = commit("head commit with finding")

    return base_sha, head_sha


@pytest.fixture
def running_service(tmp_path: Path):
    """Start an in-memory CodeAtlas HTTP server on an ephemeral port."""
    manager = ReviewStateManager()
    server = create_server(host="127.0.0.1", port=0, state_manager=manager)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    yield base_url, manager

    server.shutdown()
    server.server_close()


def _http_request(url: str, method: str = "GET", data: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
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


def test_service_health(running_service):
    base_url, _ = running_service
    status, body = _http_request(f"{base_url}/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["service"] == "codeatlas-service"
    assert "timestamp" in body
    assert body["version"] == "0.1.0"
    assert body["pid"] > 0
    assert body["active_reviews"] == 0
    assert body["provider"] == "deterministic"


def test_health_reports_active_providers_without_review_content(running_service, tmp_path):
    from codeatlas.service.models import ReviewCreateRequest
    from codeatlas.service.state import ReviewRunRecord

    base_url, manager = running_service
    for name, provider in [("first", "mock"), ("second", "live")]:
        request = ReviewCreateRequest(repo="private-repo", review_provider=provider, provider_model="private-model")
        record = ReviewRunRecord(name, request, tmp_path)
        record.status = "reviewing"
        record.errors = ["credential must not appear in health"]
        manager._reviews[name] = record
    _, body = _http_request(f"{base_url}/health")
    assert body["active_reviews"] == 2
    assert body["provider"] == "mixed"
    assert "private" not in json.dumps(body)
    assert "credential" not in json.dumps(body)
    manager._reviews["first"].status = "completed"
    _, body = _http_request(f"{base_url}/health")
    assert body["active_reviews"] == 1
    assert body["provider"] == "live"


def test_profile_options_reuse_existing_review_pipeline(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from codeatlas.service.models import ReviewCreateRequest
    from codeatlas.service.state import ReviewRunRecord

    captured = {}

    def review(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(manifest=SimpleNamespace(
            policy_decisions=[], errors=[], packet_limitations=[], packet_truncated=False,
            tests_status="not_run", full_suite_status="not_run", findings=[],
        ))

    monkeypatch.setattr("codeatlas.service.state.run_review", review)
    request = ReviewCreateRequest(repo=str(tmp_path), review_provider="mock", provider_model="test-model",
                                  provider_timeout=7, max_findings=8, allow_patch_suggestions=True)
    record = ReviewRunRecord("profile-run", request, tmp_path)
    ReviewStateManager()._execute_review_run(record)
    assert record.status == "completed"
    assert captured["review_provider"] == "mock"
    assert captured["provider_model"] == "test-model"
    assert captured["provider_timeout"] == 7
    assert captured["allow_patch_suggestions"] is True
    assert "allow_isolated_apply" not in captured
    assert "run_tests" not in captured


def test_profile_max_findings_limits_display_without_losing_evidence(tmp_path):
    from codeatlas.service.models import ReviewCreateRequest
    from codeatlas.service.state import ReviewRunRecord

    manager = ReviewStateManager()
    record = ReviewRunRecord("bounded", ReviewCreateRequest(repo=str(tmp_path), max_findings=1), tmp_path)
    record.findings = [{"id": "one", "severity": "high"}, {"id": "two", "severity": "low"}]
    manager._reviews[record.run_id] = record
    assert len(manager.get_findings(record.run_id)) == 1
    assert manager.get_findings(record.run_id, severity="low")[0].id == "two"
    assert manager.get_review_status(record.run_id).finding_counts["total"] == 2
    assert manager.get_finding_detail(record.run_id, "two").id == "two"


@pytest.mark.parametrize("options", [{"provider_timeout": 0}, {"provider_timeout": 301}, {"review_provider": "unknown"}])
def test_invalid_profile_request_options_rejected(options):
    from pydantic import ValidationError
    from codeatlas.service.models import ReviewCreateRequest

    with pytest.raises(ValidationError):
        ReviewCreateRequest(repo="unused", **options)


def test_start_review_and_status_lifecycle(tmp_path: Path, running_service):
    base_url, manager = running_service
    repo_dir = tmp_path / "repo"
    base_sha, head_sha = _create_test_repo(repo_dir)

    status, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(repo_dir), "base": base_sha, "head": head_sha},
    )
    assert status == 201
    run_id = body["run_id"]
    assert run_id.startswith("rev-")

    # Poll status until completed
    for _ in range(30):
        s_code, s_body = _http_request(f"{base_url}/reviews/{run_id}")
        assert s_code == 200
        if s_body["status"] in {"completed", "failed"}:
            break
        time.sleep(0.1)

    assert s_body["status"] == "completed"
    assert s_body["finding_counts"]["total"] >= 1
    assert s_body["finding_counts"]["high"] >= 1
    assert s_body["repository"] == str(repo_dir.resolve())
    assert s_body["test_status"] == "not_run"


def test_list_and_filter_findings(tmp_path: Path, running_service):
    base_url, manager = running_service
    repo_dir = tmp_path / "repo"
    base_sha, head_sha = _create_test_repo(repo_dir)

    _, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(repo_dir), "base": base_sha, "head": head_sha},
    )
    run_id = body["run_id"]

    for _ in range(30):
        _, s_body = _http_request(f"{base_url}/reviews/{run_id}")
        if s_body["status"] == "completed":
            break
        time.sleep(0.1)

    # All findings
    status, f_body = _http_request(f"{base_url}/reviews/{run_id}/findings")
    assert status == 200
    assert f_body["total"] >= 1
    assert len(f_body["findings"]) >= 1
    finding = f_body["findings"][0]
    assert "file" in finding
    assert "claim" in finding
    assert "severity" in finding
    assert "evidence_strength" in finding

    # Filter by severity
    _, high_body = _http_request(f"{base_url}/reviews/{run_id}/findings?severity=high")
    assert high_body["filtered_total"] >= 1

    _, low_body = _http_request(f"{base_url}/reviews/{run_id}/findings?severity=low")
    assert low_body["filtered_total"] == 0

    # Filter by category
    _, sec_body = _http_request(f"{base_url}/reviews/{run_id}/findings?category=HARD_CODED_SECRET")
    assert sec_body["filtered_total"] >= 1


def test_finding_detail_and_context(tmp_path: Path, running_service):
    base_url, manager = running_service
    repo_dir = tmp_path / "repo"
    base_sha, head_sha = _create_test_repo(repo_dir)

    _, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(repo_dir), "base": base_sha, "head": head_sha},
    )
    run_id = body["run_id"]

    for _ in range(30):
        _, s_body = _http_request(f"{base_url}/reviews/{run_id}")
        if s_body["status"] == "completed":
            break
        time.sleep(0.1)

    _, f_body = _http_request(f"{base_url}/reviews/{run_id}/findings")
    finding_id = f_body["findings"][0]["id"]

    # Detail
    status, d_body = _http_request(f"{base_url}/reviews/{run_id}/findings/{finding_id}")
    assert status == 200
    assert d_body["id"] == finding_id
    assert d_body["severity"] == "high"
    assert d_body["category"] == "HARD_CODED_SECRET"
    assert "context" in d_body
    ctx = d_body["context"]
    assert "changed_lines" in ctx
    assert "evidence_sources" in ctx


def test_explain_finding(tmp_path: Path, running_service):
    base_url, manager = running_service
    repo_dir = tmp_path / "repo"
    base_sha, head_sha = _create_test_repo(repo_dir)

    _, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(repo_dir), "base": base_sha, "head": head_sha},
    )
    run_id = body["run_id"]

    for _ in range(30):
        _, s_body = _http_request(f"{base_url}/reviews/{run_id}")
        if s_body["status"] == "completed":
            break
        time.sleep(0.1)

    _, f_body = _http_request(f"{base_url}/reviews/{run_id}/findings")
    finding_id = f_body["findings"][0]["id"]

    status, exp = _http_request(
        f"{base_url}/findings/{finding_id}/explain",
        method="POST",
        data={"run_id": run_id},
    )
    assert status == 200
    assert exp["finding_id"] == finding_id
    assert "remediation_advice" in exp
    assert "Hardcoding secrets" in exp["explanation"]


def test_patch_proposal_and_validation(tmp_path: Path, running_service):
    base_url, manager = running_service
    repo_dir = tmp_path / "repo"
    base_sha, head_sha = _create_test_repo(repo_dir)

    _, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(repo_dir), "base": base_sha, "head": head_sha},
    )
    run_id = body["run_id"]

    for _ in range(30):
        _, s_body = _http_request(f"{base_url}/reviews/{run_id}")
        if s_body["status"] == "completed":
            break
        time.sleep(0.1)

    _, f_body = _http_request(f"{base_url}/reviews/{run_id}/findings")
    finding_id = f_body["findings"][0]["id"]

    # Propose draft fix (creates proposal only, never applies automatically)
    status, prop = _http_request(
        f"{base_url}/findings/{finding_id}/patch-proposal",
        method="POST",
        data={"run_id": run_id},
    )
    assert status == 201
    proposal_id = prop["proposal_id"]
    assert prop["status"] == "requires_human_approval"
    assert prop["approval_required"] is True

    # Validate without approval token (fails closed / unverified approval)
    v_status, v_res = _http_request(
        f"{base_url}/proposals/{proposal_id}/validate",
        method="POST",
        data={"approval_token": ""},
    )
    assert v_status == 200
    assert v_res["approval_verified"] is False

    # Validate with valid scoped approval token
    item = manager.get_proposal(proposal_id)
    assert item is not None
    proposal, _ = item

    token = generate_approval_token(
        proposal_id=proposal_id,
        base_commit=proposal.base_commit,
        patch_hash=proposal.patch_hash,
        allowed_paths=proposal.target_files,
        run_id="default",
    )

    v_status2, v_res2 = _http_request(
        f"{base_url}/proposals/{proposal_id}/validate",
        method="POST",
        data={"approval_token": token, "run_tests": False},
    )
    assert v_status2 == 200
    assert v_res2["approval_verified"] is True


def test_cancel_review(tmp_path: Path, running_service):
    base_url, manager = running_service
    repo_dir = tmp_path / "repo"
    base_sha, head_sha = _create_test_repo(repo_dir)

    _, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(repo_dir), "base": base_sha, "head": head_sha},
    )
    run_id = body["run_id"]

    c_status, c_body = _http_request(f"{base_url}/reviews/{run_id}/cancel", method="POST")
    assert c_status == 200
    assert c_body["status"] == "cancelled"


def test_dismiss_finding_locally(tmp_path: Path, running_service):
    base_url, manager = running_service
    repo_dir = tmp_path / "repo"
    base_sha, head_sha = _create_test_repo(repo_dir)

    _, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(repo_dir), "base": base_sha, "head": head_sha},
    )
    run_id = body["run_id"]

    for _ in range(30):
        _, s_body = _http_request(f"{base_url}/reviews/{run_id}")
        if s_body["status"] == "completed":
            break
        time.sleep(0.1)

    _, f_body = _http_request(f"{base_url}/reviews/{run_id}/findings")
    finding_id = f_body["findings"][0]["id"]

    # Dismiss
    status, d_resp = _http_request(
        f"{base_url}/reviews/{run_id}/findings/{finding_id}/dismiss",
        method="POST",
    )
    assert status == 200
    assert d_resp["dismissed"] is True

    # Now excluded from default list
    _, f_body2 = _http_request(f"{base_url}/reviews/{run_id}/findings")
    assert f_body2["filtered_total"] == 0

    # Included if include_dismissed=true
    _, f_body3 = _http_request(f"{base_url}/reviews/{run_id}/findings?include_dismissed=true")
    assert f_body3["filtered_total"] == 1


# --- Security & Boundary Tests ---


def test_security_rejects_non_git_repo(tmp_path: Path, running_service):
    base_url, _ = running_service
    not_git = tmp_path / "not-git"
    not_git.mkdir()

    status, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(not_git), "base": "main", "head": "HEAD"},
    )
    assert status == 400
    assert "not a valid Git repository" in body["message"]


def test_security_rejects_path_traversal(running_service):
    base_url, _ = running_service
    status, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": "../../etc/passwd", "base": "main", "head": "HEAD"},
    )
    assert status == 400


def test_security_no_arbitrary_shell_endpoint(running_service):
    base_url, _ = running_service
    status, body = _http_request(f"{base_url}/exec", method="POST", data={"cmd": "whoami"})
    assert status == 404


def test_security_no_arbitrary_file_read_endpoint(running_service):
    base_url, _ = running_service
    status, body = _http_request(f"{base_url}/files/etc/passwd", method="GET")
    assert status == 404


def test_security_rejects_malformed_json(running_service):
    base_url, _ = running_service
    req = urllib.request.Request(f"{base_url}/reviews", data=b"{invalid-json", method="POST")
    req.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400


def test_security_original_worktree_unchanged(tmp_path: Path, running_service):
    base_url, _ = running_service
    repo_dir = tmp_path / "repo"
    base_sha, head_sha = _create_test_repo(repo_dir)

    tree_before = _git(repo_dir, "rev-parse", "HEAD")
    status_before = _git(repo_dir, "status", "--porcelain")

    # Run review via service
    _, body = _http_request(
        f"{base_url}/reviews",
        method="POST",
        data={"repo": str(repo_dir), "base": base_sha, "head": head_sha},
    )
    run_id = body["run_id"]
    for _ in range(30):
        _, s_body = _http_request(f"{base_url}/reviews/{run_id}")
        if s_body["status"] == "completed":
            break
        time.sleep(0.1)

    assert _git(repo_dir, "rev-parse", "HEAD") == tree_before
    assert _git(repo_dir, "status", "--porcelain") == status_before


def test_cli_serve_help():
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output
    assert "127.0.0.1" in result.output
