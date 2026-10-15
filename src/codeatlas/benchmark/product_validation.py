"""Versioned product-validation metrics and human-review protocol contracts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .repository import (
    BenchmarkCategory,
    PatchScopeExpectation,
    Severity,
    TestCommandPolicy,
)

MetricGroup = Literal["review_usefulness", "autofix_usefulness", "reliability", "trust_and_safety"]
MetricUnit = Literal["rate", "milliseconds", "minutes", "count"]
IntervalMethod = Literal["wilson_95", "bootstrap_95", "none"]
ReviewerOutcome = Literal[
    "accepted",
    "rejected",
    "needs_more_context",
    "needs_regeneration",
    "abstained",
    "not_evaluated",
]
ProtocolResult = Literal["yes", "no", "not_evaluated"]
ExpectedAbstention = Literal["required", "allowed", "forbidden"]
ExpectedFixability = Literal["not_fixable", "review_required", "fixable", "ambiguous"]
ExpectedAnchorDisposition = Literal["report", "support_abstention"]

REQUIRED_VALIDATION_LANGUAGES = frozenset({"python", "javascript_typescript", "go", "java", "rust"})
REQUIRED_VALIDATION_CATEGORIES = frozenset({
    "clean_changes",
    "real_bugs",
    "security_issues",
    "sensitive_data_exposure",
    "authorization_authentication",
    "type_nullability",
    "performance_regressions",
    "ambiguous_changes",
    "correct_abstention",
    "ai_generated_defects",
})


def _validate_relative_path(value: str, label: str) -> None:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a safe repository-relative path")


def _is_public_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


class ProductMetricDefinition(BaseModel):
    """One metric definition; the catalog never combines unrelated outcomes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    group: MetricGroup
    definition: str = Field(min_length=10, max_length=1_000)
    numerator: str = Field(min_length=3, max_length=500)
    denominator: str = Field(min_length=3, max_length=500)
    population: str = Field(min_length=3, max_length=500)
    exclusions: tuple[str, ...] = ()
    benchmark_version: str = Field(min_length=1, max_length=64)
    confidence_interval_method: IntervalMethod
    unit: MetricUnit = "rate"


class ProductMetricCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: str = Field(min_length=1, max_length=64)
    benchmark_version: str = Field(min_length=1, max_length=64)
    metrics: tuple[ProductMetricDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> ProductMetricCatalog:
        names = [metric.name for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("metric names must be unique")
        if any(metric.benchmark_version != self.benchmark_version for metric in self.metrics):
            raise ValueError("metric benchmark versions must match the catalog")
        return self


class ControlledExpectedAnchor(BaseModel):
    """An immutable HEAD anchor for a finding or a justified abstention."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    file: str = Field(min_length=1, max_length=512)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    category: str = Field(min_length=1, max_length=128)
    severity: Severity
    disposition: ExpectedAnchorDisposition
    description: str = Field(min_length=10, max_length=1_000)

    @model_validator(mode="after")
    def validate_anchor(self) -> ControlledExpectedAnchor:
        _validate_relative_path(self.file, "anchor file")
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ControlledRepositoryCase(BaseModel):
    """Metadata-only label for one immutable public repository comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    repository_identity: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    repository_url: str = Field(min_length=12, max_length=2_000)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    language: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    changed_files: tuple[str, ...] = Field(min_length=1, max_length=100)
    validation_categories: tuple[BenchmarkCategory, ...] = Field(min_length=1)
    expected_findings: tuple[str, ...]
    expected_anchors: tuple[ControlledExpectedAnchor, ...]
    expected_severity: tuple[Severity, ...]
    expected_abstention: ExpectedAbstention
    expected_fixability: ExpectedFixability
    expected_patch_scope: PatchScopeExpectation
    test_policy: TestCommandPolicy
    license_spdx: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+() -]{0,127}$")
    provenance_urls: tuple[str, ...] = Field(min_length=1, max_length=10)
    provenance: str = Field(min_length=10, max_length=1_000)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=20)
    source_persistence: Literal["metadata_only"] = "metadata_only"
    provider_material_collected: Literal[False] = False
    benchmark_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_case(self) -> ControlledRepositoryCase:
        if not _is_public_https_url(self.repository_url):
            raise ValueError("repository_url must be a public HTTPS URL without credentials or parameters")
        if self.base_sha == self.head_sha:
            raise ValueError("base_sha and head_sha must differ")
        if len(set(self.changed_files)) != len(self.changed_files):
            raise ValueError("changed_files must be unique")
        for path in self.changed_files:
            _validate_relative_path(path, "changed file")
        if len(set(self.validation_categories)) != len(self.validation_categories):
            raise ValueError("validation_categories must be unique")
        if len(set(self.expected_findings)) != len(self.expected_findings):
            raise ValueError("expected_findings must be unique")
        if any(len(finding) < 10 or len(finding) > 1_000 for finding in self.expected_findings):
            raise ValueError("expected_findings must be bounded descriptions")
        anchor_ids = [anchor.anchor_id for anchor in self.expected_anchors]
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("expected anchor IDs must be unique")
        if any(anchor.file not in set(self.changed_files) for anchor in self.expected_anchors):
            raise ValueError("expected anchors must refer to changed_files")
        if tuple(anchor.severity for anchor in self.expected_anchors) != self.expected_severity:
            raise ValueError("expected_severity must align with expected_anchors")
        report_anchors = tuple(anchor for anchor in self.expected_anchors if anchor.disposition == "report")
        if len(report_anchors) != len(self.expected_findings):
            raise ValueError("expected_findings must align with report anchors")
        if self.expected_abstention == "required" and report_anchors:
            raise ValueError("required abstention cannot declare report anchors")
        if self.expected_abstention == "forbidden" and any(
            anchor.disposition == "support_abstention" for anchor in self.expected_anchors
        ):
            raise ValueError("forbidden abstention cannot declare abstention-support anchors")
        if self.expected_fixability == "not_fixable" and self.expected_patch_scope.allowed_files:
            raise ValueError("not_fixable cases cannot allow patch files")
        if len(set(self.provenance_urls)) != len(self.provenance_urls):
            raise ValueError("provenance URLs must be unique")
        for url in self.provenance_urls:
            if not _is_public_https_url(url):
                raise ValueError("provenance URLs must be public HTTPS URLs without credentials or parameters")
        if any(len(limitation) < 10 or len(limitation) > 1_000 for limitation in self.limitations):
            raise ValueError("limitations must be bounded descriptions")
        return self


class ControlledRepositorySuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_version: str = Field(min_length=1, max_length=64)
    generalization_limit: str = Field(min_length=20, max_length=1_000)
    cases: tuple[ControlledRepositoryCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_suite(self) -> ControlledRepositorySuite:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case IDs must be unique")
        if any(case.benchmark_version != self.benchmark_version for case in self.cases):
            raise ValueError("case benchmark versions must match the suite")
        languages = {case.language for case in self.cases}
        missing_languages = REQUIRED_VALIDATION_LANGUAGES - languages
        if missing_languages:
            raise ValueError(f"missing required languages: {sorted(missing_languages)}")
        categories = {category for case in self.cases for category in case.validation_categories}
        missing_categories = REQUIRED_VALIDATION_CATEGORIES - categories
        if missing_categories:
            raise ValueError(f"missing required validation categories: {sorted(missing_categories)}")
        return self


class HumanReviewEvaluation(BaseModel):
    """Bounded reviewer outcome without comments, prompts, transcripts, or source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: str = Field(min_length=1, max_length=64)
    benchmark_version: str = Field(min_length=1, max_length=64)
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    reviewer_id_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    completed_at: datetime
    understand_finding: ProtocolResult
    identify_affected_code: ProtocolResult
    understand_evidence: ProtocolResult
    decide_finding_validity: ProtocolResult
    understand_proposed_fix: ProtocolResult
    choose_fix_action: ProtocolResult
    validate_result: ProtocolResult
    recover_safely: ProtocolResult
    finding_outcome: ReviewerOutcome
    fix_outcome: ReviewerOutcome
    time_to_understand_finding_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    time_to_resolve_finding_ms: int | None = Field(default=None, ge=0, le=604_800_000)
    time_to_first_useful_finding_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    time_to_accepted_fix_ms: int | None = Field(default=None, ge=0, le=604_800_000)
    manual_correction_baseline_ms: int | None = Field(default=None, ge=0, le=604_800_000)
    reason_codes: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_evaluation(self) -> HumanReviewEvaluation:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        if any(not code or len(code) > 64 or not code.replace("_", "").isalnum() for code in self.reason_codes):
            raise ValueError("reason_codes must be bounded identifiers")
        if self.understand_finding != "yes" and self.time_to_understand_finding_ms is not None:
            raise ValueError("understanding time requires an understood finding")
        if self.finding_outcome == "not_evaluated" and self.time_to_resolve_finding_ms is not None:
            raise ValueError("an unevaluated finding cannot report resolution time")
        if self.finding_outcome != "accepted" and self.time_to_first_useful_finding_ms is not None:
            raise ValueError("time to first useful finding requires an accepted finding")
        if self.fix_outcome != "accepted" and self.time_to_accepted_fix_ms is not None:
            raise ValueError("time_to_accepted_fix_ms requires an accepted fix")
        if self.manual_correction_baseline_ms is not None and self.fix_outcome != "accepted":
            raise ValueError("manual correction comparison requires an accepted fix")
        return self


def load_metric_catalog(path: str | Path) -> ProductMetricCatalog:
    return ProductMetricCatalog.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_controlled_repository_suite(path: str | Path) -> ControlledRepositorySuite:
    return ControlledRepositorySuite.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_human_evaluations(path: str | Path) -> tuple[HumanReviewEvaluation, ...]:
    rows: list[HumanReviewEvaluation] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid evaluation JSON on line {number}") from error
        rows.append(HumanReviewEvaluation.model_validate(payload))
    identities = [(row.case_id, row.run_id, row.reviewer_id_hash) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate reviewer evaluation")
    return tuple(rows)


__all__ = [
    "REQUIRED_VALIDATION_CATEGORIES",
    "REQUIRED_VALIDATION_LANGUAGES",
    "ControlledExpectedAnchor",
    "ControlledRepositoryCase",
    "ControlledRepositorySuite",
    "ExpectedAbstention",
    "ExpectedAnchorDisposition",
    "ExpectedFixability",
    "HumanReviewEvaluation",
    "IntervalMethod",
    "MetricGroup",
    "ProductMetricCatalog",
    "ProductMetricDefinition",
    "ProtocolResult",
    "ReviewerOutcome",
    "load_controlled_repository_suite",
    "load_human_evaluations",
    "load_metric_catalog",
]
