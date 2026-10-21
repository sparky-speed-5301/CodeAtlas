from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from codeatlas.release_evidence import (
    MODES,
    ReleaseEvidence,
    ReleaseObservation,
    load_release_evidence,
)


def _observation(
    *,
    status: str = "measured",
    mode: str = "offline",
    count: int = 1,
    source: str = "test-fixture",
    limitations: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "status": status,
        "mode": mode,
        "numerator": count,
        "denominator": count,
        "value": 1.0,
        "observation_count": count,
        "evidence_source": source,
        "exclusions": [],
        "limitations": list(limitations),
    }


def _not_run(mode: str) -> dict[str, object]:
    return {
        "status": "not_run",
        "mode": mode,
        "numerator": 0,
        "denominator": 0,
        "value": None,
        "observation_count": 0,
        "evidence_source": "not-run-fixture",
        "exclusions": [],
        "limitations": ["This mode was not exercised in the controlled run."],
    }


def _payload() -> dict[str, object]:
    now = datetime(2026, 10, 21, tzinfo=UTC).isoformat()
    return {
        "evidence_version": "limited-release-evidence-v1",
        "generated_at": now,
        "evidence_plan": {
            "plan_version": "controlled-beta-evidence-plan-v1",
            "plan_sha256": "a" * 64,
            "benchmark_version": "wp7-controlled-beta-v1",
            "observation": _observation(status="contract_only", count=1),
        },
        "benchmark": {
            "benchmark_version": "wp7-controlled-beta-v1",
            "suite_sha256": "b" * 64,
            "metric_catalog_sha256": "c" * 64,
            "observation": _observation(status="contract_only", count=1),
        },
        "source_revision": {
            "source_revision": "d" * 40,
            "source_tree_sha256": "e" * 64,
            "reproducibility_identity": "f" * 64,
            "observation": _observation(),
        },
        "sessions": [
            {
                "session_id": "beta-session-" + "1" * 32,
                "repository_identity_hash": "repo-" + "2" * 64,
                "case_id": "case-1",
                "provider_mode": "offline",
                "observation": _observation(),
            }
        ],
        "repository_counts": {
            "authorized_repositories": 1,
            "observed_repositories": 1,
            "excluded_repositories": 0,
            "observation": _observation(),
        },
        "provider_mode_coverage": [
            {
                "mode": mode,
                "sessions": 1 if mode == "offline" else 0,
                "provider_requests": 0,
                "observation": _observation() if mode == "offline" else _not_run(mode),
            }
            for mode in MODES
        ],
        "platform_coverage": [
            {
                "platform": "linux",
                "runtime": "python-3-11",
                "observation": _observation(status="contract_only"),
            }
        ],
        "artifacts": [],
        "kpis": [
            {
                "metric_name": name,
                "metric_group": group,
                "observation": _observation(status="contract_only"),
            }
            for name, group in (
                ("finding_precision", "review_usefulness"),
                ("proposal_generation_success", "autofix_usefulness"),
                ("review_completion_rate", "reliability"),
                ("secret_redaction_success", "trust_and_safety"),
            )
        ],
        "product_issues": [
            {
                "issue_code": "missing_required_evidence",
                "count": 1,
                "observation": _observation(status="contract_only"),
            }
        ],
        "rollback_observations": [
            {
                "operation": "rollback",
                "result": "not_run",
                "observation": _not_run("not_run"),
            }
        ],
        "cleanup_observations": [
            {
                "operation": "cleanup",
                "result": "passed",
                "observation": _observation(status="contract_only"),
            }
        ],
        "limitations": ["This fixture is contract-only and not external beta evidence."],
        "assessment": {
            "decision": "release_candidate_with_limitations",
            "evaluated_at": now,
            "evidence_complete": False,
            "critical_safety_failures": False,
            "blocking_conditions": [],
            "limitations": ["External beta evidence is not evaluated."],
            "production_ready": False,
        },
    }


def _validate(payload: dict[str, object]) -> ReleaseEvidence:
    return ReleaseEvidence.model_validate_json(json.dumps(payload))


def _validate_observation(payload: dict[str, object]) -> ReleaseObservation:
    return ReleaseObservation.model_validate_json(json.dumps(payload))


def test_release_evidence_contract_preserves_modes_groups_and_limitations() -> None:
    evidence = _validate(_payload())

    assert tuple(item.mode for item in evidence.provider_mode_coverage) == MODES
    assert {item.metric_group for item in evidence.kpis} == {
        "review_usefulness",
        "autofix_usefulness",
        "reliability",
        "trust_and_safety",
    }
    assert evidence.assessment.decision == "release_candidate_with_limitations"
    assert evidence.assessment.production_ready is False


def test_release_observation_rejects_bad_counts_and_requires_blocked_limitations() -> None:
    with pytest.raises(ValidationError, match="numerator"):
        _validate_observation(_observation(count=2) | {"denominator": 1, "value": 1.0})

    with pytest.raises(ValidationError, match="blocked observations require limitations"):
        _validate_observation(_not_run("blocked") | {"status": "blocked", "limitations": []})

    blocked = _validate_observation(
        _not_run("blocked") | {"status": "blocked", "limitations": ["Policy gate blocked execution."]}
    )
    assert blocked.status == "blocked"
    assert blocked.mode == "blocked"


def test_release_evidence_rejects_duplicates_missing_groups_and_incomplete_identity() -> None:
    payload = _payload()
    payload["sessions"].append(payload["sessions"][0])  # type: ignore[union-attr]
    with pytest.raises(ValidationError, match="session identifiers"):
        _validate(payload)

    payload = _payload()
    payload["kpis"] = payload["kpis"][:3]  # type: ignore[index]
    with pytest.raises(ValidationError, match="all required metric groups"):
        _validate(payload)

    payload = _payload()
    payload["source_revision"]["source_tree_sha256"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="source identity"):
        _validate(payload)


def test_release_candidate_requires_complete_unlimited_evidence() -> None:
    payload = _payload()
    payload["assessment"]["decision"] = "release_candidate"  # type: ignore[index]
    payload["assessment"]["evidence_complete"] = True  # type: ignore[index]
    payload["assessment"]["limitations"] = []  # type: ignore[index]
    with pytest.raises(ValidationError, match="release_candidate"):
        _validate(payload)


def test_loader_rejects_duplicate_oversized_linked_and_secret_payloads(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"evidence_version":"x","evidence_version":"y"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_release_evidence(duplicate)

    oversized = tmp_path / "oversized.json"
    oversized.write_text("{" + "x" * 64_001 + "}", encoding="utf-8")
    with pytest.raises(ValueError, match="bound"):
        load_release_evidence(oversized)

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(_payload()), encoding="utf-8")
    linked = tmp_path / "linked.json"
    linked.hardlink_to(valid)
    with pytest.raises(ValueError, match="unlinked"):
        load_release_evidence(linked)

    secret = _payload()
    secret["limitations"] = ["sk-" + "x" * 30]
    secret_path = tmp_path / "secret.json"
    secret_path.write_text(json.dumps(secret), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        load_release_evidence(secret_path)
