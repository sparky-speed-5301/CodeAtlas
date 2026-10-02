"""Integration tests for Phase 7A: live provider wired into the review pipeline."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

import pytest  # noqa: F401  (pytest fixtures available)
from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.orchestrator.review import run_review
from codeatlas.providers import FakeTransport


FAKE_KEY = "sk-fake-integration-key-9876543210"

BANNER_LINES = [
    "CodeAtlas live review",
    "Provider: live",
    "Model: test-model",
    "Repository: work",
    "Changed files: 1",
    "Packet bytes: ",
    "Execution: review-only",
    "Patches: disabled",
]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return result.stdout.strip()


def _repo_with_change(root: Path) -> tuple[str, str]:
    """Create a git repo whose second commit adds a division-by-zero bug."""
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text(
        "def compute(items):\n    total = 0\n    for i in items:\n        total += i\n    return total\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "before")
    base = _git(root, "rev-parse", "HEAD")

    time.sleep(0.05)
    (root / "src" / "app.py").write_text(
        "def compute(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        total += i\n"
        "        if total > 10:\n"
        "            total = total // 0\n"
        "    return total\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "after")
    head = _git(root, "rev-parse", "HEAD")
    return base, head


def _provider_response() -> dict:
    return {
        "summary": "One real defect found.",
        "findings": [{
            "id": "CA-REV-IT-1",
            "file": "src/app.py",
            "start_line": 5,
            "end_line": 6,
            "severity": "high",
            "category": "LOGIC_BUG",
            "claim": "Division by zero when total exceeds the threshold",
            "impact": "compute raises ZeroDivisionError at runtime",
            "evidence": ["changed lines add 'total = total // 0'"],
            "evidence_strength": "supported",
            "confidence": 0.92,
            "limitations": [],
            "status": "detected",
            "fixability": "review_required",
            "provenance": {"origin": "reviewer"},
        }],
        "limitations": [],
        "abstentions": [],
    }


def _run_live(work: Path, base: str, head: str, **kwargs):
    defaults = dict(
        assemble_review_packet=True,
        review_provider="live",
        provider_model="test-model",
        provider_transport=FakeTransport(simulated_response=_provider_response(), latency_ms=4.0),
        evidence_output=kwargs.pop("evidence_output", None),
    )
    defaults.update(kwargs)
    return run_review(work, base=base, head=head, **defaults)


# ---------------------------------------------------------------------------

def test_live_review_end_to_end(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    result = _run_live(tmp_path / "work", base, head)
    manifest = result.manifest

    assert manifest.provider_name == "live"
    assert manifest.live_provider_enabled is True
    assert manifest.model_name == "test-model"
    assert manifest.provider_output_valid is True
    assert manifest.provider_request_id == "fake-req-1"
    assert manifest.provider_latency_ms is not None
    assert "estimated_cost" in manifest.provider_usage
    assert any(e == "request_started" for e in manifest.provider_safety_events)
    assert "patches_disabled" in manifest.provider_safety_events
    assert "repository_execution_disabled" in manifest.provider_safety_events

    # The provider finding merged into user-facing findings and re-ranked.
    assert any(f["id"] == "CA-REV-IT-1" for f in manifest.merged_findings)
    # High security finding triggers human approval after policy re-evaluation.
    final_policy = manifest.policy_decisions[-1]
    assert final_policy["decision"] == "requires_human_approval"

    # No patches, no repository execution, no tests executed.
    assert manifest.patch_proposals == []
    assert all(t.startswith("git ") for t in manifest.tools_run)
    assert manifest.tests_run == []


def test_evidence_events_and_no_secrets_in_artifacts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEATLAS_API_KEY", FAKE_KEY)
    work = tmp_path / "work"
    base, head = _repo_with_change(work)
    evidence_path = tmp_path / "evidence.jsonl"
    manifest_path = tmp_path / "manifest.json"

    _run_live(work, base, head, evidence_output=evidence_path, manifest_output=manifest_path)

    events = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines() if line]
    names = [e["event"] for e in events]
    assert "live_provider_configured" in names
    assert "live_provider_request_started" in names
    assert "live_provider_request_completed" in names
    assert "live_review_completed" in names

    evidence_text = evidence_path.read_text(encoding="utf-8")
    manifest_text = manifest_path.read_text(encoding="utf-8")
    for artifact in (evidence_text, manifest_text):
        assert FAKE_KEY not in artifact
        assert "Authorization" not in artifact
        assert "Bearer" not in artifact


def test_banner_is_fixed_and_safe(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    banners: list[str] = []
    _run_live(tmp_path / "work", base, head, banner_callback=banners.append)

    assert len(banners) == 1
    lines = banners[0].splitlines()
    assert lines[:5] == BANNER_LINES[:5]
    assert lines[5].startswith("Packet bytes: ")
    assert lines[6:] == BANNER_LINES[6:]
    # No prompt or key material ever reaches the banner.
    assert "UNTRUSTED" not in banners[0]
    assert "compute" not in banners[0]


def test_original_repository_unchanged(tmp_path: Path):
    work = tmp_path / "work"
    base, head = _repo_with_change(work)
    files_before = {p: p.read_text(encoding="utf-8") for p in work.rglob("*.py")}

    _run_live(work, base, head)

    assert _git(work, "rev-parse", "HEAD") == head
    assert _git(work, "status", "--porcelain") == ""
    files_after = {p: p.read_text(encoding="utf-8") for p in work.rglob("*.py")}
    assert files_after == files_before


def test_missing_credentials_fail_clearly_without_network(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CODEATLAS_API_KEY", raising=False)
    work = tmp_path / "work"
    base, head = _repo_with_change(work)

    result = _run_live(work, base, head, provider_transport=None)
    manifest = result.manifest
    assert manifest.live_provider_enabled is True
    assert any("missing credentials" in e.lower() for e in manifest.errors)
    assert any("CODEATLAS_API_KEY" in e for e in manifest.errors)
    assert manifest.provider_output_valid is None
    assert manifest.merged_findings == []


def test_policy_cannot_be_changed_by_provider_output(tmp_path: Path):
    response = _provider_response()
    response["policy_decision"] = "allowed_by_model"
    response["policy_override"] = True
    base, head = _repo_with_change(tmp_path / "work")
    result = _run_live(tmp_path / "work", base, head, provider_transport=FakeTransport(simulated_response=response))

    manifest = result.manifest
    final_policy = manifest.policy_decisions[-1]
    # Policy stays the pipeline's own deterministic evaluation (human approval
    # for the high-severity finding), not the provider's claim.
    assert final_policy["decision"] == "requires_human_approval"
    assert not any("allowed_by_model" in json.dumps(f) for f in manifest.merged_findings)


def test_invalid_provider_findings_never_become_user_facing(tmp_path: Path):
    response = _provider_response()
    response["findings"].append({
        "id": "CA-REV-IT-2",
        "file": "outside/escaped.py",
        "start_line": 1,
        "end_line": 1,
        "severity": "low",
        "category": "STYLE",
        "claim": "Escapes the snapshot",
        "impact": "None",
        "evidence": [],
        "evidence_strength": "none",
        "confidence": 0.5,
        "status": "detected",
        "fixability": "review_required",
        "provenance": {},
    })
    base, head = _repo_with_change(tmp_path / "work")
    result = _run_live(tmp_path / "work", base, head, provider_transport=FakeTransport(simulated_response=response))

    manifest = result.manifest
    ids = [f["id"] for f in manifest.merged_findings]
    assert "CA-REV-IT-1" in ids
    assert "CA-REV-IT-2" not in ids
    assert any("outside review packet" in e for e in manifest.provider_validation_errors)


def test_dry_run_flag_makes_no_request(tmp_path: Path):
    transport = FakeTransport(simulated_response=_provider_response())
    base, head = _repo_with_change(tmp_path / "work")
    result = _run_live(
        tmp_path / "work", base, head,
        provider_dry_run=True,
        provider_transport=transport,
    )
    manifest = result.manifest
    assert transport.captured_payloads == []
    assert manifest.provider_output_valid is None
    assert manifest.merged_findings == []
    # Dry run must not produce a user-facing error.
    assert not any("provider" in e.lower() and "skipped" not in e.lower() for e in manifest.errors)


# ---------------------------------------------------------------------------

cli_runner = CliRunner()


def _cli_repo(tmp_path: Path) -> Path:
    work = tmp_path / "cliwork"
    _repo_with_change(work)
    return work


def test_cli_live_dry_run_makes_no_request(tmp_path: Path):
    work = _cli_repo(tmp_path)
    manifest_path = tmp_path / "m.json"
    result = cli_runner.invoke(app, [
        "review",
        "--repo", str(work),
        "--base", "HEAD~1",
        "--head", "HEAD",
        "--index-repository",
        "--assemble-review-packet",
        "--review-provider", "live",
        "--provider", "test-model",
        "--dry-run",
        "--manifest-output", str(manifest_path),
    ])
    assert result.exit_code == 0, result.output
    assert "CodeAtlas live review" in result.output
    assert "Patches: disabled" in result.output
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["live_provider_enabled"] is True
    assert manifest["model_name"] == "test-model"
    assert manifest["provider_usage"].get("dry_run") is True


def test_cli_live_missing_credentials_fails_clearly(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CODEATLAS_API_KEY", raising=False)
    work = _cli_repo(tmp_path)
    result = cli_runner.invoke(app, [
        "review",
        "--repo", str(work),
        "--base", "HEAD~1",
        "--head", "HEAD",
        "--index-repository",
        "--assemble-review-packet",
        "--review-provider", "live",
        "--provider-timeout", "5",
    ])
    # Live without --dry-run and without credentials must fail with a clear error.
    assert result.exit_code == 1
    assert "credentials" in result.output.lower()
