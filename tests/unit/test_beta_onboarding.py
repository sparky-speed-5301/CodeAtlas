from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from codeatlas import __version__, onboarding
from codeatlas.cli import app
from codeatlas.client import ClientError
from codeatlas.service.models import StorageHealthResponse


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.name", "Onboarding Test")
    git("config", "user.email", "test@example.com")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git("add", "calc.py")
    git("commit", "-m", "fixture")
    return tmp_path


def request(repository: Path, **overrides: object) -> onboarding.OnboardingRequest:
    return onboarding.OnboardingRequest.model_validate({
        "repository": str(repository), "base": "HEAD", **overrides,
    })


def checks(report: onboarding.OnboardingReport) -> dict:
    return {check.name: check for check in report.checks}


def test_clean_repository_onboarding_is_bounded_and_read_only(repository: Path) -> None:
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repository, check=True, capture_output=True).stdout
    report = onboarding.onboard_repository(request(repository))
    assert report.status == "ready_with_limitations"
    assert report.beta_ready and not report.production_ready
    assert report.languages == ("python",)
    assert len(report.checks) == 18
    assert checks(report)["offline_benchmark"].status == "ok"
    assert checks(report)["cleanup"].status == "ok"
    assert str(repository) not in report.model_dump_json()
    assert len(report.model_dump_json().encode()) < 32768
    after = subprocess.run(["git", "status", "--porcelain"], cwd=repository, check=True, capture_output=True).stdout
    assert before == after


def test_dirty_worktree_blocks_without_discarding_files(repository: Path) -> None:
    dirty = repository / "private.txt"
    dirty.write_text("private source and token", encoding="utf-8")
    report = onboarding.onboard_repository(request(repository))
    assert report.status == "blocked"
    assert checks(report)["cleanliness"].code == "dirty_worktree"
    assert dirty.read_text() == "private source and token"
    assert "private.txt" not in report.model_dump_json()


@pytest.mark.parametrize("overrides", [
    {"base": "missing-revision"},
    {"expected_head_sha": "a" * 40},
    {"expected_base_sha": "b" * 40},
])
def test_stale_revisions_block(repository: Path, overrides: dict) -> None:
    report = onboarding.onboard_repository(request(repository, **overrides))
    assert report.status == "blocked"
    assert checks(report)["revisions"].code == "stale_revision"


def test_repository_identity_mismatch_blocks(repository: Path) -> None:
    report = onboarding.onboard_repository(request(repository, expected_repository_id="repo-" + "a" * 64))
    assert report.status == "blocked"
    assert checks(report)["repository"].code == "repository_identity_mismatch"


def test_invalid_beta_configuration_is_structured_and_private(repository: Path, tmp_path: Path) -> None:
    config = tmp_path / "private-config.json"
    config.write_text('{"api_key":"private-provider-token"}', encoding="utf-8")
    report = onboarding.onboard_repository(request(repository), config_path=config)
    assert report.status == "blocked"
    assert checks(report)["configuration"].code == "invalid_beta_configuration"
    assert "private-provider-token" not in report.model_dump_json()


def test_live_provider_requires_invocation_authorization(repository: Path) -> None:
    provider = repository / "provider.json"
    provider.write_text(json.dumps({"provider_name": "live", "model_name": "model", "api_key_env_var": "BETA_TEST_KEY"}), encoding="utf-8")
    config = repository / "beta.json"
    config.write_text(json.dumps({"provider_mode": "live_provider", "live_provider": {"model_name": "model", "provider_config_path": "provider.json", "api_key_env_var": "BETA_TEST_KEY"}}), encoding="utf-8")
    report = onboarding.onboard_repository(request(repository), config_path=config)
    assert report.status == "blocked"
    assert checks(report)["live_authorization"].code == "live_provider_not_authorized"
    assert checks(report)["provider"].code == "missing_provider"


def test_unavailable_test_tool_is_not_readiness(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "_test_tools", lambda *args: False)
    report = onboarding.onboard_repository(request(repository))
    assert report.status == "unavailable"
    assert checks(report)["test_tool"].code == "unavailable_test_tool"


@pytest.mark.parametrize("url", ["http://example.com", "https://localhost", "http://localhost/api"])
def test_service_origin_rejected_without_remote_requests(repository: Path, url: str) -> None:
    report = onboarding.onboard_repository(request(repository, service_url=url))
    assert report.status == "unavailable"
    assert checks(report)["service"].code == "unavailable_service"


def fake_service(monkeypatch: pytest.MonkeyPatch, storage: dict, *, version: str = __version__) -> list:
    calls = []

    def get(self, method, endpoint, body=None, **kwargs):
        calls.append((method, endpoint, body))
        if endpoint == "/health":
            return {"status": "ok", "service": "codeatlas-service", "version": version, "timestamp": "", "pid": 1}
        if endpoint == "/capabilities":
            return {"state_lifetime": storage.get("state_lifetime", "persistent")}
        return storage

    monkeypatch.setattr(onboarding.CodeAtlasClient, "request", get)
    return calls


