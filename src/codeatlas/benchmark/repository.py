"""Versioned real-repository review benchmark contracts and metrics."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

BenchmarkCategory = Literal[
    "clean_changes",
    "real_bugs",
    "security_issues",
    "sensitive_data_exposure",
    "authorization_authentication",
    "type_nullability",
    "test_regressions",
    "performance_regressions",
    "ambiguous_changes",
    "correct_abstention",
    "ai_generated_defects",
    "multi_language_changes",
]
Population = Literal["positive", "negative", "ambiguous"]
RunMode = Literal["deterministic", "live_provider"]
Severity = Literal["info", "low", "medium", "high", "blocker"]
QualityDecision = Literal[
    "report", "report_with_uncertainty", "review_only", "abstain",
    "suppress_duplicate", "suppress_low_evidence",
]


class ExpectedAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor_id: str = Field(min_length=1, max_length=128)
    file: str = Field(min_length=1, max_length=512)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    category: str = Field(min_length=1, max_length=128)
    severity: Severity
    quality_decision: QualityDecision = "report"
    fixability: Literal["not_fixable", "review_required", "fixable", "ambiguous"] = "review_required"
    evidence_required: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> ExpectedAnchor:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        _validate_relative_path(self.file, "anchor file")
        return self


class PatchScopeExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_files: tuple[str, ...] = ()
    forbidden_files: tuple[str, ...] = ()
    changed_line_only: bool = True
    max_files: int = Field(default=1, ge=0, le=100)
    max_changed_lines: int = Field(default=150, ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_paths(self) -> PatchScopeExpectation:
        for path in (*self.allowed_files, *self.forbidden_files):
            _validate_relative_path(path, "patch scope file")
        if set(self.allowed_files).intersection(self.forbidden_files):
            raise ValueError("allowed_files and forbidden_files must not overlap")
        return self


class TestCommandPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: Literal["not_run", "targeted_only", "targeted_then_full", "full_allowed"]
    allowed_commands: tuple[str, ...] = ()
    network_allowed: bool = False
    dependency_install_allowed: bool = False


class RepositoryBenchmarkCase(BaseModel):
    """One immutable benchmark label for a public repository or local mirror."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=128)
    repository_url: str | None = Field(default=None, max_length=2_000)
    fixture_ref: str | None = Field(default=None, max_length=512)
    repository_identity: str = Field(min_length=1, max_length=256)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    language: tuple[str, ...] = Field(min_length=1)
    changed_files: tuple[str, ...] = Field(min_length=1)
    benchmark_category: BenchmarkCategory
    population: Population
    expected_finding_categories: tuple[str, ...]
    expected_finding_anchors: tuple[ExpectedAnchor, ...]
    expected_severity: tuple[Severity, ...]
    expected_abstention: Literal["required", "allowed", "forbidden"]
    expected_fixability: Literal["not_fixable", "review_required", "fixable", "ambiguous"]
    expected_proposal_eligible: bool
    expected_patch_scope: PatchScopeExpectation
    expected_test_command_policy: TestCommandPolicy
    known_limitations: tuple[str, ...]
    benchmark_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_case(self) -> RepositoryBenchmarkCase:
        if not self.repository_url and not self.fixture_ref:
            raise ValueError("repository_url or fixture_ref is required")
        if self.fixture_ref:
            _validate_relative_path(self.fixture_ref, "fixture_ref")
        if self.repository_url and not self.repository_url.startswith(("https://", "http://")):
            raise ValueError("repository_url must use HTTP(S)")
        if self.base_sha == self.head_sha:
            raise ValueError("base_sha and head_sha must differ")
        for path in self.changed_files:
            _validate_relative_path(path, "changed file")
        changed = set(self.changed_files)
        if any(anchor.file not in changed for anchor in self.expected_finding_anchors):
            raise ValueError("expected anchors must refer to changed_files")
        categories = tuple(anchor.category for anchor in self.expected_finding_anchors)
        severities = tuple(anchor.severity for anchor in self.expected_finding_anchors)
        if categories != self.expected_finding_categories:
            raise ValueError("expected_finding_categories must align with expected_finding_anchors")
        if severities != self.expected_severity:
            raise ValueError("expected_severity must align with expected_finding_anchors")
        if self.population == "positive" and not self.expected_finding_anchors:
            raise ValueError("positive cases require at least one expected finding anchor")
        if self.population == "negative" and self.expected_finding_anchors:
            raise ValueError("negative cases cannot declare expected finding anchors")
        if self.expected_abstention == "required" and self.population != "ambiguous":
            raise ValueError("required abstention is reserved for ambiguous cases")
        if self.expected_fixability == "not_fixable" and self.expected_proposal_eligible:
            raise ValueError("not_fixable cases cannot be proposal eligible")
        return self


class RepositoryBenchmarkSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_version: str = Field(min_length=1, max_length=64)
    cases: tuple[RepositoryBenchmarkCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_suite(self) -> RepositoryBenchmarkSuite:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case IDs must be unique")
        if any(case.benchmark_version != self.benchmark_version for case in self.cases):
            raise ValueError("case benchmark_version must match the suite")
        return self


class ObservedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str = Field(min_length=1, max_length=128)
    file: str = Field(min_length=1, max_length=512)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    category: str = Field(min_length=1, max_length=128)
    severity: Severity
    quality_decision: QualityDecision
    evidence: tuple[str, ...] = ()
    rank: int = Field(ge=1)
    duplicate_of: str | None = None

    @model_validator(mode="after")
    def validate_anchor(self) -> ObservedFinding:
        _validate_relative_path(self.file, "observed finding file")
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReviewObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    mode: RunMode
    findings: tuple[ObservedFinding, ...] = ()
    abstained: bool = False
    abstention_reason: str | None = None
    time_to_first_finding_ms: float | None = Field(default=None, ge=0)
    total_review_duration_ms: float = Field(ge=0)
    provider_attempts: int = Field(default=0, ge=0)
    provider_failures: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_observation(self) -> ReviewObservation:
        if self.abstained and self.findings:
            raise ValueError("an abstained observation cannot contain findings")
        provider_values = (
            self.provider_attempts, self.provider_failures, self.retries,
            self.input_tokens, self.output_tokens, self.cost,
        )
        if self.mode == "deterministic" and any(value not in {None, 0} for value in provider_values):
            raise ValueError("deterministic observations cannot report provider activity")
        if self.provider_failures > self.provider_attempts:
            raise ValueError("provider_failures cannot exceed provider_attempts")
        if self.retries > self.provider_attempts:
            raise ValueError("retries cannot exceed provider_attempts")
        ranks = [finding.rank for finding in self.findings]
        if len(ranks) != len(set(ranks)):
            raise ValueError("finding ranks must be unique within a case")
        ids = {finding.finding_id for finding in self.findings}
        if any(finding.duplicate_of not in ids for finding in self.findings if finding.duplicate_of):
            raise ValueError("duplicate_of must refer to a finding in the same observation")
        return self


class ConfidenceInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["wilson_95"] = "wilson_95"
    low: float
    high: float


class MetricResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    value: float | None
    numerator: float | None
    denominator: float | None
    positive_population: str
    negative_population: str
    ambiguous_population: str
    exclusions: tuple[str, ...]
    confidence_interval: ConfidenceInterval | None
    benchmark_version: str
    unit: Literal["rate", "milliseconds", "count", "tokens", "cost"] = "rate"


class _MetricPopulation(TypedDict):
    version: str
    positive: str
    negative: str
    ambiguous: str


class ReviewBenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_version: str
    mode: RunMode
    case_count: int
    metrics: tuple[MetricResult, ...]
    category_case_counts: dict[str, int]


def _validate_relative_path(value: str, label: str) -> None:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a safe repository-relative path")


def _wilson(numerator: int, denominator: int) -> ConfidenceInterval | None:
    if denominator <= 0:
        return None
    z = 1.959963984540054
    p = numerator / denominator
    divisor = 1 + z * z / denominator
    center = (p + z * z / (2 * denominator)) / divisor
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * denominator)) / denominator) / divisor
    return ConfidenceInterval(low=max(0.0, center - margin), high=min(1.0, center + margin))


