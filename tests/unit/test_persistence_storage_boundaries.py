"""Focused regression coverage for SQLite persistence storage boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codeatlas.service.models import ReviewCreateRequest
from codeatlas.service.persistence import (
    SCHEMA_VERSION,
    PersistenceConfig,
    PersistenceError,
    SQLitePersistence,
    canonical_json,
)
from codeatlas.service.persistence_codec import make_snapshot
from codeatlas.service.state import ReviewRunRecord, ReviewStateManager


def _snapshot(tmp_path: Path, run_id: str, created_at: str | None = None) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    created_at = created_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    manager = ReviewStateManager()
    record = ReviewRunRecord(run_id, ReviewCreateRequest(repo=str(repo), base="base", head="head"), repo)
    record.status = "completed"  # type: ignore[assignment]
    record.progress_text = "synthetic persisted review"
    record.created_at = created_at
    record.completed_at = created_at
    manager._reviews[run_id] = record
    snapshot = make_snapshot(manager, record)
    manager.close()
    return snapshot


def _rewrite_run_payload(path: Path, update) -> None:
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT payload FROM review_runs").fetchone()
        stored = json.loads(row[0])
        update(stored["review"]["status"])
        text = canonical_json(stored)
        db.execute(
            "UPDATE review_runs SET payload=?,payload_hash=?",
            (text, hashlib.sha256(text.encode()).hexdigest()),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "failed"),
        ("base_commit", "tampered-base"),
        ("head_commit", "tampered-head"),
        ("created_at", "2026-01-02T00:00:00+00:00"),
    ],
)
def test_load_binds_embedded_run_identity_to_sql_row(tmp_path: Path, field: str, value: str):
    path = tmp_path / "state.sqlite3"
    store = SQLitePersistence(PersistenceConfig(path=path))
    snapshot = _snapshot(tmp_path, "bound-run")
    store.save(snapshot, event="saved")
    _rewrite_run_payload(path, lambda status: status.__setitem__(field, value))

    with pytest.raises(PersistenceError, match="persisted_identity_invalid"):
        store.load(snapshot["repository_id"], snapshot["run_id"])
    store.close()


def test_load_binds_sql_updated_timestamp_to_latest_lifecycle_event(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    store = SQLitePersistence(PersistenceConfig(path=path))
    snapshot = _snapshot(tmp_path, "timestamp-run")
    store.save(snapshot, event="saved")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE review_runs SET updated_at=?", ("2026-01-02T00:00:00+00:00",))

    with pytest.raises(PersistenceError, match="persisted_identity_invalid"):
        store.load(snapshot["repository_id"], snapshot["run_id"])
    store.close()


def test_recovery_index_only_changes_recovery_state(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    store = SQLitePersistence(PersistenceConfig(path=path))
    snapshot = _snapshot(tmp_path, "recovery-run")
    store.save(snapshot, event="saved")
    store.recovery_index()

    row = store._db.execute(
        "SELECT recovery_state FROM review_runs WHERE repository_id=? AND run_id=?",
        (snapshot["repository_id"], snapshot["run_id"]),
    ).fetchone()
    assert row[0] == "pending_recovery"
    loaded = store.load(snapshot["repository_id"], snapshot["run_id"])
    assert loaded is not None
    assert loaded["review"]["status"]["recovery_state"] == "not_required"
    store.close()


@pytest.mark.parametrize("event", ["x" * 129, "CAT-APP-secret-event"])
def test_save_bounds_and_scrubs_checkpoint_event(tmp_path: Path, event: str):
    path = tmp_path / "state.sqlite3"
    store = SQLitePersistence(PersistenceConfig(path=path))
    snapshot = _snapshot(tmp_path, "event-run")

    with pytest.raises(PersistenceError, match="invalid_persistence_payload"):
        store.save(snapshot, event=event)
    assert store.load_reviews() == []
    store.close()


def test_migration_rejects_newer_schema_with_safe_reason(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    with pytest.raises(PersistenceError, match="unsupported_database_version"):
        SQLitePersistence(PersistenceConfig(path=path))


def test_record_bound_counts_retained_history(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    snapshot = _snapshot(tmp_path, "history-run")
    for index in range(10):
        snapshot["entities"].append({
            "kind": "lifecycle_events",
            "id": f"historical-{index}",
            "finding_id": "",
            "proposal_id": "",
            "patch_hash": "",
            "payload": {
                "event": "historical",
                "status": "completed",
                "operation": None,
                "at": f"2026-01-01T00:00:{index:02d}+00:00",
            },
        })

    initial = SQLitePersistence(PersistenceConfig(path=path, max_records_per_run=20))
    initial.save(snapshot, event="initial")
    initial.close()

    stricter = SQLitePersistence(PersistenceConfig(path=path, max_records_per_run=10))
    empty_checkpoint = _snapshot(tmp_path, "history-run", snapshot["review"]["status"]["created_at"])
    with pytest.raises(PersistenceError, match="persistence_record_limit"):
        stricter.save(empty_checkpoint, event="stricter")
    stricter.close()


def test_reopening_with_stricter_max_runs_evicts_oldest_terminal_runs(tmp_path: Path):
    path = tmp_path / "state.sqlite3"
    initial = SQLitePersistence(PersistenceConfig(path=path, max_runs=3))
    start = datetime.now(UTC).replace(microsecond=0)
    for index, run_id in enumerate(("old-run", "middle-run", "new-run")):
        created_at = (start + timedelta(seconds=index)).isoformat()
        snapshot = _snapshot(tmp_path, run_id, created_at)
        initial.save(snapshot, event="saved")
    initial.close()

    reopened = SQLitePersistence(PersistenceConfig(path=path, max_runs=1))
    rows = reopened.load_reviews()
    assert [row["run_id"] for row in rows] == ["new-run"]
    reopened.close()
