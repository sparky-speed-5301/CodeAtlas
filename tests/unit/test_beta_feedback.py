"""Enum-only beta feedback persistence and scope regression fixtures."""

from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from codeatlas.beta import write_beta_export
from codeatlas.beta_execution import load_feedback_evidence, summarize_execution
from codeatlas.findings.models import FeedbackOutcome, FeedbackReason
from codeatlas.service.models import FindingFeedbackRequest
from codeatlas.service.persistence import repository_id
from codeatlas.service.persistence_codec import make_snapshot
from codeatlas.service.state import ReviewStateManager, ServiceStateError
from tests.unit.test_service_persistence import _record


@pytest.mark.parametrize("outcome", get_args(FeedbackOutcome))
def test_outcomes(outcome):
    assert FindingFeedbackRequest(outcome=outcome).effective_outcome == outcome


@pytest.mark.parametrize("reason", get_args(FeedbackReason))
def test_reasons(reason):
    assert FindingFeedbackRequest(outcome="rejected", reason=reason).reason == reason


@pytest.mark.parametrize("data", [{"outcome": "automatic_approval"}, {"outcome": "accepted", "reason": "secret=bad"},
                                     {"reason": "correct"}, {"outcome": "accepted", "text": "raw source"},
                                     {"outcome": "accepted", "feedback": "rejected"}])
def test_invalid_feedback(data):
    with pytest.raises(ValidationError):
        FindingFeedbackRequest.model_validate(data)


def test_persistence_restart_duplicate_reversal_and_scope(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "service.sqlite3"
    manager = ReviewStateManager(persistence_path=path)
    record = _record(repo, "beta-feedback-run")
    manager._reviews[record.run_id] = record
    manager._checkpoint(record, "review_completed", required=True)
    result = manager.set_finding_feedback(record.run_id, "CA-PERSIST", outcome="accepted", reason="correct")
    assert result.changed and not result.reversed
    assert not manager.set_finding_feedback(record.run_id, "CA-PERSIST", outcome="accepted", reason="correct").changed
    snapshot = make_snapshot(manager, record)
    assert snapshot is not None
    manager.close()
    manager = ReviewStateManager(persistence_path=path)
    try:
        result = manager.get_finding_detail(record.run_id, "CA-PERSIST")
        assert result.feedback_outcome == "accepted"
        assert result.feedback_reason == "correct"
        export = manager.export_beta_feedback(record.run_id, repository_id(repo))
        target = tmp_path / "feedback.json"
        write_beta_export(export, target)
        loaded = load_feedback_evidence(target)
        assert loaded.items[0].outcome == "accepted"
        assert summarize_execution(feedback=(loaded,)).buckets[0].finding_acceptance.value == 1
        with pytest.raises(ServiceStateError):
            manager.export_beta_feedback(record.run_id, "repo-" + "0" * 64)
        changed = manager.set_finding_feedback(record.run_id, "CA-PERSIST", outcome="rejected", reason="false_positive")
        assert changed.reversed and changed.previous_outcome == "accepted"
        assert manager.set_finding_feedback(record.run_id, "CA-PERSIST").reversed
        assert manager.get_finding_detail(record.run_id, "CA-PERSIST").feedback_outcome is None
        other = _record(tmp_path, "other-run")
        other.findings[0]["id"] = "CA-OTHER"
        manager._reviews[other.run_id] = other
        with pytest.raises((KeyError, ValueError)):
            manager.set_finding_feedback(other.run_id, "CA-PERSIST", outcome="accepted")
    finally:
        manager.close()