def _metric(
    name: str,
    numerator: float | None,
    denominator: float | None,
    version: str,
    *,
    positive: str,
    negative: str,
    ambiguous: str,
    exclusions: tuple[str, ...] = (),
    unit: Literal["rate", "milliseconds", "count", "tokens", "cost"] = "rate",
    with_confidence_interval: bool = True,
) -> MetricResult:
    if unit == "rate":
        value = None if not denominator else float(numerator or 0) / float(denominator)
        interval = _wilson(int(numerator or 0), int(denominator or 0)) if with_confidence_interval else None
    else:
        value = None if numerator is None or not denominator else float(numerator) / float(denominator)
        interval = None
    return MetricResult(
        name=name,
        value=value,
        numerator=None if numerator is None else float(numerator),
        denominator=None if denominator is None else float(denominator),
        positive_population=positive,
        negative_population=negative,
        ambiguous_population=ambiguous,
        exclusions=exclusions,
        confidence_interval=interval,
        benchmark_version=version,
        unit=unit,
    )


def _match_findings(case: RepositoryBenchmarkCase, observation: ReviewObservation) -> tuple[list[tuple[ExpectedAnchor, ObservedFinding]], list[ObservedFinding], list[ExpectedAnchor]]:
    available = sorted(observation.findings, key=lambda item: item.rank)
    matches: list[tuple[ExpectedAnchor, ObservedFinding]] = []
    missing: list[ExpectedAnchor] = []
    for expected in case.expected_finding_anchors:
        candidate = next((finding for finding in available if finding.category == expected.category), None)
        if candidate is None:
            missing.append(expected)
            continue
        available.remove(candidate)
        matches.append((expected, candidate))
    return matches, available, missing