def test_owned_storage_health_and_service_connectivity(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_service(monkeypatch, {"state_lifetime": "persistent", "status": "ok", "code": "ok"})
    report = onboarding.onboard_repository(request(repository, service_url="http://127.0.0.1:8765", extension_version=__version__))
    assert report.status == "ready_with_limitations"
    assert checks(report)["database"].status == "ok"
    assert checks(report)["migration"].status == "ok"
    assert calls == [("GET", "/health", None), ("GET", "/capabilities", None), ("GET", "/diagnostics/storage", None)]


@pytest.mark.parametrize("code", ["corrupted_database", "migration_invalid"])
def test_storage_failure_blocks_without_opening_database(repository: Path, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    fake_service(monkeypatch, {"state_lifetime": "persistent", "status": "error", "code": code})
    report = onboarding.onboard_repository(request(repository, service_url="http://localhost:8765"))
    assert report.status == "blocked"
    assert checks(report)["database"].code == code


def test_extension_mismatch_blocks(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service(monkeypatch, {"state_lifetime": "process", "status": "not_applicable", "code": "not_applicable"})
    report = onboarding.onboard_repository(request(repository, service_url="http://localhost:8765", extension_version="99.0.0"))
    assert report.status == "blocked"
    assert checks(report)["extension_api"].code == "extension_service_mismatch"


def test_storage_endpoint_absence_is_explicit_limitation(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service(monkeypatch, {"error": "private-server-error"})
    report = onboarding.onboard_repository(request(repository, service_url="http://localhost:8765"))
    assert report.status == "unavailable"
    assert "private-server-error" not in report.model_dump_json()


def test_service_provider_mode_mismatch_blocks(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service(monkeypatch, {"state_lifetime": "persistent", "status": "ok", "code": "ok"})
    original = onboarding.CodeAtlasClient.request

    def get(self, method, endpoint, **kwargs):
        payload = original(self, method, endpoint, **kwargs)
        if endpoint == "/health":
            payload["provider"] = "live"
        return payload

    monkeypatch.setattr(onboarding.CodeAtlasClient, "request", get)
    report = onboarding.onboard_repository(request(repository, service_url="http://localhost:8765"))
    assert report.status == "blocked"
    assert checks(report)["provider"].code == "invalid_provider_configuration"


def test_unreachable_service_is_unavailable(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise ClientError("unreachable", "private raw error")

    monkeypatch.setattr(onboarding.CodeAtlasClient, "request", fail)
    report = onboarding.onboard_repository(request(repository, service_url="http://localhost:8765"))
    assert report.status == "unavailable"
    assert "private raw error" not in report.model_dump_json()


def test_github_permission_failure_has_stable_private_code(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = onboarding._probe
    original_which = onboarding.shutil.which
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: "gh" if name == "gh" else original_which(name))

    def probe(command, **kwargs):
        if command[0] == "gh":
            return 1, b"private-token-error"
        return original(command, **kwargs)

    monkeypatch.setattr(onboarding, "_probe", probe)
    report = onboarding.onboard_repository(request(repository, github_repository="owner/repo", github_pr=1))
    assert report.status == "blocked"
    assert checks(report)["github"].code == "github_permission_failure"
    assert "private-token-error" not in report.model_dump_json()


def test_probe_has_output_bound_and_timeout(repository: Path) -> None:
    with pytest.raises(onboarding.ProbeUnavailable):
        onboarding._probe([sys.executable, "-c", "print('x'*100000)"], cwd=repository, deadline=time.monotonic() + 5)
    with pytest.raises(onboarding.ProbeUnavailable):
        onboarding._probe([sys.executable, "-c", "import time; time.sleep(5)"], cwd=repository, deadline=time.monotonic() + 0.1)


def test_readiness_contract_rejects_contradictions_and_unknown_fields() -> None:
    check = onboarding.OnboardingCheck(name="python", status="blocked", code="unsupported_python", detail="Unsupported Python.")
    with pytest.raises(ValidationError):
        onboarding.OnboardingReport(status="ready", beta_ready=True, checks=(check,))
    with pytest.raises(ValidationError):
        onboarding.OnboardingRequest(repository=".", base="HEAD", automatic_apply=True)
    ready = onboarding.OnboardingReport(status="ready", beta_ready=True, checks=(check.model_copy(update={"status": "ok", "code": "ok"}),))
    assert ready.status == "ready"


def test_cli_onboarding_export_and_stable_errors(repository: Path, tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    result = CliRunner().invoke(app, ["--json", "beta-onboard", "--repo", str(repository), "--base", "HEAD", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ready_with_limitations"
    assert output.is_file()
    repeated = CliRunner().invoke(app, ["--json", "beta-onboard", "--repo", str(repository), "--base", "HEAD", "--output", str(output)])
    assert repeated.exit_code == 2
    assert str(output) not in repeated.output


def test_storage_health_model_never_accepts_raw_errors() -> None:
    with pytest.raises(ValidationError):
        StorageHealthResponse(state_lifetime="persistent", status="error", code="private-error", raw_source="private")


def test_contradictory_storage_health_is_unavailable(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service(monkeypatch, {"state_lifetime": "persistent", "status": "error", "code": "ok"})
    report = onboarding.onboard_repository(request(repository, service_url="http://localhost:8765"))
    assert report.status == "unavailable"
    assert checks(report)["database"].status == "unavailable"


def test_old_service_storage_endpoint_is_an_explicit_limitation(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service(monkeypatch, {"state_lifetime": "persistent", "status": "ok", "code": "ok"})
    original = onboarding.CodeAtlasClient.request

    def get(self, method, endpoint, **kwargs):
        if endpoint == "/diagnostics/storage":
            raise ClientError("private-error", "private server output", status=404)
        return original(self, method, endpoint, **kwargs)

    monkeypatch.setattr(onboarding.CodeAtlasClient, "request", get)
    report = onboarding.onboard_repository(request(repository, service_url="http://localhost:8765"))
    assert report.status == "ready_with_limitations"
    assert checks(report)["database"].code == "storage_health_not_exposed"
    assert "private" not in report.model_dump_json()


def test_unsupported_source_language_is_blocked(repository: Path) -> None:
    (repository / "calc.py").rename(repository / "calc.unknown")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "unsupported fixture"], cwd=repository, check=True, capture_output=True)
    report = onboarding.onboard_repository(request(repository))
    assert report.status == "blocked"
    assert checks(report)["language"].code == "unsupported_language"


def test_cleanup_failure_is_not_silently_ready(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "_cleanup_smoke", lambda: False)
    report = onboarding.onboard_repository(request(repository))
    assert report.status == "unavailable"
    assert checks(report)["cleanup"].code == "cleanup_unavailable"


def test_github_preflight_checks_exact_pins_without_claiming_review_execution(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = onboarding.onboard_repository(request(repository))
    original = onboarding._probe
    original_which = onboarding.shutil.which
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: "gh" if name == "gh" else original_which(name))
    calls = []

    def probe(command, **kwargs):
        if command[0] == "gh":
            calls.append(command)
            return 0, json.dumps({"number": 1, "baseRefOid": baseline.base_sha, "headRefOid": baseline.head_sha}).encode()
        return original(command, **kwargs)

    monkeypatch.setattr(onboarding, "_probe", probe)
    report = onboarding.onboard_repository(request(repository, github_repository="owner/repo", github_pr=1))
    assert report.status == "ready_with_limitations"
    assert checks(report)["github"].code == "github_review_not_run"
    assert len(calls) == 1 and calls[0][1:3] == ["pr", "view"]


def test_offline_smoke_never_executes_commands_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("offline smoke must not execute commands or use network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert onboarding._offline_smoke()


def test_missing_git_is_a_safe_unavailable_result(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codeatlas import beta
    from codeatlas.git.errors import GitExecutableError

    def missing():
        raise GitExecutableError("private installation detail")

    monkeypatch.setattr(onboarding, "git_executable", missing)
    monkeypatch.setattr(beta, "git_executable", missing)
    report = onboarding.onboard_repository(request(repository))
    assert report.status == "unavailable"
    assert "private installation detail" not in report.model_dump_json()


def test_onboarding_cli_against_owned_persistent_service(repository: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    import threading

    from codeatlas.service.server import create_server

    database = tmp_path_factory.mktemp("onboarding-service") / "service.sqlite3"
    server = create_server(port=0, database_path=database)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    arguments = [
        "--json", "--service-url", f"http://127.0.0.1:{server.server_port}",
        "beta-onboard", "--repo", str(repository), "--base", "HEAD", "--service",
    ]
    try:
        result = CliRunner().invoke(app, arguments)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        observed = {item["name"]: item for item in payload["checks"]}
        assert observed["database"]["status"] == "ok"
        assert observed["migration"]["status"] == "ok"
        assert server.state_manager.list_reviews().total == 0
        server.state_manager.persistence._db.execute("PRAGMA user_version=999")
        failed = CliRunner().invoke(app, arguments)
        assert failed.exit_code == 1, failed.output
        assert json.loads(failed.stdout)["status"] == "blocked"
        assert str(database) not in failed.stdout
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()


def test_provider_non_object_config_returns_stable_failure(repository: Path) -> None:
    provider = repository / "provider.json"
    provider.write_text("[]", encoding="utf-8")
    config = repository / "beta.json"
    config.write_text(json.dumps({"provider_mode": "live_provider", "live_provider": {"model_name": "model", "provider_config_path": "provider.json"}}), encoding="utf-8")
    report = onboarding.onboard_repository(request(repository, allow_live_provider=True), config_path=config)
    assert report.status == "blocked"
    assert checks(report)["provider"].code == "invalid_provider_configuration"
    assert report.provider_mode == "live_provider"
    assert report.live_provider_authorized is True

