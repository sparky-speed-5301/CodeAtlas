from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from codeatlas.benchmark.product_validation import (
    REQUIRED_VALIDATION_CATEGORIES,
    REQUIRED_VALIDATION_LANGUAGES,
    ControlledExpectedAnchor,
    ControlledRepositoryCase,
    ControlledRepositorySuite,
    HumanReviewEvaluation,
    load_controlled_repository_suite,
    load_human_evaluations,
    load_metric_catalog,
)
from codeatlas.benchmark.repository import PatchScopeExpectation
from codeatlas.benchmark.repository import (
    TestCommandPolicy as BenchmarkTestCommandPolicy,
)

BENCHMARK_VERSION = "wp7-controlled-beta-v1"
REVIEW_OUTCOMES = {
    "accepted",
    "rejected",
    "needs_more_context",
    "needs_regeneration",
    "abstained",
    "not_evaluated",
}
EXPECTED_METRICS = {
    "review_usefulness": {
        "finding_precision", "finding_recall", "actionable_finding_rate",
        "accepted_finding_rate", "rejected_finding_rate", "duplicate_rate",
        "false_positive_rate", "severity_usefulness", "anchor_usefulness",
        "explanation_usefulness", "evidence_usefulness",
        "time_to_understand_finding", "time_to_resolve_finding",
        "time_to_first_useful_finding",
    },
    "autofix_usefulness": {
        "proposal_eligibility_rate", "proposal_generation_success",
        "patch_parse_success", "patch_validation_success", "targeted_test_success",
        "user_approval_rate", "user_rejection_rate", "regeneration_rate",
        "apply_conflict_rate", "revert_rate", "regression_rate",
        "time_to_accepted_fix", "time_saved_vs_manual_correction",
    },
    "reliability": {
        "review_completion_rate", "timeout_rate", "cancellation_rate",
        "provider_failure_rate", "recovery_correctness", "stale_run_correctness",
        "github_delivery_correctness", "extension_startup_success",
        "service_crash_recovery", "orphaned_process_rate",
    },
    "trust_and_safety": {
        "secret_redaction_success", "unsafe_command_rejection",
        "unauthorized_apply_rejection", "cross_repository_isolation",
        "stale_proposal_rejection", "token_leakage_rate",
        "unexpected_workspace_mutation_rate",
    },
}


def _evaluation(**updates: object) -> dict[str, object]:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    data: dict[str, object] = {
        "protocol_version": "human-review-v1",
        "benchmark_version": BENCHMARK_VERSION,
        "case_id": "case-1",
        "run_id": "run:1",
        "reviewer_id_hash": "a" * 64,
        "context_identity_hash": "b" * 64,
        "started_at": started,
        "completed_at": started + timedelta(minutes=1),
        "understand_finding": "yes",
        "identify_affected_code": "yes",
        "understand_evidence": "yes",
        "decide_finding_validity": "yes",
        "understand_proposed_fix": "not_evaluated",
        "choose_fix_action": "not_evaluated",
        "validate_result": "not_evaluated",
        "recover_safely": "not_evaluated",
        "finding_outcome": "accepted",
        "fix_outcome": "not_evaluated",
        "time_to_understand_finding_ms": 1_000,
        "time_to_resolve_finding_ms": 10_000,
        "time_to_first_useful_finding_ms": 1_000,
        "reason_codes": ["evidence_clear"],
    }
    data.update(updates)
    return data