def score_review_benchmark(
    suite: RepositoryBenchmarkSuite,
    observations: list[ReviewObservation] | tuple[ReviewObservation, ...],
) -> tuple[ReviewBenchmarkReport, ...]:
    """Score deterministic and live-provider observations in separate reports."""
    case_by_id = {case.case_id: case for case in suite.cases}
    seen: set[tuple[str, RunMode]] = set()
    grouped: dict[RunMode, list[tuple[RepositoryBenchmarkCase, ReviewObservation]]] = defaultdict(list)
    for observation in observations:
        case = case_by_id.get(observation.case_id)
        if case is None:
            raise ValueError(f"observation references unknown case: {observation.case_id}")
        key = (observation.case_id, observation.mode)
        if key in seen:
            raise ValueError(f"duplicate observation: {observation.case_id}/{observation.mode}")
        seen.add(key)
        grouped[observation.mode].append((case, observation))

    reports: list[ReviewBenchmarkReport] = []
    for mode in ("deterministic", "live_provider"):
        rows = grouped.get(mode, [])
        if not rows:
            continue
        positives = [row for row in rows if row[0].population == "positive"]
        negatives = [row for row in rows if row[0].population == "negative"]
        ambiguous_rows = [row for row in rows if row[0].population == "ambiguous"]
        matches: list[tuple[ExpectedAnchor, ObservedFinding]] = []
        unmatched_findings: list[ObservedFinding] = []
        false_negatives = 0
        negative_cases_with_findings = 0
        reciprocal_ranks: list[float] = []
        for case, observation in rows:
            paired, extras, missing = _match_findings(case, observation)
            if case.population != "ambiguous":
                matches.extend(paired)
                unmatched_findings.extend(extras)
            false_negatives += len(missing) if case.population == "positive" else 0
            if case.population == "negative" and observation.findings:
                negative_cases_with_findings += 1
            if case.expected_finding_anchors:
                relevant_ranks = [finding.rank for _, finding in paired]
                reciprocal_ranks.append(0.0 if not relevant_ranks else 1.0 / min(relevant_ranks))
        true_positives = len(matches)
        false_positives = len(unmatched_findings)
        expected_positives = sum(len(case.expected_finding_anchors) for case, _ in positives)
        reported_findings = sum(len(observation.findings) for _, observation in rows)
        duplicate_findings = sum(bool(finding.duplicate_of) for _, observation in rows for finding in observation.findings)
        non_abstentions = sum(not observation.abstained for _, observation in rows)
        anchor_correct = sum(
            expected.file == finding.file
            and finding.start_line <= expected.end_line
            and finding.end_line >= expected.start_line
            for expected, finding in matches
        )
        severity_correct = sum(expected.severity == finding.severity for expected, finding in matches)
        decision_correct = sum(expected.quality_decision == finding.quality_decision for expected, finding in matches)
        abstention_policy_rows = [
            (case, observation)
            for case, observation in rows
            if case.expected_abstention != "allowed"
        ]
        abstention_behavior_correct = sum(
            observation.abstained == (case.expected_abstention == "required")
            for case, observation in abstention_policy_rows
        )
        evidence_complete = sum((not expected.evidence_required) or bool(finding.evidence) for expected, finding in matches)
        abstentions = sum(observation.abstained for _, observation in rows)
        provider_attempts = sum(observation.provider_attempts for _, observation in rows)
        provider_failures = sum(observation.provider_failures for _, observation in rows)
        retries = sum(observation.retries for _, observation in rows)
        first_times = [observation.time_to_first_finding_ms for _, observation in rows if observation.time_to_first_finding_ms is not None]
        durations = [observation.total_review_duration_ms for _, observation in rows]
        input_tokens = [observation.input_tokens for _, observation in rows if observation.input_tokens is not None]
        output_tokens = [observation.output_tokens for _, observation in rows if observation.output_tokens is not None]
        costs = [observation.cost for _, observation in rows if observation.cost is not None]
        common: _MetricPopulation = {
            "version": suite.benchmark_version,
            "positive": f"{len(positives)} positive cases containing {expected_positives} expected anchors",
            "negative": f"{len(negatives)} clean negative cases",
            "ambiguous": f"{len(ambiguous_rows)} ambiguous cases",
        }
        metrics = [
            _metric("precision", true_positives, true_positives + false_positives, **common, exclusions=("ambiguous expected labels",)),
            _metric("recall", true_positives, expected_positives, **common, exclusions=("negative and ambiguous cases",)),
            _metric("false_positive_rate", negative_cases_with_findings, len(negatives), **common, exclusions=("positive and ambiguous cases",)),
            _metric("false_negative_rate", false_negatives, expected_positives, **common, exclusions=("negative and ambiguous cases",)),
            _metric("abstention_rate", abstentions, len(rows), **common),
            _metric("coverage", non_abstentions, len(rows), **common),
            _metric("duplicate_rate", duplicate_findings, reported_findings, **common, exclusions=("observations with no findings",)),
            _metric("finding_anchor_accuracy", anchor_correct, len(matches), **common, exclusions=("unmatched findings and missed expected anchors",)),
            _metric("severity_accuracy", severity_correct, len(matches), **common, exclusions=("unmatched findings and missed expected anchors",)),
            _metric("quality_decision_accuracy", decision_correct + abstention_behavior_correct, len(matches) + len(abstention_policy_rows), **common, exclusions=("unmatched findings, missed expected anchors, and allowed abstentions",)),
            _metric("abstention_behavior_accuracy", abstention_behavior_correct, len(abstention_policy_rows), **common, exclusions=("cases where abstention is allowed",)),
            _metric("evidence_completeness", evidence_complete, len(matches), **common, exclusions=("unmatched findings and missed expected anchors",)),
            _metric("ranking_quality", sum(reciprocal_ranks), len(reciprocal_ranks), **common, exclusions=("cases without expected findings",), with_confidence_interval=False),
            _metric("time_to_first_finding", sum(first_times) if first_times else None, len(first_times), **common, exclusions=("cases without a first-finding timestamp",), unit="milliseconds"),
            _metric("total_review_duration", sum(durations) if durations else None, len(durations), **common, unit="milliseconds"),
            _metric("provider_failure_rate", provider_failures, provider_attempts, **common, exclusions=(("not applicable to deterministic runs",) if mode == "deterministic" else ())),
            _metric("retry_rate", retries, provider_attempts, **common, exclusions=(("not applicable to deterministic runs",) if mode == "deterministic" else ())),
            _metric("input_token_usage", sum(input_tokens) if input_tokens else None, len(input_tokens), **common, exclusions=("provider did not report token usage",) if not input_tokens else (), unit="tokens"),
            _metric("output_token_usage", sum(output_tokens) if output_tokens else None, len(output_tokens), **common, exclusions=("provider did not report token usage",) if not output_tokens else (), unit="tokens"),
            _metric("provider_cost", sum(costs) if costs else None, len(costs), **common, exclusions=("provider did not report cost",) if not costs else (), unit="cost"),
        ]
        reports.append(ReviewBenchmarkReport(
            benchmark_version=suite.benchmark_version,
            mode=mode,
            case_count=len(rows),
            metrics=tuple(metrics),
            category_case_counts=dict(sorted((category, sum(case.benchmark_category == category for case, _ in rows)) for category in {case.benchmark_category for case, _ in rows})),
        ))
    return tuple(reports)


