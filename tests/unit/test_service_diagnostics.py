"""Bounded, redacted service diagnostics and independent local doctor behavior."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from codeatlas import __version__
from codeatlas.cli import app
from codeatlas.client import ClientError, CodeAtlasClient
from codeatlas.service import diagnostics as d
from codeatlas.service.server import create_server

SECRET = "private-token-repository-path-provider-transcript"
NAMES = [
    "health", "version", "database", "migration", "lock", "recovery", "jobs",
    "queue", "workers", "limits", "provider", "github", "artifacts", "retention", "limitations",
]


def row(status="completed", recovery="not_required"):
    return {
        "run_id": SECRET, "repository": SECRET, "base": SECRET, "head": SECRET,
        "status": status, "recovery_state": recovery, "progress_text": SECRET,
        "errors": [SECRET], "limitations": [SECRET],
    }


def responses(*, persistent=True, rows=None, total=None):
    rows = [] if rows is None else rows
    return {
        "/health": {"timestamp": SECRET, "pid": 1, "version": __version__, "active_reviews": 2,
                    "provider": "live", "credential": SECRET},
        "/capabilities": {"state_lifetime": "persistent" if persistent else "process",
                          "capabilities": [SECRET], "openapi_url": SECRET},
        "/reviews?limit=50&offset=0": {"total": len(rows) if total is None else total,
                                     "limit": 50, "offset": 0, "reviews": rows},
    }


def fake_api(monkeypatch, data):
    calls = []

    def request(self, method, endpoint, body=None, *, timeout=None):
        calls.append((method, endpoint, body, timeout))
        value = data[endpoint]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(CodeAtlasClient, "request", request)
    return calls


def by_name(report):
    return {check.name: check for check in report.checks}


def test_fixed_typed_redacted_persistent_report(monkeypatch):
    calls = fake_api(monkeypatch, responses(rows=[row(), row("recovery_failed", "recovery_failed")], total=100))
    report = d.collect_service_diagnostics("http://127.0.0.1:8765")
    checks = by_name(report)
    assert report.status == "warning"
    assert [check.name for check in report.checks] == NAMES
    assert checks["health"].metrics == {"active_reviews": 2}
    assert checks["version"].code == "version_match"
    assert checks["database"].code == "persistent_integrity_unknown"
    assert checks["jobs"].metrics["sampled_reviews"] == 2
    assert checks["jobs"].metrics["total_reviews"] == 100
    assert checks["jobs"].code == "jobs_sampled"
    assert checks["recovery"].status == "warning"
    assert checks["recovery"].metrics["recovery_failed"] == 1
    for name in ("database", "migration", "lock", "queue", "workers", "limits", "provider", "github", "artifacts", "retention"):
        assert checks[name].status == "unknown"
    assert SECRET not in report.model_dump_json()
    assert len(report.model_dump_json()) < 8192
    assert [(method, endpoint, body) for method, endpoint, body, _ in calls] == [
        ("GET", "/health", None), ("GET", "/capabilities", None),
        ("GET", "/reviews?limit=50&offset=0", None),
    ]
    assert all(0 < timeout <= 10 for _, _, _, timeout in calls)
    assert d.ServiceDiagnosticsResponse.model_validate(report.model_dump()).model_dump() == report.model_dump()


def test_process_storage_is_not_applicable(monkeypatch):
    fake_api(monkeypatch, responses(persistent=False))
    checks = by_name(d.collect_service_diagnostics("http://localhost:8765"))
    for name in ("database", "migration", "lock", "retention"):
        assert checks[name].status == "not_applicable"
    assert checks["recovery"].code == "sample_recovery_clear"


@pytest.mark.parametrize("endpoint,name", [("/health", "health"), ("/capabilities", "database"),
                                           ("/reviews?limit=50&offset=0", "jobs")])
@pytest.mark.parametrize("code", ["unreachable", "timeout", "incompatible_api", "invalid_response", "response_too_large", SECRET])
def test_endpoint_errors_are_stable_and_never_reflected(monkeypatch, endpoint, name, code):
    data = responses()
    data[endpoint] = ClientError(code, SECRET)
    calls = fake_api(monkeypatch, data)
    report = d.collect_service_diagnostics("http://localhost:8765")
    assert report.status == "error"
    assert by_name(report)[name].code == ("http_error" if code == SECRET else code)
    assert SECRET not in report.model_dump_json()
    assert len(calls) == (1 if endpoint == "/health" else 3)


@pytest.mark.parametrize("url", ["http://example.com", "http://user:secret@localhost", "https://localhost",
                                  "http://localhost?token=secret", "file:///tmp/db"])
def test_rejects_non_loopback_origins_without_requests(monkeypatch, url):
    calls = fake_api(monkeypatch, responses())
    report = d.collect_service_diagnostics(url)
    assert by_name(report)["health"].code == "invalid_service_url"
    assert calls == []
    assert "secret" not in report.model_dump_json()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_redacted(monkeypatch, timeout):
    calls = fake_api(monkeypatch, responses())
    assert by_name(d.collect_service_diagnostics("http://localhost", timeout=timeout))["health"].code == "invalid_timeout"
    assert not calls


def test_shared_deadline_stops_later_requests(monkeypatch):
    calls = fake_api(monkeypatch, responses())
    ticks = iter([0.0, 0.25, 1.0, 1.1])
    monkeypatch.setattr(d.time, "monotonic", lambda: next(ticks))
    report = d.collect_service_diagnostics("http://localhost", timeout=1)
    assert len(calls) == 1 and calls[0][3] == 0.75
    assert by_name(report)["database"].code == "diagnostic_deadline"
    assert by_name(report)["jobs"].code == "diagnostic_deadline"


@pytest.mark.parametrize("change", [
    {"reviews": [row()] * 51, "total": 51}, {"total": -1}, {"total": 2**40},
    {"limit": 200}, {"offset": 1}, {"reviews": [row()], "total": 0},
    {"reviews": [row("unknown-secret")]}, {"reviews": [row(recovery="unknown-secret")]},
])
def test_rejects_unbounded_or_invalid_job_metadata(monkeypatch, change):
    data = responses()
    data["/reviews?limit=50&offset=0"].update(change)
    fake_api(monkeypatch, data)
    report = d.collect_service_diagnostics("http://localhost")
    assert by_name(report)["jobs"].code == "invalid_response"
    assert SECRET not in report.model_dump_json()


@pytest.mark.parametrize("change,code", [
    ({"active_reviews": -1}, "invalid_response"), ({"active_reviews": 2**40}, "invalid_response"),
    ({"provider": SECRET}, "invalid_response"), ({"status": SECRET}, "unhealthy"),
    ({"service": SECRET}, "unhealthy"), ({"api_version": "2"}, "invalid_response"),
])
def test_health_does_not_reflect_untrusted_fields(monkeypatch, change, code):
    data = responses()
    data["/health"].update(change)
    fake_api(monkeypatch, data)
    report = d.collect_service_diagnostics("http://localhost")
    assert by_name(report)["health"].code == code
    assert SECRET not in report.model_dump_json()


def test_version_mismatch_is_warning_not_raw_version(monkeypatch):
    data = responses()
    data["/health"]["version"] = SECRET
    fake_api(monkeypatch, data)
    report = d.collect_service_diagnostics("http://localhost")
    assert by_name(report)["version"].code == "version_mismatch"
    assert report.status == "warning" and SECRET not in report.model_dump_json()


def test_metric_contract_is_bounded():
    for value in (-1, 2**40, "1", True):
        with pytest.raises(ValidationError):
            d.DiagnosticCheck(name="jobs", status="ok", code="jobs_listed", detail="Counts", metrics={"total_reviews": value})


def test_ordinary_doctor_never_constructs_client(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Ordinary doctor must not contact a service")

    monkeypatch.setattr(CodeAtlasClient, "__init__", forbidden)
    monkeypatch.setattr("codeatlas.cli_platform.git_executable", lambda: "git")
    monkeypatch.setattr("codeatlas.cli_platform.subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="git version test"))
    result = CliRunner().invoke(app, ["--service-url", "http://user:secret@example.com", "--json", "doctor"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["service_required"] is False
    assert {check["name"] for check in report["checks"]} == {"python", "git", "api_contract"}


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_service_error_has_full_report_and_failure_exit(monkeypatch, json_output):
    data = responses()
    data["/health"] = ClientError("unreachable", SECRET)
    fake_api(monkeypatch, data)
    args = (["--json"] if json_output else []) + ["doctor", "--service"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert SECRET not in result.output
    if json_output:
        report = json.loads(result.stdout)
        assert report["status"] == "error" and report["service_required"] is True
        assert [check["name"] for check in report["checks"]] == NAMES
    else:
        assert "Status: error" in result.stdout and "unreachable" in result.stdout


@pytest.mark.parametrize("persistent", [False, True])
def test_real_loopback_service_and_module_entrypoint(tmp_path, persistent):
    kwargs = {"database_path": tmp_path / "private-service.sqlite3"} if persistent else {}
    server = create_server(port=0, **kwargs)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "codeatlas", "--service-url", f"http://127.0.0.1:{server.server_port}",
             "--request-timeout", "3", "--json", "doctor", "--service"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        report = json.loads(result.stdout)
        assert report["status"] == "warning"
        checks = {check["name"]: check for check in report["checks"]}
        assert checks["health"]["status"] == "ok"
        assert checks["jobs"]["metrics"]["total_reviews"] == 0
        assert checks["database"]["status"] == ("unknown" if persistent else "not_applicable")
        assert str(tmp_path) not in result.stdout
        assert server.state_manager.list_reviews().total == 0
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("mode,expected", [("redirect", "http_error"), ("malformed", "invalid_response"),
                                          ("version", "incompatible_api"), ("oversized", "response_too_large")])
def test_real_http_client_boundaries(mode, expected):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            body = b"not json" if mode == "malformed" else b"{}"
            if mode == "oversized":
                body = b"x" * (2 * 1024 * 1024 + 1)
            self.send_response(302 if mode == "redirect" else 200)
            self.send_header("Location", "http://example.com/private")
            self.send_header("CodeAtlas-API-Version", "2" if mode == "version" else "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        report = d.collect_service_diagnostics(f"http://127.0.0.1:{server.server_port}", timeout=2)
        assert by_name(report)["health"].code == expected
        assert calls == ["/api/v1/health"]
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
