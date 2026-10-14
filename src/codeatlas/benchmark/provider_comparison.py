"""Safe metadata contracts for controlled repair-provider comparisons."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.orchestrator.repair_models import repair_payload_is_safe

from .repair_metrics import CheckStatus
from .repository import RepositoryBenchmarkCase, RepositoryBenchmarkSuite

ProviderRunMode = Literal["offline", "live", "fallback", "dry_run"]
ProviderMode = Literal["offline", "online"]
ProviderOutcome = Literal["completed", "failed", "abstained", "not_run"]
ProviderFailureCode = Literal[
    "provider_unavailable", "timeout", "rate_limit", "malformed_response",
    "schema_failure", "output_too_large", "secret_detected", "unsupported_model",
    "budget_exceeded", "network_disabled", "retry_exhausted",
]
ValidationStatus = Literal[
    "not_run", "patch_policy_rejected", "static_validation_passed",
    "validated_in_sandbox", "validation_failed",
]
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_SECRET_MARKERS = ("sk-", "ghp_", "github_pat_", "akia", "asia", "bearer ", "-----begin ")


class ProviderPatchLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    max_target_files: int = Field(ge=1, le=100)
    max_patch_lines: int = Field(ge=1, le=150)
    max_patch_bytes: int = Field(ge=1, le=64_000)


class ProviderComparisonControl(BaseModel):
    """Inputs that must be identical for every run in one comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    repository_identity: str = Field(min_length=1, max_length=256)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    finding_anchor_id: str = Field(min_length=1, max_length=128)
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=64)
    patch_limits: ProviderPatchLimits
    validation_policy: str = Field(min_length=1, max_length=64)
    test_command_policy: Literal["not_run", "targeted_only", "targeted_then_full", "full_allowed"] = "not_run"
    language: tuple[str, ...] = Field(min_length=1, max_length=8)
    target_files: tuple[str, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_control(self) -> ProviderComparisonControl:
        if not repair_payload_is_safe(self.repository_identity) or any(char in self.repository_identity for char in "\r\n\x00"):
            raise ValueError("repository_identity contains unsafe metadata")
        if "://" in self.repository_identity:
            identity_url = urlsplit(self.repository_identity)
            if identity_url.username or identity_url.password or identity_url.query or identity_url.fragment:
                raise ValueError("repository_identity cannot contain credentials or query data")
        for value in (self.finding_anchor_id, self.policy_version, self.validation_policy, *self.language):
            _validate_safe_name(value, "comparison control identifier")
        if len(self.target_files) > self.patch_limits.max_target_files:
            raise ValueError("target_files exceed controlled patch limits")
        if len(set(self.target_files)) != len(self.target_files):
            raise ValueError("target_files must be unique")
        for path in self.target_files:
            _validate_relative_path(path)
        return self


class ProviderComparisonObservation(BaseModel):
    """One run's bounded metadata, excluding provider and repository content."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    comparison_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    run_mode: ProviderRunMode
    provider_name: str = Field(min_length=1, max_length=128)
    model_name: str | None = Field(default=None, max_length=128)
    provider_mode: ProviderMode
    provider_contacted: bool
    fallback_used: bool = False
    fallback_trigger_code: ProviderFailureCode | None = None
    control: ProviderComparisonControl
    request_size_bytes: int = Field(ge=0, le=2_000_000)
    response_size_bytes: int = Field(ge=0, le=2_000_000)
    request_size_basis: Literal["structured_request", "transport_payload"] = "structured_request"
    response_size_basis: Literal["accepted_structured_output", "transport_response"] = "accepted_structured_output"
    duration_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    input_tokens: int | None = Field(default=None, ge=0, le=2_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=2_000_000)
    outcome: ProviderOutcome
    failure_code: ProviderFailureCode | None = None
    retry_count: int = Field(default=0, ge=0, le=10)
    patch_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validation_status: ValidationStatus = "not_run"
    syntax: CheckStatus = "not_run"
    formatter: CheckStatus = "not_run"
    linter: CheckStatus = "not_run"
    type_check: CheckStatus = "not_run"
    targeted_tests: CheckStatus = "not_run"
    full_suite: CheckStatus = "not_run"
    cost_amount: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False)
    cost_currency: Literal["USD"] | None = None
    cost_basis: Literal["reported", "estimated"] | None = None

    @model_validator(mode="after")
    def validate_safe_state(self) -> ProviderComparisonObservation:
        for value in (self.comparison_id, self.case_id, self.provider_name):
            _validate_safe_name(value, "provider comparison identifier")
        if self.model_name is not None:
            _validate_safe_name(self.model_name, "model_name")
        if self.run_mode == "offline":
            if self.provider_mode != "offline" or self.provider_contacted or self.fallback_used:
                raise ValueError("offline runs cannot contact or fall back from a live provider")
        elif self.run_mode == "live":
            if self.provider_mode != "online" or not self.provider_contacted or self.fallback_used:
                raise ValueError("live runs require one controlled online provider path")
        elif self.run_mode == "fallback":
            if not self.fallback_used or self.fallback_trigger_code is None:
                raise ValueError("fallback runs require a safe fallback trigger code")
            if self.provider_mode != "offline":
                raise ValueError("fallback must identify the effective offline provider")
        elif self.run_mode == "dry_run":
            if self.provider_contacted or self.fallback_used or self.response_size_bytes != 0:
                raise ValueError("dry_run cannot contact a provider or record a response")
            if any(value not in {None, 0} for value in (self.input_tokens, self.output_tokens, self.cost_amount, self.retry_count)):
                raise ValueError("dry_run cannot report provider usage, cost, or retries")
            if self.patch_hash is not None or self.validation_status != "not_run":
                raise ValueError("dry_run cannot report a patch or validation")
            if any(status != "not_run" for status in self.check_statuses):
                raise ValueError("dry_run cannot report executed checks")
            if self.outcome != "not_run":
                raise ValueError("dry_run outcome must be not_run")
        if self.fallback_used != (self.fallback_trigger_code is not None):
            raise ValueError("fallback trigger metadata must match fallback_used")
        if self.outcome == "failed" and self.failure_code is None:
            raise ValueError("failed runs require a stable failure code")
        if self.outcome != "failed" and self.failure_code is not None:
            raise ValueError("only failed runs may report a final failure code")
        if self.patch_hash is None and self.validation_status not in {"not_run", "patch_policy_rejected"}:
            raise ValueError("validation results require a patch hash")
        if self.patch_hash is None and any(status != "not_run" for status in self.check_statuses):
            raise ValueError("check results require a patch hash")
        if self.targeted_tests in {"passed", "failed"} and self.control.test_command_policy == "not_run":
            raise ValueError("targeted test results violate controlled test policy")
        if self.full_suite in {"passed", "failed"} and self.control.test_command_policy not in {"targeted_then_full", "full_allowed"}:
            raise ValueError("full-suite results violate controlled test policy")
        if self.validation_status == "static_validation_passed" and any(
            status != "not_run" for status in (self.targeted_tests, self.full_suite)
        ):
            raise ValueError("static validation cannot report test execution")
        if self.outcome in {"abstained", "not_run"} and self.patch_hash is not None:
            raise ValueError("abstained or not-run providers cannot report a patch")
        if not repair_payload_is_safe(self.model_dump(mode="json")):
            raise ValueError("provider comparison contains unsafe metadata")
        if self.cost_amount is None and any(value is not None for value in (self.cost_currency, self.cost_basis)):
            raise ValueError("cost metadata requires cost_amount")
        if self.cost_amount is not None and (self.cost_currency is None or self.cost_basis is None):
            raise ValueError("cost_amount requires bounded currency and basis metadata")
        return self

    @property
    def check_statuses(self) -> tuple[CheckStatus, ...]:
        return (
            self.syntax, self.formatter, self.linter, self.type_check,
            self.targeted_tests, self.full_suite,
        )


class ProviderModeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    run_mode: ProviderRunMode
    runs: int
    completed: int
    failed: int
    abstained: int
    patches_generated: int
    validations_passed: int
    provider_contacts: int
    fallback_uses: int
    retries: int
    mean_request_size_bytes: float
    mean_response_size_bytes: float
    mean_duration_ms: float
    mean_input_tokens: float | None
    mean_output_tokens: float | None
    mean_cost: float | None


class ProviderComparisonGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    comparison_id: str
    case_id: str
    control_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    modes_present: tuple[ProviderRunMode, ...]
    missing_modes: tuple[ProviderRunMode, ...]
    runs: tuple[ProviderComparisonObservation, ...]


class ProviderComparisonReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    benchmark_version: str
    comparison_count: int
    run_count: int
    mode_summaries: tuple[ProviderModeSummary, ...]
    comparisons: tuple[ProviderComparisonGroup, ...]


def _validate_safe_name(value: str, label: str) -> None:
    lowered = value.lower()
    if not _SAFE_NAME.fullmatch(value) or any(marker in lowered for marker in _SECRET_MARKERS) or not repair_payload_is_safe(value):
        raise ValueError(f"{label} contains unsafe metadata")


def _validate_relative_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    if (
        not normalized or normalized != value or normalized.startswith(("/", ".", "-"))
        or ":" in normalized or not repair_payload_is_safe(normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError("target_files must be safe repository-relative paths")


def _control_hash(control: ProviderComparisonControl) -> str:
    payload = json.dumps(control.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_case_control(case: RepositoryBenchmarkCase, observation: ProviderComparisonObservation) -> None:
    control = observation.control
    if (
        control.repository_identity != case.repository_identity
        or control.base_sha != case.base_sha
        or control.head_sha != case.head_sha
        or control.language != case.language
        or control.target_files != case.expected_patch_scope.allowed_files
    ):
        raise ValueError(f"comparison control does not match benchmark case: {case.case_id}")
    anchor_ids = {anchor.anchor_id for anchor in case.expected_finding_anchors}
    if control.finding_anchor_id not in anchor_ids:
        raise ValueError(f"comparison finding anchor does not match benchmark case: {case.case_id}")
    if control.test_command_policy != "not_run" and control.test_command_policy != case.expected_test_command_policy.policy:
        raise ValueError(f"comparison test policy does not match benchmark case: {case.case_id}")
    expected_scope = case.expected_patch_scope
    if (
        control.patch_limits.max_target_files > expected_scope.max_files
        or control.patch_limits.max_patch_lines > expected_scope.max_changed_lines
    ):
        raise ValueError(f"comparison patch limits do not match benchmark case: {case.case_id}")


def score_provider_comparisons(
    suite: RepositoryBenchmarkSuite,
    observations: list[ProviderComparisonObservation] | tuple[ProviderComparisonObservation, ...],
) -> ProviderComparisonReport:
    """Validate controlled inputs and summarize safe operational metadata by mode."""
    cases = {case.case_id: case for case in suite.cases}
    grouped: dict[str, list[ProviderComparisonObservation]] = defaultdict(list)
    seen: set[tuple[str, ProviderRunMode]] = set()
    for observation in observations:
        case = cases.get(observation.case_id)
        if case is None:
            raise ValueError(f"provider comparison references unknown case: {observation.case_id}")
        _validate_case_control(case, observation)
        key = (observation.comparison_id, observation.run_mode)
        if key in seen:
            raise ValueError(f"duplicate comparison run mode: {observation.comparison_id}/{observation.run_mode}")
        seen.add(key)
        grouped[observation.comparison_id].append(observation)

    mode_order: tuple[ProviderRunMode, ...] = ("offline", "live", "fallback", "dry_run")
    groups: list[ProviderComparisonGroup] = []
    for comparison_id in sorted(grouped):
        runs = sorted(grouped[comparison_id], key=lambda item: mode_order.index(item.run_mode))
        case_ids = {run.case_id for run in runs}
        controls = {_control_hash(run.control) for run in runs}
        if len(case_ids) != 1 or len(controls) != 1:
            raise ValueError(f"comparison inputs are not controlled: {comparison_id}")
        modes = tuple(run.run_mode for run in runs)
        groups.append(ProviderComparisonGroup(
            comparison_id=comparison_id,
            case_id=runs[0].case_id,
            control_hash=next(iter(controls)),
            modes_present=modes,
            missing_modes=tuple(mode for mode in mode_order if mode not in modes),
            runs=tuple(runs),
        ))

    summaries: list[ProviderModeSummary] = []
    for mode in mode_order:
        rows = [observation for observation in observations if observation.run_mode == mode]
        if not rows:
            continue
        input_tokens = [row.input_tokens for row in rows if row.input_tokens is not None]
        output_tokens = [row.output_tokens for row in rows if row.output_tokens is not None]
        costs = [row.cost_amount for row in rows if row.cost_amount is not None]
        summaries.append(ProviderModeSummary(
            run_mode=mode,
            runs=len(rows),
            completed=sum(row.outcome == "completed" for row in rows),
            failed=sum(row.outcome == "failed" for row in rows),
            abstained=sum(row.outcome == "abstained" for row in rows),
            patches_generated=sum(row.patch_hash is not None for row in rows),
            validations_passed=sum(row.validation_status in {"static_validation_passed", "validated_in_sandbox"} for row in rows),
            provider_contacts=sum(row.provider_contacted for row in rows),
            fallback_uses=sum(row.fallback_used for row in rows),
            retries=sum(row.retry_count for row in rows),
            mean_request_size_bytes=mean(row.request_size_bytes for row in rows),
            mean_response_size_bytes=mean(row.response_size_bytes for row in rows),
            mean_duration_ms=mean(row.duration_ms for row in rows),
            mean_input_tokens=mean(input_tokens) if input_tokens else None,
            mean_output_tokens=mean(output_tokens) if output_tokens else None,
            mean_cost=mean(costs) if costs else None,
        ))
    return ProviderComparisonReport(
        benchmark_version=suite.benchmark_version,
        comparison_count=len(groups),
        run_count=len(observations),
        mode_summaries=tuple(summaries),
        comparisons=tuple(groups),
    )


def load_provider_comparisons(path: str | Path) -> list[ProviderComparisonObservation]:
    source = Path(path)
    if source.stat().st_size > 8_000_000:
        raise ValueError("provider comparison file exceeds the metadata byte limit")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("provider comparison file is not valid UTF-8 JSON") from error
    if not isinstance(data, list):
        raise TypeError("provider comparisons must be a JSON list")
    if len(data) > 10_000:
        raise ValueError("provider comparison file exceeds the run limit")
    return [ProviderComparisonObservation.model_validate(item) for item in data]


__all__ = [
    "ProviderComparisonControl", "ProviderComparisonGroup", "ProviderComparisonObservation",
    "ProviderComparisonReport", "ProviderModeSummary", "ProviderPatchLimits",
    "ProviderRunMode", "load_provider_comparisons", "score_provider_comparisons",
]
