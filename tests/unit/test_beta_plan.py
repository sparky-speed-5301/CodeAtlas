from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from jsonschema import Draft202012Validator

from codeatlas.beta_plan import (
    BENCHMARK_VERSION,
    BetaEvidencePlan,
    load_evidence_plan,
    validate_evidence_plan,
    validate_evidence_plan_file,
)

PLAN_PATH = Path("config/controlled-beta-evidence-plan.json")


def _payload() -> dict[str, object]:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def test_checked_in_plan_is_bounded_and_configured() -> None:
    plan = load_evidence_plan(PLAN_PATH)

    assert plan.status == "configured"
    assert plan.benchmark_version == BENCHMARK_VERSION
    assert plan.production_ready is False
    assert plan.release_assessment_status == "not_evaluated"
    assert plan.cohort_size.target_participants == 8
    assert plan.cohort_size.maximum_participants == 12
    assert sum(item.target_cases for item in plan.language_distribution) == 18
    assert sum(item.allocation.target_cases for item in plan.review_case_distribution) == 18
    assert sum(item.allocation.target_cases for item in plan.fix_proposal_case_distribution) == 8
    assert plan.consent_requirements.consent_mode == "explicit_opt_in_before_collection"
    assert plan.session_selection.unsolicited_collection is False
    assert plan.session_selection.automatic_collection is False

    schema = json.loads(
        Path("schemas/controlled-beta-evidence-plan.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_payload())


def test_plan_has_no_raw_or_secret_bearing_fields() -> None:
    payload = BetaEvidencePlan.model_validate(_payload()).model_dump(mode="json")
    keys: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            keys.update(str(key).lower() for key in value)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(payload)
    assert keys.isdisjoint(
        {
            "source",
            "source_code",
            "prompt",
            "provider_transcript",
            "credential",
            "approval_token",
            "reviewer_name",
            "reviewer_email",
            "repository_path",
            "stdout",
            "stderr",
        }
    )
    assert payload["collection_privacy"] == {
        "collection_mode": "explicit_artifact_export_only",
        "raw_repository_source": False,
        "raw_prompts": False,
        "raw_provider_transcripts": False,
        "credentials": False,
        "approval_tokens": False,
        "unrestricted_environment": False,
        "reviewer_identity_without_consent": False,
        "automatic_apply": False,
        "automatic_approval": False,
        "automatic_commit": False,
        "automatic_merge": False,
        "automatic_push": False,
        "arbitrary_commands": False,
        "dependency_installation": False,
        "unrestricted_network_access": False,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("benchmark_version", "unknown-v1"),
        ("cohort_size", {"minimum_participants": 1, "target_participants": 2, "maximum_participants": 51, "maximum_sessions": 51}),
        ("retention_policy", {"retention_days": 91}),
        ("observation_period", {"start_date": "2026-10-26", "end_date": "2027-01-24"}),
        ("consent_requirements", {"consent_mode": "implicit_collection"}),
    ],
)
def test_plan_rejects_invalid_required_bounds_or_modes(field: str, value: object) -> None:
    payload = _payload()
    if isinstance(value, dict):
        original = payload[field]
        assert isinstance(original, dict)
        original.update(value)
    else:
        payload[field] = value
    with pytest.raises(ValidationError):
        BetaEvidencePlan.model_validate(payload)


def test_plan_rejects_contradictory_distributions_and_thresholds() -> None:
    payload = _payload()
    payload["language_distribution"][0]["target_cases"] = 7  # type: ignore[index]
    with pytest.raises(ValidationError, match="distribution"):
        BetaEvidencePlan.model_validate(payload)

    payload = _payload()
    payload["success_thresholds"][0]["direction"] = "maximum"  # type: ignore[index]
    with pytest.raises(ValidationError, match="direction"):
        BetaEvidencePlan.model_validate(payload)


def test_plan_requires_explicit_statuses_and_does_not_claim_evaluation() -> None:
    payload = _payload()
    payload["status"] = "blocked"
    payload["status_reason"] = "consent_not_obtained"
    blocked = validate_evidence_plan(payload)
    assert blocked.status == "blocked"
    assert blocked.valid is False
    assert blocked.plan is not None

    payload["status"] = "not_evaluated"
    payload["status_reason"] = "no_observations_available"
    not_evaluated = validate_evidence_plan(payload)
    assert not_evaluated.status == "not_evaluated"
    assert not_evaluated.valid is False

    payload["status"] = "configured"
    payload["status_reason"] = "consent_not_obtained"
    contradictory = validate_evidence_plan(payload)
    assert contradictory.status == "invalid"
    assert contradictory.error_code == "schema_validation_failed"

    invalid = validate_evidence_plan({"api_key": "sk-test-secret-value"})
    assert invalid.status == "invalid"
    assert invalid.valid is False
    assert "sk-test-secret-value" not in invalid.model_dump_json()


def test_loader_rejects_duplicate_oversized_and_linked_files(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"status":"configured","status":"blocked"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_evidence_plan(duplicate)

    oversized = tmp_path / "oversized.json"
    oversized.write_text("{" + "x" * 32_768 + "}", encoding="utf-8")
    with pytest.raises(ValueError, match="bound"):
        load_evidence_plan(oversized)

    target = tmp_path / "target.json"
    target.write_text(PLAN_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    linked = tmp_path / "linked.json"
    linked.hardlink_to(target)
    with pytest.raises(ValueError, match="unlinked"):
        load_evidence_plan(linked)

    result = validate_evidence_plan_file(duplicate)
    assert result.status == "invalid"
    assert result.error_code == "schema_validation_failed"


def test_plan_rejects_secret_like_extra_fields() -> None:
    payload = _payload()
    payload["provider_transcript"] = "do not persist this"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BetaEvidencePlan.model_validate(payload)
