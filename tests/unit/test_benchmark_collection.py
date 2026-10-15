from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeatlas.benchmark.collection import (
    CollectionRequest,
    collect_benchmark,
    collect_offline,
    collect_public,
)

ROOT = Path("eval/cases/real-repositories/v1")
SUITE = ROOT / "suite.json"


def _write_public_suite(path: Path) -> Path:
    suite = {
        "benchmark_version": "test-v1",
        "cases": [{
            "case_id": "public", "repository_url": "https://github.com/example/project.git",
            "repository_identity": "example/project", "base_sha": "0" * 40, "head_sha": "1" * 40,
            "language": ["python"], "changed_files": ["x.py"], "benchmark_category": "real_bugs",
            "population": "positive", "expected_finding_categories": ["CORRECTNESS"],
            "expected_finding_anchors": [{"anchor_id": "a", "file": "x.py", "start_line": 1, "end_line": 1, "category": "CORRECTNESS", "severity": "high"}],
            "expected_severity": ["high"], "expected_abstention": "forbidden", "expected_fixability": "fixable",
            "expected_proposal_eligible": True, "expected_patch_scope": {"allowed_files": ["x.py"]},
            "expected_test_command_policy": {"policy": "not_run"}, "known_limitations": [], "benchmark_version": "test-v1",
        }],
    }
    path.write_text(json.dumps(suite), encoding="utf-8")
    return path


def _public_request(suite_path: Path) -> CollectionRequest:
    return CollectionRequest(
        mode="public", suite_path=str(suite_path), case_id="public",
        repository_url="https://github.com/example/project.git", base_sha="0" * 40, head_sha="1" * 40,
        license_spdx="MIT", provenance="public repository license metadata",
    )


def test_offline_collection_is_metadata_only_and_network_free() -> None:
    result = collect_offline(CollectionRequest(mode="offline", suite_path=str(SUITE), case_id="clean-change"))
    assert result.status == "available"
    assert result.repository_identity == "codeatlas/sanitized-offline-benchmark-fixture"
    assert result.license_spdx == "MIT-0"
    assert result.fixture_digest


def test_offline_collection_rejects_missing_fixture() -> None:
    result = collect_offline(CollectionRequest(mode="offline", suite_path=str(SUITE), case_id="missing"))
    assert result.status == "unavailable"
    assert result.reason == "case_not_found"


def test_public_collection_records_missing_pins_and_license() -> None:
    missing_pin = collect_public(CollectionRequest(mode="public", suite_path=str(SUITE), case_id="public"))
    assert missing_pin.status == "unavailable"
    assert missing_pin.reason == "missing_pin"
    missing_license = collect_public(CollectionRequest(
        mode="public", suite_path=str(SUITE), case_id="public",
        repository_url="https://github.com/example/project.git",
        base_sha="0" * 40, head_sha="1" * 40,
    ))
    assert missing_license.status == "unavailable"
    assert missing_license.reason == "license_missing"


def test_public_collection_does_not_persist_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    suite_path = _write_public_suite(tmp_path / "suite.json")
    calls: list[list[str]] = []

    def fake_git(directory: Path, args: list[str], timeout: float) -> bool:
        calls.append(args)
        return True

    workspace = tmp_path / "workspace"

    def fake_mkdtemp(*, prefix: str) -> str:
        assert prefix == "codeatlas-benchmark-"
        workspace.mkdir()
        return str(workspace)

    monkeypatch.setattr("codeatlas.benchmark.collection._git", fake_git)
    monkeypatch.setattr("codeatlas.benchmark.collection.tempfile.mkdtemp", fake_mkdtemp)
    result = collect_public(_public_request(suite_path))
    assert result.status == "available"
    assert result.reason is None
    assert any("fetch" == call[0] for call in calls)
    assert not workspace.exists()


def test_public_fetch_failure_is_stable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    suite_path = _write_public_suite(tmp_path / "suite.json")
    monkeypatch.setattr("codeatlas.benchmark.collection._git", lambda *args: False)
    result = collect_benchmark(_public_request(suite_path))
    assert result.status == "unavailable"
    assert result.reason == "fetch_failed"


def test_public_cleanup_failure_is_stable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    suite_path = _write_public_suite(tmp_path / "suite.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("codeatlas.benchmark.collection.tempfile.mkdtemp", lambda **kwargs: str(workspace))
    monkeypatch.setattr("codeatlas.benchmark.collection._git", lambda *args: True)

    def fail_cleanup(path: Path) -> None:
        raise OSError

    monkeypatch.setattr("codeatlas.benchmark.collection.shutil.rmtree", fail_cleanup)
    result = collect_benchmark(_public_request(suite_path))
    assert result.status == "unavailable"
    assert result.reason == "cleanup_failed"