def load_benchmark_suite(path: str | Path, *, fixture_root: str | Path | None = None) -> RepositoryBenchmarkSuite:
    """Load labels and validate local fixture references without network access."""
    suite_path = Path(path).resolve(strict=True)
    suite = RepositoryBenchmarkSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))
    root = Path(fixture_root).resolve() if fixture_root else suite_path.parent
    for case in suite.cases:
        if not case.fixture_ref:
            continue
        _validate_relative_path(case.fixture_ref, "fixture_ref")
        fixture = (root / case.fixture_ref).resolve()
        if not fixture.is_relative_to(root) or not fixture.is_dir():
            raise ValueError(f"fixture_ref is unavailable or outside fixture root: {case.fixture_ref}")
        for changed_file in case.changed_files:
            if not any((fixture / snapshot / changed_file).is_file() for snapshot in ("before", "after")):
                raise ValueError(f"fixture does not contain changed file: {case.case_id}/{changed_file}")
        revisions_path = fixture / "revisions.json"
        if revisions_path.is_file():
            revisions = json.loads(revisions_path.read_text(encoding="utf-8"))
            if not isinstance(revisions, dict) or revisions.get("base_sha") != case.base_sha or revisions.get("head_sha") != case.head_sha:
                raise ValueError(f"fixture revisions do not match case: {case.case_id}")
    return suite


def load_review_observations(path: str | Path) -> list[ReviewObservation]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise TypeError("review observations must be a JSON list")
    return [ReviewObservation.model_validate(item) for item in data]


__all__ = [
    "BenchmarkCategory", "ConfidenceInterval", "ExpectedAnchor", "MetricResult",
    "ObservedFinding", "PatchScopeExpectation", "RepositoryBenchmarkCase",
    "RepositoryBenchmarkSuite", "ReviewBenchmarkReport", "ReviewObservation",
    "TestCommandPolicy", "load_benchmark_suite", "load_review_observations",
    "score_review_benchmark",
]
