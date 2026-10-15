"""Reliability boundaries distinguish accounting from runtime enforcement."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from codeatlas.orchestrator.artifacts import (
    ArtifactOutputError,
    write_validation_artifacts,
)
from codeatlas.reliability import (
    UNAVAILABLE_CAPABILITIES,
    AdmissionGate,
    BudgetResult,
    BudgetSession,
    BudgetStatus,
    ReliabilityBudgets,
    capability_result,
    truncate_utf8,
)
from codeatlas.verification.redaction import audit_and_redact_results


@pytest.mark.parametrize("status", list(BudgetStatus))
def test_result_statuses(status):
    assert BudgetResult(status.value).status is status


def test_unknown_status_rejected():
    with pytest.raises(ValueError):
        BudgetResult("passed")


@pytest.mark.parametrize("dimension", [
    "max_files", "max_context_bytes", "max_output_bytes", "max_artifact_bytes",
    "max_artifacts", "max_db_bytes", "max_db_records",
])
def test_exact_and_exceeded_atomic_accounting(dimension):
    session = BudgetSession(ReliabilityBudgets(**{dimension: 3}))
    assert session.reserve({dimension: 3}).status == BudgetStatus.COMPLETED
    result = session.reserve({dimension: 1})
    assert result.status == BudgetStatus.REJECTED_BY_BUDGET
    assert result.observed == 4 and result.limit == 3
    assert session.usage == {dimension: 3}


def test_multi_dimension_rejection_does_not_consume_other_limits():
    session = BudgetSession(ReliabilityBudgets(max_files=2, max_context_bytes=3))
    assert session.reserve({"max_files": 2, "max_context_bytes": 4}).status == BudgetStatus.REJECTED_BY_BUDGET
    assert session.usage == {}
    assert session.reserve({"max_files": 2, "max_context_bytes": 3}).status == BudgetStatus.COMPLETED


@pytest.mark.parametrize("kwargs", [
    {"wall_time_seconds": 0}, {"wall_time_seconds": float("nan")},
    {"wall_time_seconds": float("inf")}, {"max_files": -1},
    {"max_files": True}, {"max_concurrent": 0}, {"max_queued": -1},
    {"max_retries": 1.5},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        ReliabilityBudgets(**kwargs)


def test_failed_and_unavailable_reservations():
    session = BudgetSession(ReliabilityBudgets())
    assert session.reserve({"max_files": -1}).status == BudgetStatus.FAILED
    assert session.reserve({"max_files": True}).status == BudgetStatus.FAILED
    assert session.reserve({"memory": 1}).status == BudgetStatus.UNAVAILABLE
    assert session.reserve({"max_retries": 1}).status == BudgetStatus.UNAVAILABLE
    assert session.usage == {}


def test_deadline_exact_and_exceeded():
    now = [0.0]
    session = BudgetSession(ReliabilityBudgets(wall_time_seconds=2), clock=lambda: now[0])
    now[0] = 1.999
    assert session.checkpoint().status == BudgetStatus.COMPLETED
    now[0] = 2
    assert session.reserve({"max_files": 1}).status == BudgetStatus.TIMED_OUT
    now[0] = 3
    assert session.begin_attempt().status == BudgetStatus.TIMED_OUT
    assert session.usage == {}


def test_cancel_before_and_after_work():
    cancellation = threading.Event()
    session = BudgetSession(ReliabilityBudgets(), cancellation=cancellation)
    assert session.reserve({"max_files": 1}).status == BudgetStatus.COMPLETED
    cancellation.set()
    assert session.checkpoint().status == BudgetStatus.CANCELLED
    assert session.reserve({"max_files": 1}).status == BudgetStatus.CANCELLED
    assert session.begin_attempt().status == BudgetStatus.CANCELLED
    assert AdmissionGate(session).acquire().status == BudgetStatus.CANCELLED
    assert session.usage == {"max_files": 1}
    second = BudgetSession(ReliabilityBudgets())
    second.cancel()
    assert second.checkpoint().status == BudgetStatus.CANCELLED


@pytest.mark.parametrize("retries", [0, 1, 3])
def test_initial_attempt_plus_exact_retries(retries):
    session = BudgetSession(ReliabilityBudgets(max_retries=retries))
    for _ in range(retries + 1):
        assert session.begin_attempt().status == BudgetStatus.COMPLETED
    assert session.begin_attempt().status == BudgetStatus.REJECTED_BY_BUDGET
    assert session.usage["attempts"] == retries + 1


def test_retry_reservations_are_thread_safe():
    session = BudgetSession(ReliabilityBudgets(max_retries=2))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: session.begin_attempt(), range(30)))
    assert sum(r.status == BudgetStatus.COMPLETED for r in results) == 3


def test_queue_concurrency_and_release_boundaries():
    gate = AdmissionGate(BudgetSession(ReliabilityBudgets(max_concurrent=1, max_queued=1)))
    active = gate.acquire().value
    queued = gate.acquire().value
    assert active is not None and queued is not None
    assert not active.queued and queued.queued
    assert gate.acquire().status == BudgetStatus.REJECTED_BY_BUDGET
    assert queued.promote().status == BudgetStatus.REJECTED_BY_BUDGET
    assert active.close().status == BudgetStatus.COMPLETED
    assert active.close().status == BudgetStatus.COMPLETED
    assert active.promote().status == BudgetStatus.FAILED
    assert queued.promote().status == BudgetStatus.COMPLETED
    assert not queued.queued
    assert queued.close().status == BudgetStatus.COMPLETED
    assert gate.acquire().status == BudgetStatus.COMPLETED


def test_concurrent_admission_never_exceeds_capacity():
    gate = AdmissionGate(BudgetSession(ReliabilityBudgets(max_concurrent=3, max_queued=2)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: gate.acquire(), range(30)))
    leases = [r.value for r in results if r.status == BudgetStatus.COMPLETED]
    assert len(leases) == 5
    assert sum(not lease.queued for lease in leases) == 3
    assert sum(lease.queued for lease in leases) == 2
    for lease in leases:
        lease.close()


def test_queued_cancellation_and_timeout_require_explicit_release():
    now = [0.0]
    session = BudgetSession(ReliabilityBudgets(wall_time_seconds=1, max_queued=1), clock=lambda: now[0])
    gate = AdmissionGate(session)
    active = gate.acquire().value
    queued = gate.acquire().value
    active.close()
    now[0] = 1
    assert queued.promote().status == BudgetStatus.TIMED_OUT
    session.cancel()
    assert queued.promote().status == BudgetStatus.CANCELLED
    assert queued.close().status == BudgetStatus.COMPLETED


@pytest.mark.parametrize("text,limit,expected,truncated", [
    ("abc", 3, "abc", False), ("abcd", 3, "abc", True),
    ("éé", 4, "éé", False), ("éé", 3, "é", True),
    ("😀", 3, "", True), ("abc", 0, "", True), ("", 0, "", False),
])
def test_utf8_truncation(text, limit, expected, truncated):
    result = truncate_utf8(text, limit)
    assert result.status == BudgetStatus.COMPLETED
    assert result.value == expected and result.truncated is truncated
    assert len(result.value.encode("utf-8")) <= limit


def test_invalid_truncation_budget():
    assert truncate_utf8("test", -1).status == BudgetStatus.FAILED
    with pytest.raises(ValueError):
        audit_and_redact_results("test", "", [], max_output_bytes=-1)


def test_redaction_precedes_byte_truncation_and_bounds_replacement():
    out, err, _, audit = audit_and_redact_results(
        "😀😀", "prefix-private-secret-suffix", [],
        extra_tokens=["private-secret"], max_output_bytes=9,
    )
    assert out == "😀😀"
    assert "private" not in err
    assert len(out.encode()) <= 9 and len(err.encode()) <= 9
    assert audit.raw_value_matches == 1
    out, _, _, _ = audit_and_redact_results("secret", "", [], extra_tokens=["secret"], max_output_bytes=6)
    assert len(out.encode()) <= 6 and "secret" not in out


@pytest.mark.parametrize("capability", sorted(UNAVAILABLE_CAPABILITIES) + ["unknown"])
def test_missing_runtime_enforcement_is_unavailable(capability):
    assert capability_result(capability).status == BudgetStatus.UNAVAILABLE


def test_artifact_exact_bytes_and_count(tmp_path):
    artifacts = {"review_packet": {"message": "é"}}
    expected = json.dumps(artifacts["review_packet"], indent=2, sort_keys=True, allow_nan=False) + "\n"
    session = BudgetSession(ReliabilityBudgets(max_artifact_bytes=len(expected.encode()), max_artifacts=1))
    output = tmp_path / "out.json"
    write_validation_artifacts(artifacts, packet_output=str(output), repository_root=tmp_path / "repo", budget=session)
    assert output.read_bytes() == expected.encode()
    assert session.usage == {"max_artifacts": 1, "max_artifact_bytes": len(expected.encode())}


@pytest.mark.parametrize("budgets", [ReliabilityBudgets(max_artifact_bytes=0), ReliabilityBudgets(max_artifacts=0)])
def test_artifact_exceeded_preflight_no_files_or_directories(tmp_path, budgets):
    output = tmp_path / "not-created" / "out.json"
    session = BudgetSession(budgets)
    with pytest.raises(ArtifactOutputError) as caught:
        write_validation_artifacts({"review_packet": {}}, packet_output=str(output), repository_root=tmp_path / "repo", budget=session)
    assert caught.value.budget_result.status == BudgetStatus.REJECTED_BY_BUDGET
    assert not output.parent.exists()
    assert session.usage == {}


def test_artifact_batch_rejected_atomically(tmp_path):
    session = BudgetSession(ReliabilityBudgets(max_artifacts=1))
    with pytest.raises(ArtifactOutputError) as caught:
        write_validation_artifacts(
            {"review_packet": {}, "human_approval_manifest": {}},
            packet_output=str(tmp_path / "a.json"), manifest_output=str(tmp_path / "b.json"),
            repository_root=tmp_path / "repo", budget=session,
        )
    assert caught.value.budget_result.status == BudgetStatus.REJECTED_BY_BUDGET
    assert not (tmp_path / "a.json").exists() and not (tmp_path / "b.json").exists()


@pytest.mark.parametrize("cancelled", [False, True])
def test_artifact_timeout_and_cancel_prevent_writes(tmp_path, cancelled):
    now = [0.0]
    session = BudgetSession(ReliabilityBudgets(wall_time_seconds=1), clock=lambda: now[0])
    now[0] = 1
    if cancelled:
        session.cancel()
    with pytest.raises(ArtifactOutputError) as caught:
        write_validation_artifacts({"review_packet": {}}, packet_output=str(tmp_path / "a.json"), repository_root=tmp_path / "repo", budget=session)
    assert caught.value.budget_result.status == (BudgetStatus.CANCELLED if cancelled else BudgetStatus.TIMED_OUT)
    assert not (tmp_path / "a.json").exists()


def test_existing_command_timeout_and_launch_failure(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace

    from codeatlas.verification import executor
    from codeatlas.verification.models import ResourceLimits

    monkeypatch.setattr(executor, "run_git", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    monkeypatch.setattr(executor, "resolve_runner_command", lambda path, command, **kwargs: command)
    calls = []

    def timeout(command, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(executor.subprocess, "run", timeout)
    result = executor.execute_test_command(tmp_path, ["python", "-m", "pytest"], limits=ResourceLimits(timeout_seconds=0.1))
    assert result.status == "timed_out"
    assert calls[0]["timeout"] == 0.1
    assert calls[0]["capture_output"] is True
    assert capability_result("command_capture_memory").status == BudgetStatus.UNAVAILABLE

    def fail(command, **kwargs):
        raise FileNotFoundError("runner missing")

    monkeypatch.setattr(executor.subprocess, "run", fail)
    result = executor.execute_test_command(tmp_path, ["python", "-m", "pytest"])
    assert result.status == "error" and not result.execution_started


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_existing_provider_transport_exact_retry_limit(monkeypatch, retries):
    import urllib.error

    from codeatlas.providers import transport
    from codeatlas.providers.config import ProviderConfig
    from codeatlas.providers.errors import ProviderTransportError

    monkeypatch.setenv("RELIABILITY_TEST_KEY", "offline-test-key")
    config = ProviderConfig(model_name="offline", api_key_env_var="RELIABILITY_TEST_KEY", max_retries=retries)
    calls = []
    sleeps = []

    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        raise urllib.error.URLError("offline test")

    monkeypatch.setattr(transport.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(transport.time, "sleep", sleeps.append)
    with pytest.raises(ProviderTransportError):
        transport.HttpTransport().send({}, config)
    assert len(calls) == retries + 1
    assert len(sleeps) == retries
    assert all(call["timeout"] == config.timeout_seconds for call in calls)
    assert capability_result("provider_total_deadline").status == BudgetStatus.UNAVAILABLE


def test_existing_database_serialization_boundary_is_not_total_db_enforcement():
    from codeatlas.service.persistence import PersistenceError, canonical_json

    payload = {"message": "é"}
    text = canonical_json(payload)
    size = len(text.encode())
    assert canonical_json(payload, max_bytes=size) == text
    with pytest.raises(PersistenceError) as caught:
        canonical_json(payload, max_bytes=size - 1)
    assert caught.value.code == "persistence_payload_too_large"
    assert capability_result("database_total_bytes").status == BudgetStatus.UNAVAILABLE
