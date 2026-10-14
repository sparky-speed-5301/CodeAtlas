"""Work Package 5 persistence, scoping, and recovery contracts."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

import codeatlas.service.persistence as persistence_module
from codeatlas.patching.proposal import generate_validation_approval_token
from codeatlas.service.models import ReviewCreateRequest
from codeatlas.service.persistence import (
    PersistenceConfig,
    PersistenceError,
    SQLitePersistence,
    repository_id,
)
from codeatlas.service.persistence_codec import make_snapshot
from codeatlas.service.state import (
    ReviewRunRecord,
    ReviewStateManager,
    ServiceStateError,
)


def _record(repo: Path, run_id: str, status: str = "completed") -> ReviewRunRecord:
    record = ReviewRunRecord(run_id, ReviewCreateRequest(repo=str(repo), base="base", head="head"), repo)
    record.status = status  # type: ignore[assignment]
    record.progress_text = "test"
    record.completed_at = "2026-01-01T00:00:00+00:00" if status == "completed" else ""
    record.findings = [{
        "id": "CA-PERSIST",
        "severity": "high",
        "category": "STYLE",
        "claim": "bounded persisted finding",
        "file": "src/app.py",
        "line": 1,
        "start_line": 1,
        "end_line": 1,
        "confidence": 0.9,
        "evidence_strength": "strong",
        "status": "review_only",
        "quality_decision": "review_only",
    }]
    return record


def test_sqlite_migration_is_versioned_and_reopens(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    first = SQLitePersistence(PersistenceConfig(path=path))
    assert first.schema_version == 2
    first.close()

    second = SQLitePersistence(PersistenceConfig(path=path))
    row = second._db.execute("SELECT version, checksum FROM schema_migrations ORDER BY version DESC").fetchone()
    assert row[0] == 2
    assert row[1]
    assert second._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    second.close()


def test_malformed_and_unusable_database_paths_fail_closed(tmp_path: Path):
    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"not a sqlite database")
    with pytest.raises(PersistenceError, match="migration_validation_failed"):
        SQLitePersistence(PersistenceConfig(path=malformed))

    unusable = tmp_path / "database-directory"
    unusable.mkdir()
    with pytest.raises(PersistenceError, match="migration_validation_failed"):
        SQLitePersistence(PersistenceConfig(path=unusable))


def test_partial_migration_rolls_back_without_data_loss(tmp_path: Path, monkeypatch):
    path = tmp_path / "partial.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    record = _record(tmp_path, "migration-survivor")
    manager._reviews[record.run_id] = record
    manager._checkpoint(record, "review_completed", required=True)
    manager.close()

    with sqlite3.connect(path) as db:
        db.execute("DROP VIEW patch_proposals")
        db.execute("DELETE FROM schema_migrations WHERE version=2")
        db.execute("PRAGMA user_version=1")

    version, statements = persistence_module.MIGRATIONS[1]
    monkeypatch.setattr(
        persistence_module,
        "MIGRATIONS",
        (persistence_module.MIGRATIONS[0], (version, (*statements, "CREATE TABLE partial_marker(id INTEGER)", "INVALID SQL"))),
    )
    with pytest.raises(PersistenceError, match="migration_validation_failed"):
        SQLitePersistence(PersistenceConfig(path=path))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM review_runs WHERE run_id='migration-survivor'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='partial_marker'").fetchone()[0] == 0

    monkeypatch.undo()
    restored = ReviewStateManager(persistence_path=path)
    assert restored.get_findings("migration-survivor")[0].id == "CA-PERSIST"
    restored.close()


def test_persistent_manager_restores_completed_findings_with_repository_scope(tmp_path: Path):
    db = tmp_path / "state.sqlite3"
    repo = tmp_path / "repo"
    other_repo = tmp_path / "other"
    repo.mkdir()
    other_repo.mkdir()

    manager = ReviewStateManager(persistence_path=db)
    record = _record(repo, "rev-persisted")
    manager._reviews[record.run_id] = record
    manager._checkpoint(record, "review_completed")
    manager.close()

    restored = ReviewStateManager(persistence_path=db)
    status = restored.get_review_status("rev-persisted")
    assert status.status == "completed"
    assert status.recovery_state == "recovered"
    assert restored.get_findings("rev-persisted")[0].id == "CA-PERSIST"
    assert restored.get_review("rev-other") is None
    assert restored.persistence is not None
    assert restored.persistence.load(repository_id(other_repo), "rev-persisted") is None
    assert repository_id(repo) != repository_id(other_repo)
    restored.close()


def test_interrupted_review_never_recovers_as_success(tmp_path: Path):
    db = tmp_path / "state.sqlite3"
    repo = tmp_path / "repo"
    repo.mkdir()

    manager = ReviewStateManager(persistence_path=db)
    record = _record(repo, "rev-interrupted", status="reviewing")
    manager._reviews[record.run_id] = record
    manager._checkpoint(record, "review_started")
    manager.close()

    restored = ReviewStateManager(persistence_path=db)
    status = restored.get_review_status("rev-interrupted")
    assert status.status == "recovery_failed"
    assert status.recovery_state == "recovery_failed"
    assert status.completed_at == ""
    assert status.errors
    restored.close()


def test_persistence_rejects_unbounded_or_cross_scope_snapshots(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    store = SQLitePersistence(PersistenceConfig(path=path, max_payload_bytes=16_384))
    with pytest.raises(PersistenceError):
        store.save({
            "repository_id": "repo-wrong",
            "repository_path": str(tmp_path),
            "run_id": "rev-bad",
            "review": {},
            "entities": [],
            "artifacts": {},
        }, event="invalid")
    store.close()


def test_persistence_never_stores_approval_tokens_or_source_artifacts(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    record = _record(tmp_path, "redaction")
    record.findings[0]["claim"] = "CAT-APP-" + "a" * 32
    record.findings[0]["raw_provider_transcript"] = "PRIVATE_TRANSCRIPT_SENTINEL"
    record.findings[0]["provenance"] = {"api_key": "PRIVATE_CREDENTIAL_SENTINEL"}
    record.errors = ["PRIVATE_DIAGNOSTIC_SENTINEL"]
    manager._reviews[record.run_id] = record
    manager._checkpoint(record, "review_completed", required=True)
    manager.close()
    with sqlite3.connect(path) as db:
        text = " ".join(row[0] for row in db.execute("SELECT payload FROM review_runs UNION ALL SELECT payload FROM records"))
    assert "CAT-APP-" not in text
    assert "PRIVATE_" not in text
    assert "original_contents" not in text


def test_retention_expires_old_completed_runs_without_evicting_active_work(tmp_path: Path):
    db = tmp_path / "state.sqlite3"
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = ReviewStateManager(
        persistence_config=PersistenceConfig(path=db, max_age_seconds=60, max_runs=2),
    )
    old = _record(repo, "rev-old")
    old.created_at = "2020-01-01T00:00:00+00:00"
    old.completed_at = old.created_at
    manager._reviews[old.run_id] = old
    manager._checkpoint(old, "review_completed")
    active = _record(repo, "rev-active", status="reviewing")
    manager._reviews[active.run_id] = active
    manager._checkpoint(active, "review_started")
    manager.close()

    restored = ReviewStateManager(persistence_path=db)
    assert restored.get_review_status("rev-old").status == "expired"
    assert restored.get_review_status("rev-active").status == "recovery_failed"
    restored.close()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo(repo: Path) -> Path:
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    source = repo / "src/app.py"
    source.write_text("def render(user):\n    return user\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    source.write_text("def render(user):\n    print(user.password)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "head")
    return repo


def _wait(manager: ReviewStateManager, run_id: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if manager.get_review_status(run_id).status in {"completed", "failed", "cancelled"}:
            return
        time.sleep(.02)
    raise AssertionError("Review did not complete")


def _token(fix, operation: str = "validate") -> str:
    return generate_validation_approval_token(
        proposal_id=fix.proposal_id, finding_id=fix.finding_id, run_id=fix.run_id,
        repository_identity=fix.repository, base_commit=fix.base_commit, head_commit=fix.head_commit,
        patch_hash=fix.patch_hash, target_files=fix.target_files, operation=operation,
    )


@pytest.mark.parametrize("stage", ["draft", "approved", "validated", "applied", "reverted"])
def test_real_fix_lifecycle_survives_restart_without_replaying_authority(tmp_path: Path, stage: str):
    repo = _repo(tmp_path / "repo")
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    run = manager.start_review(ReviewCreateRequest(repo=str(repo), base="HEAD~1", head="HEAD")).run_id
    _wait(manager, run)
    finding = manager.get_findings(run)[0].id
    fix = manager.request_fix_proposal(run, finding)
    assert fix.generation_status == "draft_ready"
    if stage != "draft":
        manager.approve_fix_proposal_for_validation(run, finding, fix.proposal_id, approval_token=_token(fix))
    if stage in {"validated", "applied", "reverted"}:
        result = manager.validate_fix_proposal(run, finding, fix.proposal_id, approval_token=_token(fix))
        assert result.valid
    if stage in {"applied", "reverted"}:
        manager.apply_fix_proposal(run, finding, fix.proposal_id, approval_token=_token(fix, "apply"), confirmed_patch_hash=fix.patch_hash)
    if stage == "reverted":
        manager.revert_fix_apply(run, finding, fix.proposal_id, confirmed_patch_hash=fix.patch_hash)
    assert not manager._persistence_errors
    before = (repo / "src/app.py").read_bytes()
    manager.close()
    restored = ReviewStateManager(persistence_path=path)
    recovered = restored.get_fix_proposal(run, finding, fix.proposal_id)
    assert recovered.patch_text == fix.patch_text
    assert recovered.patch_hash == fix.patch_hash
    assert recovered.recovery_state == "recovered"
    if stage == "approved":
        assert recovered.validation_status == "approval_required"
    if stage in {"validated", "applied", "reverted"}:
        assert recovered.validation_result["valid"] is True
        assert recovered.validation_result["patch_hash"] == fix.patch_hash
    history = restored.get_fix_apply_history(run, finding, fix.proposal_id)
    assert history.apply_status == (stage if stage in {"applied", "reverted"} else "not_applied")
    assert history.revert_available is False
    if stage in {"applied", "reverted"}:
        assert history.events[0].event == "applied"
    with pytest.raises(ServiceStateError):
        restored.apply_fix_proposal(run, finding, fix.proposal_id, approval_token=_token(fix, "apply"), confirmed_patch_hash=fix.patch_hash)
    with pytest.raises(ServiceStateError):
        restored.revert_fix_apply(run, finding, fix.proposal_id, confirmed_patch_hash=fix.patch_hash)
    assert (repo / "src/app.py").read_bytes() == before
    assert not restored._persistence_errors
    restored.close()


def test_duplicate_proposal_ids_remain_retrievable_in_each_run(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    pairs = []
    for _ in range(2):
        run = manager.start_review(ReviewCreateRequest(repo=str(repo), base="HEAD~1", head="HEAD")).run_id
        _wait(manager, run)
        fid = manager.get_findings(run)[0].id
        fix = manager.request_fix_proposal(run, fid)
        pairs.append((run, fid, fix.proposal_id))
    assert pairs[0][2] == pairs[1][2]
    assert manager.get_fix_proposal(*pairs[0]).run_id == pairs[0][0]
    manager.close()
    restored = ReviewStateManager(persistence_path=path)
    for scope in pairs:
        assert restored.get_fix_proposal(*scope).run_id == scope[0]
    restored.close()


def test_duplicate_proposal_ids_keep_validation_evidence_scoped(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    manager = ReviewStateManager()
    pairs = []
    for _ in range(2):
        run = manager.start_review(ReviewCreateRequest(repo=str(repo), base="HEAD~1", head="HEAD")).run_id
        _wait(manager, run)
        finding_id = manager.get_findings(run)[0].id
        fix = manager.request_fix_proposal(run, finding_id)
        manager.approve_fix_proposal_for_validation(run, finding_id, fix.proposal_id, approval_token=_token(fix))
        validation = manager.validate_fix_proposal(
            run, finding_id, fix.proposal_id, approval_token=_token(fix),
        )
        assert validation.valid
        pairs.append((run, finding_id, fix))

    assert pairs[0][2].proposal_id == pairs[1][2].proposal_id
    for run, finding_id, fix in pairs:
        with manager._lock:
            evidence = manager._scoped_validations[(run, fix.proposal_id)]
        assert evidence.proposal_id == fix.proposal_id
        assert manager.get_fix_apply_history(run, finding_id, fix.proposal_id).patch_hash == fix.patch_hash
    manager.close()


def test_interrupted_validation_is_failed_even_when_review_completed(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    run = manager.start_review(ReviewCreateRequest(repo=str(repo), base="HEAD~1", head="HEAD")).run_id
    _wait(manager, run)
    fid = manager.get_findings(run)[0].id
    fix = manager.request_fix_proposal(run, fid)
    manager._fix_proposals[fix.proposal_id].response.validation_status = "validating"
    manager._checkpoint(manager.get_review(run), "validation_started", required=True)
    manager.close()
    restored = ReviewStateManager(persistence_path=path)
    recovered = restored.get_fix_proposal(run, fid, fix.proposal_id)
    assert recovered.validation_status == "validation_failed"
    assert recovered.recovery_state == "recovery_failed"
    assert recovered.validation_result["valid"] is False
    assert not restored._persistence_errors
    restored.close()


def test_migration_tampering_and_database_ownership_fail_closed(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    store = SQLitePersistence(PersistenceConfig(path=path))
    with pytest.raises(PersistenceError, match="database_in_use"):
        SQLitePersistence(PersistenceConfig(path=path))
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE schema_migrations SET checksum='tampered'")
    with pytest.raises(PersistenceError, match="migration_validation_failed"):
        SQLitePersistence(PersistenceConfig(path=path))


def test_deterministic_serialization_and_payload_bounds(tmp_path: Path):
    from codeatlas.service.persistence import canonical_json
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    manager = ReviewStateManager(persistence_path=tmp_path / "state.sqlite3")
    record = _record(tmp_path, "bounded")
    manager._reviews[record.run_id] = record
    snapshot = make_snapshot(manager, record)
    snapshot["review"]["status"]["progress_text"] = "x" * 600_000
    with pytest.raises(PersistenceError, match="persistence_payload_too_large"):
        manager.persistence.save(snapshot, event="large")
    assert manager.persistence.load_reviews() == []
    manager.close()


def test_queue_capacity_cancellation_and_explicit_retry(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    manager = ReviewStateManager(max_concurrent_reviews=1, max_queued_reviews=1)
    started, release = threading.Event(), threading.Event()
    active, peak = 0, 0
    lock = threading.Lock()
    def execute(record):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        started.set()
        assert release.wait(10)
        record.status = "cancelled" if record.cancel_requested.is_set() else "completed"
        with lock:
            active -= 1
    monkeypatch.setattr(manager, "_execute_review_run", execute)
    request = ReviewCreateRequest(repo=str(repo), base="HEAD~1")
    first = manager.start_review(request)
    assert started.wait(5)
    second = manager.start_review(request)
    try:
        with pytest.raises(ServiceStateError, match="queue is full"):
            manager.start_review(request)
        manager.cancel_review(second.run_id)
    finally:
        release.set()
    _wait(manager, first.run_id)
    _wait(manager, second.run_id)
    retry = manager.retry_review(second.run_id)
    assert retry.run_id != second.run_id
    assert retry.retry_of == second.run_id
    assert peak == 1
    manager.close()


def test_populated_v1_database_migrates_without_losing_review(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    record = _record(tmp_path, "migration-run")
    manager._reviews[record.run_id] = record
    manager._checkpoint(record, "review_completed", required=True)
    manager.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP VIEW patch_proposals")
        db.execute("DELETE FROM schema_migrations WHERE version=2")
        db.execute("PRAGMA user_version=1")
    restored = ReviewStateManager(persistence_path=path)
    assert restored.persistence.schema_version == 2
    assert restored.get_findings("migration-run")[0].id == "CA-PERSIST"
    restored.close()


def test_corrupted_payload_is_quarantined_and_reported(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    record = _record(tmp_path, "corrupt")
    manager._reviews[record.run_id] = record
    manager._checkpoint(record, "review_completed", required=True)
    manager.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE records SET payload_hash='corrupt' WHERE kind='findings'")
    restored = ReviewStateManager(persistence_path=path)
    assert restored.get_review_status("corrupt").status == "recovery_failed"
    assert restored.get_findings("corrupt") == []
    assert "recovery_failed" in restored._persistence_errors
    restored.close()


def test_same_repository_names_are_independently_scoped(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    for run_id in ("left", "right"):
        repo = tmp_path / run_id / "repo"
        repo.mkdir(parents=True)
        record = _record(repo, run_id)
        record.findings[0]["claim"] = run_id
        manager._reviews[run_id] = record
        manager._checkpoint(record, "review_completed", required=True)
    manager.close()
    restored = ReviewStateManager(persistence_path=path)
    assert restored.get_findings("left")[0].claim == "left"
    assert restored.get_findings("right")[0].claim == "right"
    assert restored.get_review_events("left").repository_id != restored.get_review_events("right").repository_id
    with pytest.raises(KeyError):
        restored.explain_finding("CA-PERSIST", run_id="missing")
    with pytest.raises(KeyError):
        restored.explain_finding("CA-PERSIST")
    restored.close()


def test_feedback_events_are_shared_by_api_cli_after_restart(tmp_path: Path):
    from typer.testing import CliRunner

    from codeatlas.cli import app
    from codeatlas.client import CodeAtlasClient
    from codeatlas.service.server import create_server
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    record = _record(tmp_path, "shared")
    manager._reviews[record.run_id] = record
    manager._checkpoint(record, "review_completed", required=True)
    manager.set_finding_feedback("shared", "CA-PERSIST", "useful")
    manager.toggle_dismiss_finding("shared", "CA-PERSIST")
    manager.close()
    restored = ReviewStateManager(persistence_path=path)
    server = create_server(port=0, state_manager=restored)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        client = CodeAtlasClient(url)
        assert client.capabilities().state_lifetime == "persistent"
        status = client.wait_for_review("shared", timeout=1)
        assert status.recovery_state == "recovered"
        finding = client.get_findings("shared", include_dismissed=True).findings[0]
        assert finding.feedback == "useful" and finding.dismissed
        events = client.get_review_events("shared").events
        assert "finding_feedback_updated" in {event.event for event in events}
        result = CliRunner().invoke(app, ["--service-url", url, "--json", "reviews", "events", "shared"])
        assert result.exit_code == 0, result.output
        assert "finding_feedback_updated" in result.output
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_required_checkpoint_failure_blocks_apply_without_mutation(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    manager = ReviewStateManager(persistence_path=tmp_path / "state.sqlite3")
    run = manager.start_review(ReviewCreateRequest(repo=str(repo), base="HEAD~1")).run_id
    _wait(manager, run)
    fid = manager.get_findings(run)[0].id
    fix = manager.request_fix_proposal(run, fid)
    manager.approve_fix_proposal_for_validation(run, fid, fix.proposal_id, approval_token=_token(fix))
    assert manager.validate_fix_proposal(run, fid, fix.proposal_id, approval_token=_token(fix)).valid
    before = (repo / "src/app.py").read_bytes()
    def fail(*args, **kwargs):
        raise PersistenceError("persistence_failed")
    monkeypatch.setattr(manager.persistence, "save", fail)
    with pytest.raises(ServiceStateError) as error:
        manager.apply_fix_proposal(run, fid, fix.proposal_id, approval_token=_token(fix, "apply"), confirmed_patch_hash=fix.patch_hash)
    assert error.value.reason == "persistence_failed"
    assert (repo / "src/app.py").read_bytes() == before
    assert _git(repo, "status", "--porcelain") == ""
    manager.close()


def test_post_apply_checkpoint_failure_restores_workspace(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    manager = ReviewStateManager(persistence_path=tmp_path / "state.sqlite3")
    run = manager.start_review(ReviewCreateRequest(repo=str(repo), base="HEAD~1", head="HEAD")).run_id
    _wait(manager, run)
    finding_id = manager.get_findings(run)[0].id
    fix = manager.request_fix_proposal(run, finding_id)
    manager.approve_fix_proposal_for_validation(run, finding_id, fix.proposal_id, approval_token=_token(fix))
    assert manager.validate_fix_proposal(run, finding_id, fix.proposal_id, approval_token=_token(fix)).valid
    before = (repo / "src/app.py").read_bytes()
    original_save = manager.persistence.save

    def fail_after_mutation(snapshot, *, event):
        if event == "fix_applied":
            raise PersistenceError("persistence_failed")
        return original_save(snapshot, event=event)

    monkeypatch.setattr(manager.persistence, "save", fail_after_mutation)
    with pytest.raises(ServiceStateError, match="durably recorded") as error:
        manager.apply_fix_proposal(
            run, finding_id, fix.proposal_id,
            approval_token=_token(fix, "apply"), confirmed_patch_hash=fix.patch_hash,
        )
    assert error.value.reason == "persistence_failed"
    assert (repo / "src/app.py").read_bytes() == before
    assert _git(repo, "status", "--porcelain") == ""
    manager.close()


def test_interrupted_apply_recovery_does_not_become_validation_failure(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    path = tmp_path / "state.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    run = manager.start_review(ReviewCreateRequest(repo=str(repo), base="HEAD~1", head="HEAD")).run_id
    _wait(manager, run)
    finding_id = manager.get_findings(run)[0].id
    fix = manager.request_fix_proposal(run, finding_id)
    record = manager.get_review(run)
    assert record is not None
    record.pending_operation = {"name": "apply", "finding_id": finding_id, "proposal_id": fix.proposal_id}
    manager._checkpoint(record, "apply_started", required=True)
    manager.close()

    restored = ReviewStateManager(persistence_path=path)
    recovered = restored.get_fix_proposal(run, finding_id, fix.proposal_id)
    assert recovered.recovery_state == "recovery_failed"
    assert recovered.validation_status == "approval_required"
    assert recovered.validation_result is None
    restored.close()
