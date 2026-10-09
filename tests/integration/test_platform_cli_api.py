"""Client-independent contracts exercised over real loopback HTTP and Git repos."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import jsonschema
import pytest
from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.client import ClientError, CodeAtlasClient
from codeatlas.service.models import ReviewCreateRequest
from codeatlas.service.server import create_server

runner = CliRunner()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "app.py").write_text("def run(x):\n    return x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base")
    (repo / "app.py").write_text("def render(user):\n    print(user.password)\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "head")
    return repo


@pytest.fixture
def service():
    server = create_server(port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}"
    yield url, CodeAtlasClient(url), server
    server.shutdown()
    worker.join(timeout=5)
    server.server_close()


def cli(url: str, *args: str, exit_code: int = 0):
    result = runner.invoke(app, ["--service-url", url, "--json", *args])
    assert result.exit_code == exit_code, (result.output, result.exception)
    return json.loads(result.stdout) if result.stdout.strip() else json.loads(result.stderr)


def request(url: str, method="GET", body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response), response.headers


def test_cli_complete_repair_lifecycle_and_cross_client_visibility(service, repository):
    url, api, _ = service
    original = (repository / "app.py").read_bytes()
    head = git(repository, "rev-parse", "HEAD")
    index = (repository / ".git" / "index").read_bytes()
    started = cli(url, "reviews", "start", "--repo", str(repository), "--base", "HEAD~1", "--wait")
    run_id = started["run_id"]
    assert started["status"] == "completed"
    assert api.get_review(run_id).head_commit == head
    assert cli(url, "reviews", "list")["reviews"][0]["run_id"] == run_id
    findings = cli(url, "findings", "list", run_id)["findings"]
    finding_id = findings[0]["id"]
    assert cli(url, "findings", "show", run_id, finding_id)["id"] == finding_id
    assert cli(url, "findings", "explain", run_id, finding_id)["finding_id"] == finding_id
    assert cli(url, "findings", "feedback", run_id, finding_id, "useful")["feedback"] == "useful"
    assert api.get_finding(run_id, finding_id).feedback == "useful"
    assert cli(url, "findings", "feedback", run_id, finding_id, "clear")["feedback"] is None
    fix = ["fix", "--run-id", run_id, "--finding-id", finding_id]
    assert cli(url, *fix, "eligibility")["eligible"] is True
    generated = cli(url, *fix, "generate")
    fix += ["--proposal-id", generated["proposal_id"]]
    assert api.get_fix(run_id, finding_id).patch_hash == generated["patch_hash"]
    assert cli(url, *fix, "inspect")["patch_text"] == generated["patch_text"]
    assert cli(url, *fix, "validate", exit_code=1)["error"] == "validation_not_approved"
    assert cli(url, *fix, "apply", "--approve", "--confirmed-patch-hash", generated["patch_hash"], exit_code=1)["error"] == "apply_not_validated"
    assert (repository / "app.py").read_bytes() == original
    approved = cli(url, *fix, "approve-validation", "--approve")
    assert approved["approval_verified"] is True
    validation = cli(url, *fix, "validate")
    assert validation["valid"] and validation["cleanup_status"] == "completed"
    assert (repository / "app.py").read_bytes() == original
    assert cli(url, *fix, "apply", "--approve", "--confirmed-patch-hash", "f" * 64, exit_code=1)["error"] == "apply_confirmation_invalid"
    applied = cli(url, *fix, "apply", "--approve", "--confirmed-patch-hash", generated["patch_hash"])
    assert applied["apply_status"] == "applied"
    assert applied["resulting_diff_hash"] == validation["resulting_diff_hash"]
    assert (repository / "app.py").read_bytes() != original
    assert cli(url, *fix, "history")["revert_available"] is True
    reverted = cli(url, *fix, "revert", "--confirmed-patch-hash", generated["patch_hash"])
    assert reverted["apply_status"] == "reverted"
    assert (repository / "app.py").read_bytes() == original
    assert git(repository, "rev-parse", "HEAD") == head
    assert (repository / ".git" / "index").read_bytes() == index
    assert "approval_token" not in json.dumps([approved, validation, applied, reverted])


def test_api_openapi_contracts_and_legacy_aliases(service, repository):
    url, api, _ = service
    document = api.request("GET", "/openapi.json")
    assert document["openapi"] == "3.1.0"
    assert document["servers"] == [{"url": "/api/v1"}]
    for schema in document["components"]["schemas"].values():
        jsonschema.Draft202012Validator.check_schema(schema)
    started = api.start_review(ReviewCreateRequest(repo=str(repository), base="HEAD~1"))
    result = api.wait_for_review(started.run_id)
    assert result.status == "completed"
    for path, contract in [("/health", "HealthResponse"), ("/capabilities", "CapabilitiesResponse"),
                           (f"/reviews/{started.run_id}", "ReviewStatusResponse"),
                           (f"/reviews/{started.run_id}/findings", "FindingsListResponse")]:
        status, versioned, headers = request(url + "/api/v1" + path)
        assert status == 200 and headers["CodeAtlas-API-Version"] == "1"
        _, legacy, _ = request(url + path)
        if path != "/health":
            assert legacy == versioned
        schema = {"$ref": f"#/components/schemas/{contract}", "components": document["components"]}
        jsonschema.Draft202012Validator(schema).validate(versioned)
    assert api.capabilities().state_lifetime == "process"
    capability_data = api.capabilities()
    assert {item["language"] for item in capability_data.model_dump()["language_capabilities"]} == {
        "python", "javascript", "typescript", "go", "java", "rust",
    }
    assert all(item["parser"] in {"available", "unavailable", "unsupported", "not_run", "passed", "failed", "blocked"}
               for item in capability_data.model_dump()["language_capabilities"])
    # Cancelling a finished run must not claim it was cancelled.
    assert api.cancel_review(started.run_id).status == "completed"


@pytest.mark.parametrize("path,method,body,status,code", [
    ("/api/v2/health", "GET", None, 404, "unsupported_api_version"),
    ("/api/v1/missing", "GET", None, 404, "not_found"),
    ("/api/v1/health", "POST", {}, 405, "method_not_allowed"),
    ("/api/v1/reviews", "DELETE", None, 405, "method_not_allowed"),
    ("/api/v1/reviews", "POST", {"repo": ".", "approve": True}, 400, "bad_request"),
    ("/api/v1/reviews", "POST", {"repo": ".", "allow_patch_suggestions": "false"}, 400, "bad_request"),
    ("/api/v1/reviews", "POST", [], 400, "bad_request"),
    ("/api/v1/reviews?limit=201", "GET", None, 400, "bad_request"),
    ("/api/v1/reviews?offset=-1", "GET", None, 400, "bad_request"),
    ("/api/v1/reviews?limit=oops", "GET", None, 400, "bad_request"),
])
def test_v1_errors_are_structured(service, path, method, body, status, code):
    url, _, _ = service
    actual, response, _ = request(url + path, method, body)
    assert actual == status and response["error"] == code
    assert set(response) == {"error", "message"}


def test_invalid_approval_payload_is_not_reflected(service):
    url, _, _ = service
    secret = "operator-token-must-not-be-printed"
    status, body, _ = request(url + "/api/v1/fix-proposals/unused/approve-validation", "POST",
                              {"run_id": secret, "approval_token": {"secret": secret}})
    assert status == 400
    assert secret not in json.dumps(body)


def test_multiple_embedded_services_do_not_share_state(service, repository):
    _, api, _ = service
    started = api.start_review(ReviewCreateRequest(repo=str(repository), base="HEAD~1"))
    api.wait_for_review(started.run_id)
    other = create_server(port=0)
    try:
        assert api.get_review(started.run_id).status == "completed"
        assert other.state_manager.get_review(started.run_id) is None
    finally:
        other.server_close()


def test_cli_json_failures_and_explicit_approval(service):
    url, _, _ = service
    assert cli(url, "reviews", "status", "missing", exit_code=1)["error"] == "not_found"
    for command in ("approve-validation", "apply"):
        args = ["fix", "--run-id", "missing", "--finding-id", "missing", "--proposal-id", "missing", command]
        if command == "apply":
            args += ["--confirmed-patch-hash", "f" * 64]
        result = runner.invoke(app, ["--service-url", url, *args])
        assert result.exit_code == 2 and "--approve" in result.output


def test_standalone_cli_json_and_ci_exit_codes(repository):
    command = [sys.executable, "-m", "codeatlas", "review", "--repo", str(repository),
               "--base", "HEAD~1", "--json", "--fail-on", "high"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 3, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["findings"] and not manifest["errors"]
    # No service is needed, and the completed manifest is usable despite the CI finding exit.
    result = subprocess.run(command[:-2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and json.loads(result.stdout)["findings"]
    result = subprocess.run([sys.executable, "-m", "codeatlas", "--version"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.startswith("CodeAtlas ")


def test_doctor_is_local_and_machine_readable():
    result = runner.invoke(app, ["--json", "doctor"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "ok"
    assert data["service_required"] is False
    assert {check["name"] for check in data["checks"]} == {"python", "git", "api_contract"}
    assert all(check["status"] == "ok" for check in data["checks"])


@pytest.mark.parametrize("url", ["https://localhost:8765", "http://example.com", "http://user:secret@localhost",
                                "http://localhost/api/v1", "http://localhost?token=secret", "file:///tmp/server"])
def test_client_rejects_invalid_origins(url):
    with pytest.raises(ClientError) as error:
        CodeAtlasClient(url)
    assert error.value.code == "invalid_service_url" and "secret" not in str(error.value)


@pytest.mark.parametrize("mode,expected", [
    ("malformed", "invalid_response"), ("oversized", "response_too_large"),
    ("redirect", "http_error"), ("wrong_version", "incompatible_api"),
    ("timeout", "timeout"), ("interrupted", "invalid_response"),
])
def test_client_bounds_and_no_mutation_retries(mode, expected):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if mode == "timeout":
                time.sleep(0.15)
                return
            body = b"{" if mode == "malformed" else b"{}"
            if mode == "oversized":
                body = b" " * (2 * 1024 * 1024 + 1)
            self.send_response(302 if mode == "redirect" else 200)
            self.send_header("CodeAtlas-API-Version", "2" if mode == "wrong_version" else "1")
            self.send_header("Location", "/must-not-be-followed")
            self.send_header("Content-Length", str(len(body) + (10 if mode == "interrupted" else 0)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        api = CodeAtlasClient(f"http://127.0.0.1:{server.server_port}", timeout=0.03 if mode == "timeout" else 10)
        with pytest.raises(ClientError) as error:
            api.request("POST", "/reviews", {})
        assert error.value.code == expected
        assert calls == ["/api/v1/reviews"]
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_cli_configures_structured_repair_without_network(monkeypatch, tmp_path):
    captured = {}

    def create_server(host, port, state_manager):
        captured["manager"] = state_manager
        return SimpleNamespace(serve_forever=lambda: None, server_close=lambda: None)

    monkeypatch.setattr("codeatlas.service.server.create_server", create_server)
    config = tmp_path / "repair.json"
    config.write_text(json.dumps({"mode": "online", "enabled": True, "max_repair_attempts": 4,
                                 "deterministic_fallback": False}), encoding="utf-8")
    result = runner.invoke(app, ["serve", "--repair-config", str(config)])
    assert result.exit_code == 0, result.output
    provider = captured["manager"].repair_provider
    assert provider.requests_made == 0
    assert provider.config.max_repair_attempts == 4
    assert provider.config.deterministic_fallback is False
    config.write_text('{"api_key": "credential-must-not-appear"}', encoding="utf-8")
    result = runner.invoke(app, ["serve", "--repair-config", str(config)])
    assert result.exit_code == 1 and "credential-must-not-appear" not in result.output


def test_ci_threshold_ignores_suppressed_findings():
    from codeatlas.cli_platform import FailOn, findings_fail

    assert not findings_fail([
        {"severity": "high", "quality_decision": decision}
        for decision in ("abstain", "suppress_duplicate", "suppress_low_evidence")
    ], FailOn.high)
    assert findings_fail([{"severity": "blocker", "quality_decision": "review_only"}], FailOn.high)
