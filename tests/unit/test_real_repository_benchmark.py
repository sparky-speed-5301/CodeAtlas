from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from codeatlas.benchmark import (
    ExpectedAnchor,
    ObservedFinding,
    PatchScopeExpectation,
    RepairObservation,
    RepositoryBenchmarkCase,
    RepositoryBenchmarkSuite,
    ReviewObservation,
    load_benchmark_suite,
    score_repair_benchmark,
    score_review_benchmark,
)
from codeatlas.benchmark import (
    TestCommandPolicy as BenchmarkTestCommandPolicy,
)
from codeatlas.benchmark.runner import run_benchmark_files, write_benchmark_report
from codeatlas.cli import app

VERSION = "wp6-v1"


def _case(
    case_id: str = "bug",
    *,
    population: str = "positive",
    category: str = "real_bugs",
    abstention: str = "forbidden",
) -> RepositoryBenchmarkCase:
    anchors = () if population != "positive" else (
        ExpectedAnchor(
            anchor_id="bug-1",
            file="src/app.py",
            start_line=3,
            end_line=4,
            category="CORRECTNESS",
            severity="high",
            fixability="fixable",
        ),
    )
    return RepositoryBenchmarkCase(
        case_id=case_id,
        fixture_ref=f"fixtures/{case_id}",
        repository_identity="public/example@sanitized",
        base_sha="1" * 40,
        head_sha="2" * 40,
        language=("python",),
        changed_files=("src/app.py",),
        benchmark_category=category,
        population=population,
        expected_finding_categories=tuple(anchor.category for anchor in anchors),
        expected_finding_anchors=anchors,
        expected_severity=tuple(anchor.severity for anchor in anchors),
        expected_abstention=abstention,
        expected_fixability="fixable" if population == "positive" else "not_fixable",
        expected_proposal_eligible=population == "positive",
        expected_patch_scope=PatchScopeExpectation(allowed_files=("src/app.py",)),
        expected_test_command_policy=BenchmarkTestCommandPolicy(policy="targeted_only", allowed_commands=("pytest -q tests/test_app.py",)),
        known_limitations=("Sanitized fixture; timing is not representative.",),
        benchmark_version=VERSION,
    )


def _finding(**updates: object) -> ObservedFinding:
    data = {
        "finding_id": "CA-BENCH-1",
        "file": "src/app.py",
        "start_line": 3,
        "end_line": 3,
        "category": "CORRECTNESS",
        "severity": "high",
        "quality_decision": "report",
        "evidence": ("Observed failing branch",),
        "rank": 1,
    }
    data.update(updates)
    return ObservedFinding.model_validate(data)


def _metric(report: object, name: str) -> object:
    return next(metric for metric in report.metrics if metric.name == name)  # type: ignore[attr-defined]


def test_case_contract_requires_source_identity_and_aligned_labels() -> None:
    data = _case().model_dump(mode="json")
    data["fixture_ref"] = None
    with pytest.raises(ValidationError, match="repository_url or fixture_ref"):
        RepositoryBenchmarkCase.model_validate(data)

    data = _case().model_dump(mode="json")
    data["expected_severity"] = ["low"]
    with pytest.raises(ValidationError, match="expected_severity"):
        RepositoryBenchmarkCase.model_validate(data)


def test_suite_rejects_duplicate_ids_and_version_drift() -> None:
    with pytest.raises(ValidationError, match="unique"):
        RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(_case(), _case()))
    with pytest.raises(ValidationError, match="benchmark_version"):
        RepositoryBenchmarkSuite(benchmark_version="other", cases=(_case(),))


def test_review_metrics_keep_populations_and_denominators_separate() -> None:
    positive = _case()
    negative = _case("clean", population="negative", category="clean_changes")
    ambiguous = _case("unclear", population="ambiguous", category="correct_abstention", abstention="required")
    suite = RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(positive, negative, ambiguous))
    observations = [
        ReviewObservation(case_id="bug", mode="deterministic", findings=(_finding(),), time_to_first_finding_ms=4, total_review_duration_ms=10),
        ReviewObservation(case_id="clean", mode="deterministic", findings=(), total_review_duration_ms=5),
        ReviewObservation(case_id="unclear", mode="deterministic", abstained=True, abstention_reason="intent is not established", total_review_duration_ms=3),
    ]
    report = score_review_benchmark(suite, observations)[0]
    assert _metric(report, "precision").value == 1
    assert _metric(report, "recall").value == 1
    assert _metric(report, "false_positive_rate").denominator == 1
    assert _metric(report, "abstention_rate").value == pytest.approx(1 / 3)
    assert _metric(report, "coverage").value == pytest.approx(2 / 3)
    assert _metric(report, "finding_anchor_accuracy").value == 1
    assert _metric(report, "time_to_first_finding").value == 4
    assert _metric(report, "precision").confidence_interval is not None


