"""Contract-only regression evidence; gate success never implies correctness."""

from types import SimpleNamespace

import pytest

from codeatlas.benchmark.repair_metrics import RepairObservation
from codeatlas.benchmark.repository import RepositoryBenchmarkSuite
from codeatlas.beta_quality import contract_presentation_audit, measure_autofix
from codeatlas.beta_session import BetaSessionRunner
from tests.unit.test_real_repository_benchmark import VERSION, _case


def observation(**updates):
    data = {"case_id": "bug", "mode": "deterministic", "proposal_eligible": True,
            "proposal_generated": True, "structured_output_accepted": True, "patch_parse_succeeded": True,
            "target_files": ("src/app.py",), "changed_lines_in_scope": 2, "changed_lines_total": 2,
            "secret_safe": True, "policy_safe": True, "syntax": "passed", "formatter": "not_run",
            "linter": "unavailable", "type_check": "unavailable", "targeted_tests": "passed", "full_suite": "not_run",
            "evidence_kind": "contract_fixture", "autofix_category": "autofix_supported"}
    data.update(updates)
    return RepairObservation.model_validate(data)


def suite():
    return RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=(_case(),))


def test_presentation_audit_labels_unmeasured_surfaces():
    audit = contract_presentation_audit()
    assert len(audit.improvements) == 7
    assert next(item for item in audit.improvements if item.surface == "cli").before_errors == 1
    assert next(item for item in audit.improvements if item.surface == "vscode").evidence_basis == "unavailable"


def test_machine_report_is_not_operator_acceptance():
    manifest = SimpleNamespace(findings=[{"severity": "high", "quality_decision": "report"},
                                         {"severity": "low", "quality_decision": "suppress_duplicate"},
                                         {"severity": "medium", "quality_decision": "review_only"}])
    result = BetaSessionRunner._review_projection(manifest)
    assert result["finding_count"] == 3
    assert result["accepted_count"] == result["rejected_count"] == result["abstained_count"] == 0


def test_explicit_feedback_and_abstention():
    manifest = SimpleNamespace(findings=[{"feedback": "accepted"}, {"feedback": "rejected"}, {"quality_decision": "abstain"}])
    result = BetaSessionRunner._review_projection(manifest)
    assert result["accepted_count"] == result["rejected_count"] == result["abstained_count"] == 1


def test_autofix_dimensions_are_separate():
    report = measure_autofix(suite(), (observation(),))
    item = report.measurements[0]
    assert item.language == "python"
    assert item.finding_category == "CORRECTNESS"
    assert item.provider_mode == "offline"
    assert item.evidence_basis == "contract_only"
    assert item.metrics["syntax"].value == 1
    assert item.metrics["full_suite"].value is None
    assert item.metrics["manual_correction"].value is None
    assert item.metrics["approval"].value is None
    assert item.correctness_established is False
    assert item.statuses["tests_passed"] == 1


def test_fallback_is_not_live_and_duplicates_fail():
    item = observation(provider_run_mode="fallback")
    assert measure_autofix(suite(), (item,)).measurements[0].provider_mode == "fallback"
    with pytest.raises(ValueError, match="duplicate"):
        measure_autofix(suite(), (item, item))


def test_unexecuted_provider_cannot_claim_generation():
    with pytest.raises(ValueError):
        observation(provider_run_mode="unavailable")