def _case(language: str, category: str, *, case_id: str | None = None) -> ControlledRepositoryCase:
    identifier = case_id or f"{language}-{category}"
    anchor = ControlledExpectedAnchor(
        anchor_id=f"{identifier}-anchor",
        file="src/example.txt",
        start_line=1,
        end_line=2,
        category="CORRECTNESS",
        severity="medium",
        disposition="report",
        description="The changed branch has a deterministic correctness defect.",
    )
    return ControlledRepositoryCase(
        case_id=identifier,
        repository_identity="public/example",
        repository_url="https://github.com/public/example.git",
        base_sha=(identifier.encode().hex() + "1" * 40)[:40],
        head_sha=(identifier.encode().hex() + "2" * 40)[:40],
        language=language,
        changed_files=("src/example.txt",),
        validation_categories=(category,),
        expected_findings=("The changed branch returns the wrong value.",),
        expected_anchors=(anchor,),
        expected_severity=("medium",),
        expected_abstention="forbidden",
        expected_fixability="fixable",
        expected_patch_scope=PatchScopeExpectation(allowed_files=("src/example.txt",)),
        test_policy=BenchmarkTestCommandPolicy(policy="targeted_only", allowed_commands=("test example",)),
        license_spdx="MIT",
        provenance_urls=("https://github.com/public/example/commit/" + "2" * 40,),
        provenance="Public commit metadata and independently reviewed patch labels.",
        limitations=("Metadata-only contract; the repository test was not executed.",),
        benchmark_version=BENCHMARK_VERSION,
    )


def test_metric_catalog_has_exact_independent_metric_groups() -> None:
    catalog = load_metric_catalog("eval/product-validation/metrics-v1.json")
    assert catalog.catalog_version == "product-metrics-v1"
    assert catalog.benchmark_version == BENCHMARK_VERSION
    actual = {
        group: {metric.name for metric in catalog.metrics if metric.group == group}
        for group in EXPECTED_METRICS
    }
    assert actual == EXPECTED_METRICS
    assert len(catalog.metrics) == sum(map(len, EXPECTED_METRICS.values()))
    assert all(metric.numerator and metric.denominator and metric.population for metric in catalog.metrics)
    assert all(metric.benchmark_version == BENCHMARK_VERSION for metric in catalog.metrics)
    assert all(
        metric.confidence_interval_method == ("wilson_95" if metric.unit == "rate" else "bootstrap_95")
        for metric in catalog.metrics
    )


def test_metric_catalog_rejects_duplicates_and_version_drift() -> None:
    catalog = load_metric_catalog("eval/product-validation/metrics-v1.json")
    payload = catalog.model_dump(mode="json")
    payload["metrics"].append(payload["metrics"][0])
    with pytest.raises(ValidationError, match="unique"):
        type(catalog).model_validate(payload)

    payload = catalog.model_dump(mode="json")
    payload["metrics"][0]["benchmark_version"] = "other"
    with pytest.raises(ValidationError, match="benchmark versions"):
        type(catalog).model_validate(payload)


def test_controlled_case_rejects_unpinned_unsafe_or_inconsistent_metadata() -> None:
    case = _case("python", "real_bugs")
    payload = case.model_dump(mode="json")
    payload["repository_url"] = "https://user:secret@example.com/repository.git"
    with pytest.raises(ValidationError, match="public HTTPS"):
        ControlledRepositoryCase.model_validate(payload)

    payload = case.model_dump(mode="json")
    payload["expected_anchors"][0]["file"] = "../outside.py"
    with pytest.raises(ValidationError, match="safe repository-relative"):
        ControlledRepositoryCase.model_validate(payload)

    payload = case.model_dump(mode="json")
    payload["expected_findings"] = []
    with pytest.raises(ValidationError, match="align with report anchors"):
        ControlledRepositoryCase.model_validate(payload)


def test_controlled_suite_requires_languages_categories_and_unique_case_ids() -> None:
    cases = [
        _case("python", "clean_changes", case_id="clean"),
        _case("javascript_typescript", "real_bugs", case_id="bug"),
        _case("go", "security_issues", case_id="security"),
        _case("java", "sensitive_data_exposure", case_id="sensitive"),
        _case("rust", "authorization_authentication", case_id="authorization"),
        _case("python", "type_nullability", case_id="type"),
        _case("python", "performance_regressions", case_id="performance"),
        _case("python", "ambiguous_changes", case_id="ambiguous"),
        _case("python", "correct_abstention", case_id="abstention"),
        _case("python", "ai_generated_defects", case_id="ai"),
    ]
    suite = ControlledRepositorySuite(
        benchmark_version=BENCHMARK_VERSION,
        generalization_limit="This small curated set does not support statistical generalization.",
        cases=tuple(cases),
    )
    assert {case.language for case in suite.cases} == REQUIRED_VALIDATION_LANGUAGES
    assert {category for case in suite.cases for category in case.validation_categories} == REQUIRED_VALIDATION_CATEGORIES

    with pytest.raises(ValidationError, match="missing required languages"):
        ControlledRepositorySuite(
            benchmark_version=BENCHMARK_VERSION,
            generalization_limit=suite.generalization_limit,
            cases=tuple(case for case in cases if case.language != "rust"),
        )
    with pytest.raises(ValidationError, match="case IDs must be unique"):
        ControlledRepositorySuite(
            benchmark_version=BENCHMARK_VERSION,
            generalization_limit=suite.generalization_limit,
            cases=(*suite.cases, suite.cases[0]),
        )