def test_review_metrics_expose_anchor_severity_evidence_duplicates_and_ranking() -> None:
    suite = RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(_case(),))
    observations = [ReviewObservation(
        case_id="bug",
        mode="live_provider",
        findings=(
            _finding(file="src/app.py", start_line=30, end_line=30, severity="medium", evidence=(), rank=2),
            _finding(finding_id="CA-DUP", category="STYLE", rank=1, duplicate_of="CA-BENCH-1"),
        ),
        total_review_duration_ms=20,
        provider_attempts=2,
        provider_failures=1,
        retries=1,
        input_tokens=100,
        output_tokens=20,
        cost=0.01,
    )]
    report = score_review_benchmark(suite, observations)[0]
    assert _metric(report, "finding_anchor_accuracy").value == 0
    assert _metric(report, "severity_accuracy").value == 0
    assert _metric(report, "evidence_completeness").value == 0
    assert _metric(report, "duplicate_rate").value == pytest.approx(0.5)
    assert _metric(report, "ranking_quality").value == pytest.approx(0.5)
    assert _metric(report, "ranking_quality").confidence_interval is None
    assert _metric(report, "provider_failure_rate").value == pytest.approx(0.5)
    assert _metric(report, "retry_rate").value == pytest.approx(0.5)


def test_quality_decision_accuracy_includes_required_abstention() -> None:
    ambiguous = _case("unclear", population="ambiguous", category="correct_abstention", abstention="required")
    suite = RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(ambiguous,))
    report = score_review_benchmark(suite, [
        ReviewObservation(case_id="unclear", mode="deterministic", abstained=False, total_review_duration_ms=1),
    ])[0]
    assert _metric(report, "abstention_behavior_accuracy").value == 0
    assert _metric(report, "quality_decision_accuracy").value == 0


def test_review_reports_separate_deterministic_and_provider_modes() -> None:
    suite = RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(_case(),))
    reports = score_review_benchmark(suite, [
        ReviewObservation(case_id="bug", mode="deterministic", findings=(_finding(),), total_review_duration_ms=1),
        ReviewObservation(case_id="bug", mode="live_provider", findings=(_finding(),), total_review_duration_ms=2, provider_attempts=1),
    ])
    assert [report.mode for report in reports] == ["deterministic", "live_provider"]


def test_observation_rejects_inconsistent_abstention_and_provider_activity() -> None:
    with pytest.raises(ValidationError, match="abstained"):
        ReviewObservation(case_id="bug", mode="deterministic", findings=(_finding(),), abstained=True, total_review_duration_ms=1)
    with pytest.raises(ValidationError, match="provider activity"):
        ReviewObservation(case_id="bug", mode="deterministic", total_review_duration_ms=1, provider_attempts=1)


def test_repair_metrics_report_each_gate_and_unavailable_checks() -> None:
    suite = RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(_case(),))
    observation = RepairObservation(
        case_id="bug",
        mode="live_provider",
        proposal_eligible=True,
        proposal_generated=True,
        structured_output_accepted=True,
        patch_parse_succeeded=True,
        target_files=("src/app.py",),
        changed_lines_in_scope=2,
        changed_lines_total=2,
        secret_safe=True,
        policy_safe=True,
        syntax="passed",
        formatter="unavailable",
        linter="passed",
        type_check="unavailable",
        targeted_tests="passed",
        full_suite="not_run",
        regression_detected=False,
        patch_accepted=True,
        validated_in_sandbox=True,
        apply_attempted=True,
        applied_after_confirmation=True,
        repair_attempts=2,
        latency_ms=25,
        cost=0.04,
        lifecycle_statuses=(
            "proposal_generated", "patch_policy_accepted", "syntax_passed",
            "type_check_unavailable", "targeted_tests_passed", "full_suite_not_run",
            "validated_in_sandbox", "applied_after_confirmation",
        ),
    )
    report = score_repair_benchmark(suite, [observation])[0]
    assert _metric(report, "proposal_eligibility_correctness").value == 1
    assert _metric(report, "target_file_scope_accuracy").value == 1
    assert _metric(report, "changed_line_scope").value == 1
    assert _metric(report, "formatter_pass_rate").value is None
    assert _metric(report, "type_check_pass_rate").value is None
    assert _metric(report, "targeted_test_pass_rate").value == 1
    assert _metric(report, "full_suite_pass_rate").value is None
    assert _metric(report, "cost_per_proposal").value == pytest.approx(0.04)


def test_repair_observation_rejects_unearned_parse_and_apply_states() -> None:
    base = {
        "case_id": "bug", "mode": "deterministic", "proposal_eligible": True,
        "proposal_generated": False, "structured_output_accepted": False,
        "patch_parse_succeeded": True, "target_files": ["src/app.py"],
        "secret_safe": True, "policy_safe": True, "syntax": "not_run",
        "formatter": "not_run", "linter": "not_run", "type_check": "not_run",
        "targeted_tests": "not_run", "full_suite": "not_run",
    }
    with pytest.raises(ValidationError, match="patch parse success"):
        RepairObservation.model_validate(base)

    base.update({
        "proposal_generated": True, "structured_output_accepted": True,
        "patch_accepted": True, "apply_attempted": False,
        "applied_after_confirmation": True,
    })
    with pytest.raises(ValidationError, match="application requires an apply attempt"):
        RepairObservation.model_validate(base)


