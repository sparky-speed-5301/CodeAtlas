"""Focused safety, retention, and restart contracts for beta incidents."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from pydantic import ValidationError

from codeatlas.beta_incidents import (
    MAX_INCIDENT_BYTES,
    MAX_INCIDENTS,
    BetaIncident,
    BetaIncidentReport,
    BetaIncidentStore,
    IncidentReasonCode,
    create_beta_incident,
)
from codeatlas.service.persistence import repository_id

NOW = datetime(2026, 10, 16, tzinfo=UTC)
SESSION_ID = "beta-session-" + "a" * 32
RUN_ID = "rev-" + "b" * 12


def incident(repository, reason="service_unavailable", **kwargs):
    return create_beta_incident(repository=repository, reason_code=reason, timestamp=NOW, **kwargs)


@pytest.mark.parametrize("reason", get_args(IncidentReasonCode))
def test_all_failure_families_have_closed_safe_projections(tmp_path, reason) -> None:
    record = incident(tmp_path, reason, session_id=SESSION_ID, run_id=RUN_ID)
    restored = BetaIncident.model_validate_json(record.model_dump_json())

    assert restored == record
    assert record.reason_code == reason
    assert record.repository_identity_hash == repository_id(tmp_path)
    assert record.redaction_status == "safe_metadata_only"
    assert record.workspace_unchanged is None
    assert str(tmp_path) not in record.model_dump_json()
    assert record.safe_message.endswith(".")
    assert record.remediation.endswith(".")


@pytest.mark.parametrize("status", [
    "not_required", "pending_recovery", "recovered", "recovery_failed", "expired",
    "cancelled", "manual_review_required",
])
def test_recovery_status_is_metadata_not_recovery_authority(tmp_path, status) -> None:
    record = incident(tmp_path, "pending_recovery", recovery_status=status)
    assert record.recovery_status == status
    assert "historical" in record.remediation
    assert "approval_token" not in BetaIncident.model_fields


@pytest.mark.parametrize("field", [
    "source", "prompt", "transcript", "credential", "exception", "repository_path",
    "worktree_path", "approval_token", "patch", "command", "stdout", "stderr",
])
def test_unsafe_extra_fields_are_rejected(tmp_path, field) -> None:
    values = incident(tmp_path).model_dump(mode="python")
    values[field] = "private-payload"
    with pytest.raises(ValidationError) as error:
        BetaIncident.model_validate(values)
    assert "private-payload" not in str(error.value)


@pytest.mark.parametrize(("field", "value"), [
    ("reason_code", "private-reason"), ("safe_message", "private-message"),
    ("remediation", "run rm -rf /"), ("component", "private-component"),
    ("severity", "private-severity"), ("recovery_status", "private-status"),
    ("redaction_status", "unredacted"), ("session_id", "/private/session"),
    ("run_id", "https://token@example.com/private"),
    ("repository_identity_hash", "C:/private/repository"),
    ("workspace_unchanged", "true"), ("timestamp", NOW.timestamp()),
    ("incident_id", "x" * 1000),
])
def test_unsafe_or_unbounded_allowed_field_values_are_rejected(tmp_path, field, value) -> None:
    values = incident(tmp_path).model_dump(mode="python")
    values[field] = value
    with pytest.raises(ValidationError):
        BetaIncident.model_validate(values)


def test_valid_but_mismatched_fixed_message_is_rejected(tmp_path) -> None:
    values = incident(tmp_path).model_dump(mode="python")
    values["safe_message"] = incident(tmp_path, "provider_failed").safe_message
    with pytest.raises(ValidationError, match="incident_projection_invalid"):
        BetaIncident.model_validate(values)


@pytest.mark.parametrize("timestamp", [
    NOW.replace(tzinfo=None), datetime(1999, 1, 1, tzinfo=UTC),
    datetime(2100, 1, 1, tzinfo=UTC),
])
def test_timestamp_bounds(tmp_path, timestamp) -> None:
    with pytest.raises(ValueError, match="incident_timestamp_invalid"):
        create_beta_incident(repository=tmp_path, reason_code="service_unavailable", timestamp=timestamp)


def test_identifiers_follow_both_existing_run_shapes(tmp_path) -> None:
    for run_id in (RUN_ID, "run-" + "c" * 32):
        assert incident(tmp_path, run_id=run_id).run_id == run_id
    with pytest.raises(ValueError, match="incident_reason_invalid"):
        incident(tmp_path, "exception-with-private-content")


def test_store_restart_duplicate_and_conflict(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    store = BetaIncidentStore(path, repository=tmp_path)
    record = incident(tmp_path, workspace_unchanged=True)
    first = store.append(record, now=NOW)
    before = path.read_bytes()
    restarted = BetaIncidentStore(path, repository=tmp_path)

    assert restarted.load(now=NOW) == first
    assert restarted.append(record, now=NOW) == first
    assert path.read_bytes() == before
    conflict = incident(tmp_path, "provider_failed", incident_id=record.incident_id)
    with pytest.raises(ValueError, match="incident_duplicate_conflict"):
        restarted.append(conflict, now=NOW)
    assert path.read_bytes() == before


def test_count_retention_is_deterministic_bounded_and_restart_safe(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    store = BetaIncidentStore(path, repository=tmp_path, max_incidents=3)
    for number in range(8):
        record = create_beta_incident(
            repository=tmp_path, reason_code="patch_conflict",
            timestamp=NOW + timedelta(seconds=number),
            incident_id=f"beta-incident-{number:032x}",
        )
        report = store.append(record, now=NOW + timedelta(seconds=8))
    assert [item.incident_id for item in report.incidents] == [f"beta-incident-{number:032x}" for number in (5, 6, 7)]
    assert len(path.read_bytes()) <= MAX_INCIDENT_BYTES
    assert BetaIncidentStore(path, repository=tmp_path, max_incidents=3).load(now=NOW + timedelta(seconds=8)) == report


def test_age_retention_read_and_export_do_not_mutate_storage(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    store = BetaIncidentStore(path, repository=tmp_path, max_age_seconds=60)
    old = incident(tmp_path)
    store.append(old, now=NOW)
    before = path.read_bytes()
    assert len(store.load(now=NOW + timedelta(seconds=60)).incidents) == 1
    assert store.load(now=NOW + timedelta(seconds=61)).incidents == ()
    exported = store.export(tmp_path / "export.json", now=NOW + timedelta(seconds=61))
    assert exported.incidents == ()
    assert path.read_bytes() == before
    new = create_beta_incident(repository=tmp_path, reason_code="service_timeout", timestamp=NOW + timedelta(seconds=61))
    assert store.append(new, now=NOW + timedelta(seconds=61)).incidents == (new,)


@pytest.mark.parametrize(("count", "age"), [(0, 60), (MAX_INCIDENTS + 1, 60), (True, 60), (1, 59), (1, 90 * 86400 + 1), (1, True)])
def test_retention_configuration_is_bounded(tmp_path, count, age) -> None:
    with pytest.raises(ValueError, match="incident_retention_invalid"):
        BetaIncidentStore(tmp_path / "incidents.json", repository=tmp_path, max_incidents=count, max_age_seconds=age)


def test_repository_scope_rejects_load_append_and_mixed_report(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    store = BetaIncidentStore(path, repository=tmp_path / "a")
    record = incident(tmp_path / "a")
    store.append(record, now=NOW)
    before = path.read_bytes()
    other = incident(tmp_path / "b")
    with pytest.raises(ValueError, match="incident_scope_mismatch"):
        store.append(other, now=NOW)
    with pytest.raises(ValueError, match="incident_scope_mismatch"):
        BetaIncidentStore(path, repository=tmp_path / "b").load(now=NOW)
    with pytest.raises(ValidationError, match="incident_scope_mismatch"):
        BetaIncidentReport(repository_identity_hash=record.repository_identity_hash, incidents=(other,))
    assert path.read_bytes() == before


@pytest.mark.parametrize("malformation", ["oversize", "duplicate_key", "duplicate_id", "unsafe", "array", "invalid_json", "wrong_version"])
def test_unsafe_storage_is_rejected_without_rewriting(tmp_path, malformation) -> None:
    path = tmp_path / "incidents.json"
    record = incident(tmp_path)
    report = BetaIncidentReport(repository_identity_hash=record.repository_identity_hash, incidents=(record,))
    values = report.model_dump(mode="json")
    if malformation == "oversize":
        content = b" " * (MAX_INCIDENT_BYTES + 1)
    elif malformation == "duplicate_key":
        content = report.model_dump_json().replace('"report_version":', '"report_version":"beta-incidents-v1","report_version":').encode()
    elif malformation == "duplicate_id":
        values["incidents"].append(values["incidents"][0])
        content = json.dumps(values).encode()
    elif malformation == "unsafe":
        values["incidents"][0]["transcript"] = "private-transcript"
        content = json.dumps(values).encode()
    elif malformation == "array":
        content = b"[]"
    elif malformation == "invalid_json":
        content = b"private-not-json"
    else:
        values["report_version"] = "unknown"
        content = json.dumps(values).encode()
    path.write_bytes(content)
    store = BetaIncidentStore(path, repository=tmp_path)
    with pytest.raises(ValueError) as error:
        store.load(now=NOW)
    assert "private" not in str(error.value)
    with pytest.raises(ValueError):
        store.append(record, now=NOW)
    assert path.read_bytes() == content


def test_hard_model_count_bound_and_future_timestamp(tmp_path) -> None:
    records = tuple(incident(tmp_path) for _ in range(MAX_INCIDENTS + 1))
    with pytest.raises(ValidationError):
        BetaIncidentReport(repository_identity_hash=repository_id(tmp_path), incidents=records)
    path = tmp_path / "incidents.json"
    store = BetaIncidentStore(path, repository=tmp_path)
    with pytest.raises(ValueError, match="incident_timestamp_future"):
        store.append(records[0], now=NOW - timedelta(seconds=1))
    assert not path.exists()


def test_full_retention_export_is_bounded_redacted_and_no_clobber(tmp_path) -> None:
    path = tmp_path / "incidents.json"
    store = BetaIncidentStore(path, repository=tmp_path)
    for _ in range(MAX_INCIDENTS):
        store.append(incident(tmp_path, "database_migration_failed", session_id=SESSION_ID, run_id=RUN_ID), now=NOW)
    output = tmp_path / "export.json"
    report = store.export(output, now=NOW)
    assert len(report.incidents) == MAX_INCIDENTS
    assert len(output.read_bytes()) <= MAX_INCIDENT_BYTES
    assert BetaIncidentReport.model_validate_json(output.read_bytes()) == report
    text = output.read_text(encoding="utf-8")
    for unsafe in (str(tmp_path), "credential", "transcript", "prompt", "approval_token", "worktree_path"):
        assert unsafe not in text
    before = output.read_bytes()
    with pytest.raises(ValueError):
        store.export(output, now=NOW)
    assert output.read_bytes() == before
    assert not tuple(tmp_path.glob(".codeatlas-*.tmp"))


def test_atomic_failure_preserves_store_and_removes_owned_temporary(tmp_path, monkeypatch) -> None:
    path = tmp_path / "incidents.json"
    store = BetaIncidentStore(path, repository=tmp_path)
    store.append(incident(tmp_path), now=NOW)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("private-system-error")

    monkeypatch.setattr("codeatlas.beta_incidents.os.replace", fail)
    with pytest.raises(ValueError, match="incident_storage_unavailable") as error:
        store.append(incident(tmp_path, "provider_failed"), now=NOW)
    assert "private" not in str(error.value)
    assert path.read_bytes() == before
    assert not tuple(tmp_path.glob(".codeatlas-incidents-*.tmp"))


def test_explicit_create_missing_load_and_invalid_paths(tmp_path) -> None:
    store = BetaIncidentStore(tmp_path / "incidents.json", repository=tmp_path)
    with pytest.raises(ValueError, match="incident_storage_unavailable"):
        store.load(now=NOW)
    assert store.create().incidents == ()
    with pytest.raises(ValueError, match="incident_storage_exists"):
        store.create()
    for path in (tmp_path, tmp_path / "missing" / "incidents.json"):
        with pytest.raises(ValueError, match="incident_storage_path_invalid"):
            BetaIncidentStore(path, repository=tmp_path).create()


def test_linked_storage_and_export_paths_rejected(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="incident_storage_path_invalid"):
        BetaIncidentStore(linked / "incidents.json", repository=tmp_path).create()
    store = BetaIncidentStore(tmp_path / "incidents.json", repository=tmp_path)
    store.append(incident(tmp_path), now=NOW)
    with pytest.raises(ValueError):
        store.export(linked / "export.json", now=NOW)
    assert list(target.iterdir()) == []


def test_link_detection_is_enforced_without_symlink_privileges(tmp_path, monkeypatch) -> None:
    store = BetaIncidentStore(tmp_path / "incidents.json", repository=tmp_path)
    store.append(incident(tmp_path), now=NOW)
    before = store.path.read_bytes()
    monkeypatch.setattr("codeatlas.beta_incidents._is_linked_path", lambda path: True)
    with pytest.raises(ValueError, match="incident_storage_path_invalid"):
        store.load(now=NOW)
    with pytest.raises(ValueError, match="incident_storage_path_invalid"):
        store.append(incident(tmp_path), now=NOW)
    assert store.path.read_bytes() == before


def test_atomic_create_failure_leaves_no_record_or_temporary(tmp_path, monkeypatch) -> None:
    store = BetaIncidentStore(tmp_path / "incidents.json", repository=tmp_path)

    def fail(*args, **kwargs):
        raise OSError("private-link-error")

    monkeypatch.setattr("codeatlas.beta_incidents.os.link", fail)
    with pytest.raises(ValueError, match="incident_storage_unavailable"):
        store.create()
    assert not store.path.exists()
    assert not tuple(tmp_path.glob(".codeatlas-incidents-*.tmp"))


def test_incident_schema_contains_only_allowed_metadata() -> None:
    assert set(BetaIncident.model_fields) == {
        "incident_id", "timestamp", "component", "reason_code", "severity", "session_id",
        "run_id", "repository_identity_hash", "recovery_status", "safe_message",
        "remediation", "redaction_status", "workspace_unchanged",
    }


def test_recording_incidents_never_runs_tools_sqlite_cleanup_or_workspace_mutations(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "repository"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_text("private-source", encoding="utf-8")
    orphan = workspace / "orphan-worktree"
    orphan.mkdir()
    marker = orphan / "keep"
    marker.write_text("owned-elsewhere", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("incident recording must remain metadata-only")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("shutil.rmtree", forbidden)
    monkeypatch.setattr("codeatlas.service.persistence.SQLitePersistence", forbidden)
    store = BetaIncidentStore(tmp_path / "incidents.json", repository=workspace)
    for reason in get_args(IncidentReasonCode):
        store.append(incident(workspace, reason), now=NOW)
    store.export(tmp_path / "export.json", now=NOW)
    assert source.read_text(encoding="utf-8") == "private-source"
    assert marker.read_text(encoding="utf-8") == "owned-elsewhere"
    assert sorted(item.name for item in workspace.iterdir()) == ["orphan-worktree", "source.py"]
