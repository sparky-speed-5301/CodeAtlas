"""Contract-only beta report fixtures; no live-provider quality claims."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from codeatlas.beta import build_beta_report, default_beta_config, write_beta_export
from codeatlas.beta_execution import BetaExecutionSummary, summarize_execution
from codeatlas.beta_session import BetaSession, BetaSessionEvent
from codeatlas.cli import app


def session(mode="offline", number=1, **updates):
    now = datetime.now(UTC)
    data = {"session_id": f"beta-session-{number:032x}", "repository_identity_hash": "repo-" + "a" * 64,
            "case_id": "contract-fixture", "language": "python", "base_sha": "b" * 40, "head_sha": "c" * 40,
            "provider_mode": mode, "created_at": now, "updated_at": now,
            "events": (BetaSessionEvent(sequence=1, stage="created", result="not_run", at=now),), "events_total": 1}
    data.update(updates)
    return BetaSession.model_validate(data)


def test_mode_separation_and_no_fabricated_success():
    summary = summarize_execution(tuple(session(mode, i) for i, mode in enumerate(("offline", "live", "fallback", "unavailable", "blocked", "not_run"))), evidence_basis="contract_only")
    assert summary.session_count == 6
    assert all(bucket.sessions == 1 for bucket in summary.buckets)
    assert all(bucket.review_completion.value is None for bucket in summary.buckets)
    assert all(bucket.false_positive_rate.status == "unavailable" for bucket in summary.buckets)
    assert all(bucket.fix_proposal_acceptance.status == "unavailable" for bucket in summary.buckets)
    assert summary.correctness_established is False


def test_empty_and_duplicate_bounds():
    empty = summarize_execution()
    assert empty.evidence_basis == "unavailable"
    with pytest.raises(ValueError, match="duplicate"):
        summarize_execution((session(), session()))
    with pytest.raises(ValueError, match="too many"):
        summarize_execution(tuple(session(number=i) for i in range(51)))


def test_rate_counts_and_safe_export(tmp_path: Path):
    from codeatlas.beta_session import SeverityDistribution

    item = session(lifecycle_status="completed", finding_count=4,
                   severity_distribution=SeverityDistribution(high=4), accepted_count=1, rejected_count=1,
                   abstained_count=1, review_duration_ms=12.0, validation_result="passed", test_result="not_run")
    summary = summarize_execution((item,), evidence_basis="contract_only")
    bucket = summary.buckets[0]
    assert bucket.finding_acceptance.value == 0.5
    assert bucket.abstention.value == 0.25
    assert bucket.validation_success.value == 1
    assert bucket.test_success.value is None
    report = build_beta_report(default_beta_config(), execution=summary)
    target = tmp_path / "report.json"
    write_beta_export(report, target)
    payload = target.read_text()
    assert len(payload.encode()) < 32768
    assert "contract_only" in payload
    assert "beta-session-" not in payload
    assert "repository_identity_hash" not in payload
    with pytest.raises(ValueError):
        write_beta_export(report, target)


def test_schema_rejects_inconsistent_counts():
    value = summarize_execution().model_dump()
    value["session_count"] = 1
    with pytest.raises(ValidationError):
        BetaExecutionSummary.model_validate(value)


def test_report_cli_no_evidence():
    result = CliRunner().invoke(app, ["--json", "beta-report"])
    assert result.exit_code == 0, result.output
    assert '"beta_ready": true' in result.output