def test_repair_statuses_cannot_overstate_observed_outcomes() -> None:
    data = {
        "case_id": "bug", "mode": "deterministic", "proposal_eligible": True,
        "proposal_generated": True, "structured_output_accepted": True,
        "patch_parse_succeeded": True, "secret_safe": True, "policy_safe": True,
        "syntax": "failed", "formatter": "not_run", "linter": "not_run",
        "type_check": "not_run", "targeted_tests": "not_run", "full_suite": "not_run",
        "lifecycle_statuses": ["syntax_passed"],
    }
    with pytest.raises(ValidationError, match="syntax_passed"):
        RepairObservation.model_validate(data)


def test_fixture_loader_is_offline_and_rejects_escape(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "bug" / "before" / "src"
    fixture.mkdir(parents=True)
    (fixture / "app.py").write_text("value = 1\n", encoding="utf-8")
    suite_path = tmp_path / "suite.json"
    suite = RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(_case(),))
    suite_path.write_text(suite.model_dump_json(indent=2), encoding="utf-8")
    assert load_benchmark_suite(suite_path).cases[0].case_id == "bug"

    data = suite.model_dump(mode="json")
    data["cases"][0]["fixture_ref"] = "../outside"
    suite_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="safe repository-relative"):
        load_benchmark_suite(suite_path)


def test_file_runner_output_is_stable_and_contains_both_report_types(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "bug" / "after" / "src"
    fixture.mkdir(parents=True)
    (fixture / "app.py").write_text("value = 2\n", encoding="utf-8")
    suite = RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(_case(),))
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(suite.model_dump_json(indent=2), encoding="utf-8")
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps([ReviewObservation(case_id="bug", mode="deterministic", findings=(_finding(),), total_review_duration_ms=0).model_dump(mode="json")]), encoding="utf-8")
    repairs = tmp_path / "repairs.json"
    repairs.write_text(json.dumps([RepairObservation(
        case_id="bug", mode="deterministic", proposal_eligible=True, proposal_generated=False,
        structured_output_accepted=False, patch_parse_succeeded=False, secret_safe=True,
        policy_safe=True, syntax="not_run", formatter="not_run", linter="not_run",
        type_check="not_run", targeted_tests="not_run", full_suite="not_run",
    ).model_dump(mode="json")]), encoding="utf-8")
    first = run_benchmark_files(suite_path, review_observations_path=reviews, repair_observations_path=repairs)
    second = run_benchmark_files(suite_path, review_observations_path=reviews, repair_observations_path=repairs)
    assert first == second
    assert len(first["review_reports"]) == 1
    assert len(first["repair_reports"]) == 1


def test_checked_in_suite_covers_categories_and_bundle_revisions(tmp_path: Path) -> None:
    root = Path("eval/cases/real-repositories/v1")
    suite = load_benchmark_suite(root / "suite.json")
    assert {case.benchmark_category for case in suite.cases} == {
        "clean_changes", "real_bugs", "security_issues", "sensitive_data_exposure",
        "authorization_authentication", "type_nullability", "test_regressions",
        "performance_regressions", "ambiguous_changes", "correct_abstention",
        "ai_generated_defects", "multi_language_changes",
    }
    checkout = tmp_path / "mirror"
    bundle = root / "fixtures" / "sanitized-offline-mirror" / "mirror.bundle"
    subprocess.run(["git", "clone", "-q", str(bundle.resolve()), str(checkout)], check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    base = subprocess.check_output(["git", "rev-parse", "HEAD^"], cwd=checkout, text=True).strip()
    assert {case.head_sha for case in suite.cases} == {head}
    assert {case.base_sha for case in suite.cases} == {base}


def test_benchmark_cli_runs_checked_in_offline_contract() -> None:
    root = Path("eval/cases/real-repositories/v1")
    result = CliRunner().invoke(app, [
        "benchmark", "--suite", str(root / "suite.json"),
        "--review-observations", str(root / "example-review-observations.json"),
        "--repair-observations", str(root / "example-repair-observations.json"),
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["benchmark_version"] == "real-repositories-v1"
    assert payload["suite_case_count"] == 12
    assert len(payload["review_reports"]) == 1
    assert len(payload["repair_reports"]) == 1


def test_report_writer_does_not_replace_benchmark_inputs(tmp_path: Path) -> None:
    suite = tmp_path / "suite.json"
    suite.write_text("labels", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot replace"):
        write_benchmark_report({"metrics": []}, suite, protected_inputs=(suite,))
    assert suite.read_text(encoding="utf-8") == "labels"


def test_benchmark_cli_requires_observations() -> None:
    suite = "eval/cases/real-repositories/v1/suite.json"
    result = CliRunner().invoke(app, ["benchmark", "--suite", suite])
    assert result.exit_code == 2
    assert "at least one observation or comparison file is required" in result.output
