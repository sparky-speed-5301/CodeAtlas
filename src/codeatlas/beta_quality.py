"""Bounded beta measurements projected from the existing repair benchmark contract."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.benchmark.repair_metrics import AutofixCategory, RepairObservation
from codeatlas.benchmark.repository import RepositoryBenchmarkSuite
from codeatlas.beta_execution import MODES, EvidenceRate, Mode, rate


class PresentationImprovement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    surface: Literal["cli", "api", "github", "markdown", "json", "vscode", "fix_proposal"]
    issue: Literal["acceptance_conflation", "missing_abstention", "correctness_overclaim", "unavailable"]
    responsible_layer: Literal["beta_session", "repair_benchmark", "presentation"]
    regression_fixture: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/#-]*$")
    evidence_basis: Literal["measured", "contract_only", "unavailable"]
    before_errors: int = Field(ge=0, le=1000)
    after_errors: int = Field(ge=0, le=1000)


class PresentationAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    audit_version: Literal["beta-presentation-v1"] = "beta-presentation-v1"
    improvements: tuple[PresentationImprovement, ...] = Field(min_length=7, max_length=7)


def contract_presentation_audit() -> PresentationAudit:
    """Document the inspected surfaces without claiming external beta evidence."""
    entries = []
    for surface in ("cli", "api", "github", "markdown", "json", "vscode", "fix_proposal"):
        changed = surface in {"cli", "api", "json", "fix_proposal"}
        entries.append(PresentationImprovement(
            surface=surface,
            issue="acceptance_conflation" if changed else "unavailable",
            responsible_layer="beta_session" if changed else "presentation",
            regression_fixture="tests/unit/test_beta_quality.py::test_machine_report_is_not_operator_acceptance",
            evidence_basis="contract_only" if changed else "unavailable",
            before_errors=1 if changed else 0, after_errors=0,
        ))
    return PresentationAudit(improvements=tuple(entries))


class AutofixMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    language: Literal["python", "javascript", "typescript", "go", "java", "rust"]
    finding_category: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    classification: AutofixCategory
    provider_mode: Mode
    evidence_basis: Literal["measured", "contract_only", "unavailable"]
    count: int = Field(ge=1, le=1000)
    metrics: dict[str, EvidenceRate] = Field(max_length=20)
    statuses: dict[Literal["generated", "rejected_by_policy", "validated", "tests_passed", "tests_failed", "not_run", "unavailable", "blocked", "applied_after_confirmation", "reverted"], int] = Field(max_length=10)
    correctness_established: Literal[False] = False


class AutofixBetaReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    report_version: Literal["beta-autofix-v1"] = "beta-autofix-v1"
    measurements: tuple[AutofixMeasurement, ...] = Field(default=(), max_length=100)
    automatic_actions_enabled: Literal[False] = False
    correctness_established: Literal[False] = False


def measure_autofix(suite: RepositoryBenchmarkSuite, observations: tuple[RepairObservation, ...]) -> AutofixBetaReport:
    """Separate each observed gate, language, category, and provider execution mode."""
    if len(observations) > 1000:
        raise ValueError("beta autofix observation bound exceeded")
    cases = {case.case_id: case for case in suite.cases}
    grouped: dict[tuple[str, str, str, str, str], list[RepairObservation]] = {}
    seen = set()
    for original in observations:
        item = RepairObservation.model_validate(original.model_dump())
        if item.case_id not in cases:
            raise ValueError("unknown beta autofix case")
        selected_mode = item.effective_provider_run_mode
        if selected_mode not in MODES:
            raise ValueError("dry-run observations require an explicit beta execution mode")
        identity = (item.case_id, selected_mode)
        if identity in seen:
            raise ValueError("duplicate beta autofix observation")
        seen.add(identity)
        case = cases[item.case_id]
        basis = "measured" if item.evidence_kind == "measured" else "contract_only" if item.evidence_kind == "contract_fixture" else "unavailable"
        for language in case.language:
            for category in case.expected_finding_categories or (case.benchmark_category,):
                key = (language, category, item.autofix_category, selected_mode, basis)
                grouped.setdefault(key, []).append(item)
    measurements = []
    for (language, category, classification, mode, basis), rows in sorted(grouped.items()):
        metrics: dict[str, EvidenceRate] = {}
        generated = [item for item in rows if item.proposal_generated]
        parsed = [item for item in rows if item.patch_parse_succeeded]
        def add(name, numerator, denominator, *, target=metrics, evidence=basis):
            target[name] = rate(numerator, denominator, evidence) if evidence != "unavailable" else EvidenceRate(status="unavailable")
        add("eligibility", sum(item.proposal_eligible for item in rows), len(rows))
        add("generation", len(generated), sum(item.proposal_eligible for item in rows))
        add("parsing", len(parsed), len(generated))
        add("policy_acceptance", sum(item.policy_safe for item in generated), len(generated))
        add("scope", sum(item.changed_lines_in_scope for item in parsed), sum(item.changed_lines_total for item in parsed))
        add("secret_safety", sum(item.secret_safe for item in generated), len(generated))
        for field in ("syntax", "formatter", "linter", "type_check", "targeted_tests", "full_suite"):
            available = [item for item in rows if getattr(item, field) in {"passed", "failed"}]
            add(field, sum(getattr(item, field) == "passed" for item in available), len(available))
        decided = [item for item in rows if item.patch_accepted or item.patch_rejected]
        add("approval", sum(item.patch_accepted for item in decided), len(decided))
        add("rejection", sum(item.patch_rejected for item in decided), len(decided))
        add("regeneration", sum(item.regenerated for item in generated), len(generated))
        add("conflict", sum(item.apply_conflict for item in rows), sum(item.apply_attempted for item in rows))
        add("revert", sum(item.reverted for item in rows), sum(item.applied_after_confirmation for item in rows))
        for field in ("manual_correction", "regression_detected"):
            available = [item for item in rows if getattr(item, field) is not None]
            add(field, sum(bool(getattr(item, field)) for item in available), len(available))
        counts: Counter[str] = Counter()
        for item in rows:
            if item.proposal_generated:
                counts["generated"] += 1
                if not item.policy_safe:
                    counts["rejected_by_policy"] += 1
            if item.validated_in_sandbox:
                counts["validated"] += 1
            if item.targeted_tests == "passed":
                counts["tests_passed"] += 1
            elif item.targeted_tests == "failed":
                counts["tests_failed"] += 1
            else:
                counts[item.targeted_tests if item.targeted_tests in {"not_run", "unavailable", "blocked"} else "unavailable"] += 1
            if item.applied_after_confirmation:
                counts["applied_after_confirmation"] += 1
            if item.reverted:
                counts["reverted"] += 1
        measurements.append(AutofixMeasurement.model_validate({"language": language, "finding_category": category, "classification": classification, "provider_mode": mode, "evidence_basis": basis, "count": len(rows), "metrics": metrics, "statuses": dict(counts)}))
    return AutofixBetaReport(measurements=tuple(measurements))