def test_checked_in_controlled_suite_is_metadata_only_and_complete() -> None:
    suite = load_controlled_repository_suite("eval/cases/product-validation/v1/suite.json")
    assert suite.benchmark_version == BENCHMARK_VERSION
    assert {case.language for case in suite.cases} >= REQUIRED_VALIDATION_LANGUAGES
    assert {category for case in suite.cases for category in case.validation_categories} >= REQUIRED_VALIDATION_CATEGORIES
    assert all(case.source_persistence == "metadata_only" for case in suite.cases)
    assert all(case.provider_material_collected is False for case in suite.cases)
    payload = suite.model_dump(mode="json")
    keys: set[str] = set()

    def collect_keys(value: object) -> None:
        if isinstance(value, dict):
            keys.update(str(key).lower() for key in value)
            for nested in value.values():
                collect_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_keys(nested)

    collect_keys(payload)
    assert keys.isdisjoint({"raw_prompt", "provider_transcript", "credential", "source_code"})


def test_human_review_outcomes_schema_and_privacy_are_exact() -> None:
    evaluation = HumanReviewEvaluation.model_validate(_evaluation())
    schema = HumanReviewEvaluation.model_json_schema()
    assert set(schema["properties"]["finding_outcome"]["enum"]) == REVIEW_OUTCOMES
    assert set(schema["properties"]["fix_outcome"]["enum"]) == REVIEW_OUTCOMES
    checked_in = json.loads(Path("schemas/human-review-evaluation.schema.json").read_text(encoding="utf-8"))
    assert checked_in == schema
    assert evaluation.finding_outcome == "accepted"

    for prohibited in ("reviewer_comment", "source", "prompt", "provider_transcript", "credential", "approval_token"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            HumanReviewEvaluation.model_validate(_evaluation(**{prohibited: "not allowed"}))


def test_human_review_rejects_timing_contradictions_and_duplicate_rows(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot report resolution time"):
        HumanReviewEvaluation.model_validate(_evaluation(finding_outcome="not_evaluated"))
    with pytest.raises(ValidationError, match="requires an accepted fix"):
        HumanReviewEvaluation.model_validate(_evaluation(time_to_accepted_fix_ms=1))
    with pytest.raises(ValidationError, match="requires an understood finding"):
        HumanReviewEvaluation.model_validate(
            _evaluation(understand_finding="no", time_to_understand_finding_ms=1)
        )
    with pytest.raises(ValidationError, match="requires an accepted finding"):
        HumanReviewEvaluation.model_validate(
            _evaluation(finding_outcome="rejected", time_to_first_useful_finding_ms=1)
        )
    with pytest.raises(ValidationError, match="must not precede"):
        HumanReviewEvaluation.model_validate(
            _evaluation(completed_at=datetime(2025, 1, 1, tzinfo=UTC))
        )
    with pytest.raises(ValidationError, match="bounded identifiers"):
        HumanReviewEvaluation.model_validate(_evaluation(reason_codes=["free form comment"] ))

    row = HumanReviewEvaluation.model_validate(_evaluation()).model_dump_json()
    path = tmp_path / "evaluations.jsonl"
    path.write_text(f"{row}\n{row}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate reviewer evaluation"):
        load_human_evaluations(path)
