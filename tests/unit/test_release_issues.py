from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codeatlas.beta_incidents import BetaIncidentReport, create_beta_incident
from codeatlas.beta_session import BetaSession, BetaSessionEvent
from codeatlas.release_issues import ISSUE_CODES, aggregate_product_issues
from codeatlas.service.persistence import repository_id


def _session(
    *,
    failure_code: str | None = None,
    session_id: str = "1",
    repository_identity_hash: str = "repo-" + "a" * 64,
) -> BetaSession:
    now = datetime(2026, 10, 21, tzinfo=UTC)
    return BetaSession(
        session_id="beta-session-" + session_id * 32,
        repository_identity_hash=repository_identity_hash,
        case_id="case-1",
        language="python",
        base_sha="b" * 40,
        head_sha="c" * 40,
        provider_mode="offline",
        lifecycle_status="failed" if failure_code else "active",
        created_at=now,
        updated_at=now,
        events=(BetaSessionEvent(sequence=1, stage="created", result="offline", at=now),),
        events_total=1,
        failure_code=failure_code,
    )


def test_issue_aggregation_has_stable_order_and_separate_sources(tmp_path) -> None:
    repository_identity = repository_id(tmp_path)
    incidents = BetaIncidentReport(
        repository_identity_hash=repository_identity,
        incidents=(
            create_beta_incident(repository=tmp_path, reason_code="provider_failed"),
            create_beta_incident(repository=tmp_path, reason_code="orphan_worktree"),
            create_beta_incident(repository=tmp_path, reason_code="tools_unavailable"),
        ),
    )
    result = aggregate_product_issues(
        (
            _session(
                failure_code="provider_unavailable",
                repository_identity_hash=repository_identity,
            ),
            _session(
                failure_code="operation_timeout",
                session_id="2",
                repository_identity_hash=repository_identity,
            ),
        ),
        incidents=incidents,
        evidence_basis="contract_only",
    )

    assert tuple(item.issue_code for item in result.issues) == ISSUE_CODES
    by_code = {item.issue_code: item for item in result.issues}
    assert by_code["provider_unavailable"].session_count == 1
    assert by_code["provider_failure"].incident_count == 1
    assert by_code["timeout"].session_count == 1
    assert by_code["orphan_worktree"].incident_count == 1
    assert by_code["unsupported_tool"].incident_count == 1
    assert result.production_ready is False


def test_issue_aggregation_does_not_fabricate_zero_input_success() -> None:
    result = aggregate_product_issues()

    assert result.session_count == 0
    assert result.incident_count == 0
    assert result.limitations
    assert all(item.count == 0 for item in result.issues)


def test_issue_aggregation_rejects_duplicate_sessions() -> None:
    session = _session()
    with pytest.raises(ValueError, match="duplicate sessions"):
        aggregate_product_issues((session, session))
