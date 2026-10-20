"""Repair benchmark observations, lifecycle statuses, and explicit metrics."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.orchestrator.repair_models import repair_payload_is_safe

from .repository import (
    MetricResult,
    RepositoryBenchmarkCase,
    RepositoryBenchmarkSuite,
    _metric,
    _MetricPopulation,
)

CheckStatus = Literal["passed", "failed", "not_run", "unavailable", "blocked", "unsupported"]
RepairProviderRunMode = Literal["offline", "live", "fallback", "dry_run", "unavailable", "blocked", "not_run"]
AutofixCategory = Literal["autofix_supported", "requires_more_context", "review_only", "default_abstention", "unsupported"]
AutofixResult = Literal["generated", "rejected_by_policy", "validated", "tests_passed", "tests_failed", "not_run", "unavailable", "blocked", "applied_after_confirmation", "reverted"]
RepairEvidenceKind = Literal["contract_fixture", "measured", "unspecified"]
_REPAIR_DIMENSIONS = ("semantic_correctness", "minimality", "maintainability", "explanation_quality")
_CHECK_FIELDS = ("syntax", "formatter", "linter", "type_check", "targeted_tests", "full_suite", *_REPAIR_DIMENSIONS)
RepairLifecycleStatus = Literal[
    "proposal_generated",
    "patch_policy_accepted",
    "syntax_passed",
    "type_check_unavailable",
    "targeted_tests_passed",
    "full_suite_not_run",
    "validated_in_sandbox",
    "applied_after_confirmation",
    "reverted_by_user",
]


class RepairObservation(BaseModel):
    """Measured outcomes for one proposal; no intermediate state implies success."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    mode: Literal["deterministic", "live_provider"]
    proposal_eligible: bool
    proposal_generated: bool
    structured_output_accepted: bool
    patch_parse_succeeded: bool
    target_files: tuple[str, ...] = ()
    changed_lines_in_scope: int = Field(default=0, ge=0)
    changed_lines_total: int = Field(default=0, ge=0)
    secret_safe: bool
    policy_safe: bool
    syntax: CheckStatus
    formatter: CheckStatus
    linter: CheckStatus
    type_check: CheckStatus
    targeted_tests: CheckStatus
    full_suite: CheckStatus
    regression_detected: bool | None = None
    patch_accepted: bool = False
    patch_rejected: bool = False
    validated_in_sandbox: bool = False
    apply_attempted: bool = False
    applied_after_confirmation: bool = False
    reverted: bool = False
    apply_conflict: bool = False
    repair_attempts: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    cost: float | None = Field(default=None, ge=0)
    lifecycle_statuses: tuple[RepairLifecycleStatus, ...] = ()
    regenerated: bool = False
    provider_run_mode: RepairProviderRunMode | None = None
    evidence_kind: RepairEvidenceKind = "unspecified"
    evidence_source: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/#-]*$")
    autofix_category: AutofixCategory = "review_only"
    manual_correction: bool | None = None
    semantic_correctness: CheckStatus = "not_run"
    minimality: CheckStatus = "not_run"
    maintainability: CheckStatus = "not_run"
    explanation_quality: CheckStatus = "not_run"

    @property
    def effective_provider_run_mode(self) -> RepairProviderRunMode:
        return self.provider_run_mode or ("offline" if self.mode == "deterministic" else "live")

    @model_validator(mode="after")
    def validate_state(self) -> RepairObservation:
        if self.patch_accepted and self.patch_rejected:
            raise ValueError("a patch cannot be both accepted and rejected")
        if self.structured_output_accepted and not self.proposal_generated:
            raise ValueError("structured output acceptance requires a generated proposal")
        if (self.patch_accepted or self.patch_rejected) and not self.proposal_generated:
            raise ValueError("patch acceptance decisions require a generated proposal")
        if self.patch_parse_succeeded and not self.structured_output_accepted:
            raise ValueError("patch parse success requires accepted structured output")
        if self.patch_parse_succeeded and not self.target_files:
            raise ValueError("a parsed patch requires target_files")
        checks = (self.syntax, self.formatter, self.linter, self.type_check, self.targeted_tests, self.full_suite)
        if any(check == "passed" for check in checks) and not self.patch_parse_succeeded:
            raise ValueError("passing validation checks require a parsed patch")
        if self.validated_in_sandbox and not self.patch_parse_succeeded:
            raise ValueError("sandbox validation requires a parsed patch")
        if self.apply_attempted and not self.patch_accepted:
            raise ValueError("apply attempts require an accepted patch")
        if self.changed_lines_in_scope > self.changed_lines_total:
            raise ValueError("changed_lines_in_scope cannot exceed changed_lines_total")
        if self.apply_conflict and not self.apply_attempted:
            raise ValueError("apply conflict requires an apply attempt")
        if self.applied_after_confirmation and not self.apply_attempted:
            raise ValueError("application requires an apply attempt")
        if self.reverted and not self.applied_after_confirmation:
            raise ValueError("only an applied patch can be reverted")
        if "proposal_generated" in self.lifecycle_statuses and not self.proposal_generated:
            raise ValueError("proposal_generated status requires a generated proposal")
        if "patch_policy_accepted" in self.lifecycle_statuses and not self.policy_safe:
            raise ValueError("patch_policy_accepted status requires policy safety")
        if "syntax_passed" in self.lifecycle_statuses and self.syntax != "passed":
            raise ValueError("syntax_passed status requires a passing syntax check")
        if "type_check_unavailable" in self.lifecycle_statuses and self.type_check != "unavailable":
            raise ValueError("type_check_unavailable status requires an unavailable type check")
        if "targeted_tests_passed" in self.lifecycle_statuses and self.targeted_tests != "passed":
            raise ValueError("targeted_tests_passed status requires passing targeted tests")
        if "full_suite_not_run" in self.lifecycle_statuses and self.full_suite != "not_run":
            raise ValueError("full_suite_not_run status requires a not-run full suite")
        if "validated_in_sandbox" in self.lifecycle_statuses and not self.validated_in_sandbox:
            raise ValueError("validated_in_sandbox status requires sandbox validation")
        if "applied_after_confirmation" in self.lifecycle_statuses and not self.applied_after_confirmation:
            raise ValueError("applied_after_confirmation requires confirmed application")
        if "reverted_by_user" in self.lifecycle_statuses and not self.reverted:
            raise ValueError("reverted_by_user requires a reverted patch")
        if not repair_payload_is_safe(self.model_dump(mode="json")):
            raise ValueError("repair observation failed the existing secret audit")
        if self.effective_provider_run_mode in {"unavailable", "blocked", "not_run"} and self.proposal_generated:
            raise ValueError("unexecuted provider modes cannot claim generation")
        return self


class RepairBenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_version: str
    mode: Literal["deterministic", "live_provider"]
    observation_count: int
    metrics: tuple[MetricResult, ...]


def _status_metric(
    name: str,
    field: str,
    rows: list[tuple[RepositoryBenchmarkCase, RepairObservation]],
    population: _MetricPopulation,
) -> MetricResult:
    available = [
        observation
        for _, observation in rows
        if getattr(observation, field) in {"passed", "failed"}
    ]
    passed = sum(getattr(observation, field) == "passed" for observation in available)
    return _metric(
        name,
        passed,
        len(available),
        **population,
        exclusions=(f"{field} not_run, unavailable, blocked, or unsupported",),
    )


def score_repair_benchmark(
    suite: RepositoryBenchmarkSuite,
    observations: list[RepairObservation] | tuple[RepairObservation, ...],
) -> tuple[RepairBenchmarkReport, ...]:
    """Report each repair gate independently instead of claiming aggregate correctness."""
    cases = {case.case_id: case for case in suite.cases}
    grouped: dict[str, list[tuple[RepositoryBenchmarkCase, RepairObservation]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for observation in observations:
        case = cases.get(observation.case_id)
        if case is None:
            raise ValueError(f"repair observation references unknown case: {observation.case_id}")
        key = (observation.case_id, observation.mode)
        if key in seen:
            raise ValueError(f"duplicate repair observation: {observation.case_id}/{observation.mode}")
        seen.add(key)
        grouped[observation.mode].append((case, observation))

    reports: list[RepairBenchmarkReport] = []
    for mode in ("deterministic", "live_provider"):
        rows = grouped.get(mode, [])
        if not rows:
            continue
        total = len(rows)
        generated = [observation for _, observation in rows if observation.proposal_generated]
        parsed = [observation for observation in generated if observation.patch_parse_succeeded]
        expected_eligible = [case.expected_proposal_eligible for case, _ in rows]
        eligibility_correct = sum(expected == observation.proposal_eligible for expected, (_, observation) in zip(expected_eligible, rows))
        scope_correct = sum(
            bool(observation.target_files)
            and len(observation.target_files) <= case.expected_patch_scope.max_files
            and set(observation.target_files).issubset(set(case.expected_patch_scope.allowed_files))
            and not set(observation.target_files).intersection(case.expected_patch_scope.forbidden_files)
            for case, observation in rows
            if observation.patch_parse_succeeded
        )
        changed_scope_numerator = sum(observation.changed_lines_in_scope for observation in parsed)
        changed_scope_denominator = sum(observation.changed_lines_total for observation in parsed)
        common: _MetricPopulation = {
            "version": suite.benchmark_version,
            "positive": f"{sum(expected_eligible)} cases labelled fixable",
            "negative": f"{total - sum(expected_eligible)} cases not labelled fixable",
            "ambiguous": f"{sum(case.population == 'ambiguous' for case, _ in rows)} ambiguous cases",
        }

        metrics = [
            _metric("proposal_eligibility_correctness", eligibility_correct, total, **common),
            _metric("proposal_generation_success", len(generated), sum(item.proposal_eligible for _, item in rows), **common, exclusions=("proposal not eligible",)),
            _metric("regeneration_rate", sum(item.regenerated for _, item in rows), len(generated), **common, exclusions=("no proposal generated",)),
            _metric("structured_output_acceptance", sum(item.structured_output_accepted for item in generated), len(generated), **common, exclusions=("no proposal generated",)),
            _metric("patch_parse_success", sum(item.patch_parse_succeeded for item in generated), len(generated), **common, exclusions=("no proposal generated",)),
            _metric("target_file_scope_accuracy", scope_correct, len(parsed), **common, exclusions=("patch did not parse",)),
            _metric("changed_line_scope", changed_scope_numerator, changed_scope_denominator, **common, exclusions=("patch did not parse or changed no lines",)),
            _metric("secret_safety", sum(item.secret_safe for item in generated), len(generated), **common, exclusions=("no proposal generated",)),
            _metric("policy_safety", sum(item.policy_safe for item in generated), len(generated), **common, exclusions=("no proposal generated",)),
            _status_metric("syntax_pass_rate", "syntax", rows, common),
            _status_metric("formatter_pass_rate", "formatter", rows, common),
            _status_metric("linter_pass_rate", "linter", rows, common),
            _status_metric("type_check_pass_rate", "type_check", rows, common),
            _status_metric("targeted_test_pass_rate", "targeted_tests", rows, common),
            _status_metric("full_suite_pass_rate", "full_suite", rows, common),
        ]
        regression_rows = [item for _, item in rows if item.regression_detected is not None]
        metrics.extend([
            _metric("regression_rate", sum(bool(item.regression_detected) for item in regression_rows), len(regression_rows), **common, exclusions=("regression outcome unavailable",)),
            _metric("accepted_patch_rate", sum(item.patch_accepted for _, item in rows), total, **common),
            _metric("rejected_patch_rate", sum(item.patch_rejected for _, item in rows), total, **common),
            _metric("revert_rate", sum(item.reverted for _, item in rows), sum(item.applied_after_confirmation for _, item in rows), **common, exclusions=("patch was not applied",)),
            _metric("apply_conflict_rate", sum(item.apply_conflict for _, item in rows), sum(item.apply_attempted for _, item in rows), **common, exclusions=("apply was not attempted",)),
            _metric("average_repair_attempts", sum(item.repair_attempts for _, item in rows), total, **common, unit="count"),
            _metric("repair_latency", sum(item.latency_ms for _, item in rows), total, **common, unit="milliseconds"),
        ])
        costs = [item.cost for _, item in rows if item.proposal_generated and item.cost is not None]
        metrics.append(_metric("cost_per_proposal", sum(costs) if costs else None, len(costs), **common, exclusions=("cost unavailable or no proposal generated",), unit="cost"))
        reports.append(RepairBenchmarkReport(
            benchmark_version=suite.benchmark_version,
            mode=mode,
            observation_count=total,
            metrics=tuple(metrics),
        ))
    return tuple(reports)


__all__ = ["CheckStatus", "RepairBenchmarkReport", "RepairLifecycleStatus", "RepairObservation", "score_repair_benchmark"]
