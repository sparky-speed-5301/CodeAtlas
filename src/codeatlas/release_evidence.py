"""Bounded evidence contract for a controlled limited-release decision.

This module only validates explicitly supplied evidence.  It never discovers
repositories, scans arbitrary paths, executes providers or tests, verifies a
release artifact by itself, or turns unavailable/not-run evidence into a pass.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from codeatlas.orchestrator.repair_models import repair_payload_is_safe

RELEASE_EVIDENCE_VERSION: Literal["limited-release-evidence-v1"] = (
    "limited-release-evidence-v1"
)
SUPPORTED_PLAN_VERSION: Literal["controlled-beta-evidence-plan-v1"] = (
    "controlled-beta-evidence-plan-v1"
)
SUPPORTED_BENCHMARK_VERSION: Literal["wp7-controlled-beta-v1"] = "wp7-controlled-beta-v1"
MAX_RELEASE_EVIDENCE_BYTES = 64_000
MAX_RELEASE_SESSIONS = 50
MAX_RELEASE_REPOSITORIES = 50
MAX_RELEASE_ARTIFACTS = 20
MAX_RELEASE_OBSERVATIONS = 200
MAX_RELEASE_ISSUES = 50

EvidenceStatus = Literal[
    "measured", "contract_only", "unavailable", "not_run", "blocked"
]
ExecutionMode = Literal[
    "offline", "live", "fallback", "unavailable", "blocked", "not_run"
]
ReleaseDecision = Literal[
    "release_candidate", "release_candidate_with_limitations", "blocked"
]
MetricGroup = Literal[
    "review_usefulness", "autofix_usefulness", "reliability", "trust_and_safety"
]
MetricUnit = Literal["rate", "milliseconds", "minutes", "count"]
ProductIssueCode = Literal[
    "provider_unavailable",
    "provider_failure",
    "timeout",
    "cancellation",
    "stale_run",
    "recovery_failure",
    "orphan_worktree",
    "extension_failure",
    "unsupported_tool",
    "storage_failure",
    "unsafe_patch_rejected",
    "stale_proposal",
    "unauthorized_apply",
    "artifact_verification_failure",
    "missing_required_evidence",
    "cross_repository_scope_failure",
    "approval_binding_failure",
    "network_boundary_failure",
    "credential_boundary_failure",
    "rollback_failure",
    "cleanup_failure",
]

SafeIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Reason = Annotated[str, Field(min_length=1, max_length=256)]

MODES: tuple[ExecutionMode, ...] = (
    "offline",
    "live",
    "fallback",
    "unavailable",
    "blocked",
    "not_run",
)
REQUIRED_KPI_GROUPS: frozenset[str] = frozenset(
    {"review_usefulness", "autofix_usefulness", "reliability", "trust_and_safety"}
)


class _ReleaseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ReleaseObservation(_ReleaseModel):
    """One bounded status-bearing observation, including its execution mode."""

    status: EvidenceStatus
    mode: ExecutionMode
    numerator: int = Field(default=0, ge=0, le=500_000)
    denominator: int = Field(default=0, ge=0, le=500_000)
    value: float | None = Field(default=None, ge=0, le=1_000_000_000, allow_inf_nan=False)
    unit: MetricUnit = "rate"
    observation_count: int = Field(default=0, ge=0, le=500_000)
    evidence_source: SafeIdentifier
    exclusions: tuple[Reason, ...] = Field(default=(), max_length=20)
    limitations: tuple[Reason, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_observation(self) -> ReleaseObservation:
        if self.unit == "rate":
            if self.numerator > self.denominator:
                raise ValueError("observation numerator exceeds denominator")
            expected = self.numerator / self.denominator if self.denominator else None
            if self.value != expected:
                raise ValueError("observation value does not match its counts")
        elif self.numerator or self.denominator:
            raise ValueError("non-rate observations cannot claim rate counts")
        if self.status in {"unavailable", "not_run", "blocked"} and self.denominator:
            raise ValueError("unavailable, not-run, and blocked observations cannot claim measured counts")
        if self.status in {"unavailable", "not_run", "blocked"} and not self.limitations:
            raise ValueError("unavailable, not-run, and blocked observations require limitations")
        if self.status in {"unavailable", "not_run", "blocked"} and self.value is not None:
            raise ValueError("unavailable, not-run, and blocked observations cannot claim a value")
        if self.status in {"measured", "contract_only"} and self.observation_count == 0 and (
            self.denominator or self.value is not None
        ):
            raise ValueError("measured observations with counts require observation_count")
        return self


class EvidencePlanIdentity(_ReleaseModel):
    plan_version: Literal["controlled-beta-evidence-plan-v1"] = SUPPORTED_PLAN_VERSION
    plan_sha256: Digest
    benchmark_version: Literal["wp7-controlled-beta-v1"] = SUPPORTED_BENCHMARK_VERSION
    observation: ReleaseObservation

    @model_validator(mode="after")
    def validate_identity(self) -> EvidencePlanIdentity:
        if self.observation.status in {"measured", "contract_only"} and self.observation.mode != "offline":
            raise ValueError("plan identity must use offline evidence mode")
        return self


class BenchmarkIdentity(_ReleaseModel):
    benchmark_version: Literal["wp7-controlled-beta-v1"] = SUPPORTED_BENCHMARK_VERSION
    suite_sha256: Digest
    metric_catalog_sha256: Digest
    observation: ReleaseObservation


class SourceRevisionIdentity(_ReleaseModel):
    source_revision: Commit | None = None
    source_tree_sha256: Digest | None = None
    reproducibility_identity: Digest | None = None
    observation: ReleaseObservation

    @model_validator(mode="after")
    def validate_revision(self) -> SourceRevisionIdentity:
        has_identity = all(
            value is not None
            for value in (
                self.source_revision,
                self.source_tree_sha256,
                self.reproducibility_identity,
            )
        )
        if self.observation.status in {"measured", "contract_only"} and not has_identity:
            raise ValueError("measured source identity requires revision and reproducibility hashes")
        if self.observation.status in {"unavailable", "not_run", "blocked"} and has_identity:
            raise ValueError("unavailable source identity cannot claim complete hashes")
        return self


class SessionEvidenceIdentity(_ReleaseModel):
    session_id: str = Field(pattern=r"^beta-session-[0-9a-f]{32}$")
    repository_identity_hash: str = Field(pattern=r"^repo-[0-9a-f]{64}$")
    case_id: SafeIdentifier
    provider_mode: ExecutionMode
    observation: ReleaseObservation


class RepositoryCountMetadata(_ReleaseModel):
    authorized_repositories: int = Field(ge=0, le=MAX_RELEASE_REPOSITORIES)
    observed_repositories: int = Field(ge=0, le=MAX_RELEASE_REPOSITORIES)
    excluded_repositories: int = Field(ge=0, le=MAX_RELEASE_REPOSITORIES)
    observation: ReleaseObservation

    @model_validator(mode="after")
    def validate_counts(self) -> RepositoryCountMetadata:
        if self.observed_repositories > self.authorized_repositories:
            raise ValueError("observed repositories cannot exceed authorized repositories")
        if self.observation.status in {"measured", "contract_only"}:
            if self.observation.observation_count != self.observed_repositories:
                raise ValueError("repository observation count must match observed repositories")
        return self


class ProviderModeCoverage(_ReleaseModel):
    mode: ExecutionMode
    sessions: int = Field(ge=0, le=MAX_RELEASE_SESSIONS)
    provider_requests: int = Field(ge=0, le=MAX_RELEASE_SESSIONS * 100)
    observation: ReleaseObservation


class PlatformCoverage(_ReleaseModel):
    platform: SafeIdentifier
    runtime: SafeIdentifier
    observation: ReleaseObservation


class ArtifactIdentity(_ReleaseModel):
    artifact_id: SafeIdentifier
    artifact_type: Literal["wheel", "vsix", "checksum_manifest", "evidence_report"]
    file_name: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    sha256: Digest | None = None
    size_bytes: int = Field(ge=0, le=MAX_RELEASE_EVIDENCE_BYTES * 16)
    source_revision: Commit | None = None
    reproducibility_identity: Digest | None = None
    observation: ReleaseObservation

    @model_validator(mode="after")
    def validate_artifact(self) -> ArtifactIdentity:
        complete = self.sha256 is not None and self.source_revision is not None and self.reproducibility_identity is not None
        if self.observation.status in {"measured", "contract_only"} and not complete:
            raise ValueError("measured artifact identity requires checksum, source revision, and reproducibility identity")
        if self.observation.status in {"unavailable", "not_run", "blocked"} and complete:
            raise ValueError("unavailable artifact evidence cannot claim complete identity")
        return self


class KPIObservation(_ReleaseModel):
    metric_name: SafeIdentifier
    metric_group: MetricGroup
    observation: ReleaseObservation


class ProductIssueObservation(_ReleaseModel):
    issue_code: ProductIssueCode
    count: int = Field(ge=0, le=MAX_RELEASE_SESSIONS * 100)
    observation: ReleaseObservation

    @model_validator(mode="after")
    def validate_issue_count(self) -> ProductIssueObservation:
        if self.observation.status in {"measured", "contract_only"} and self.observation.observation_count != self.count:
            raise ValueError("issue observation count must match its issue count")
        return self


class RecoveryObservation(_ReleaseModel):
    operation: Literal[
        "rollback",
        "cleanup",
        "interrupted_session_recovery",
        "artifact_cleanup",
    ]
    result: Literal["passed", "failed", "blocked", "unavailable", "not_run"]
    observation: ReleaseObservation

    @model_validator(mode="after")
    def validate_result(self) -> RecoveryObservation:
        if self.observation.status == "measured" and self.result not in {"passed", "failed"}:
            raise ValueError("measured recovery observations require a passed or failed result")
        if self.observation.status in {"blocked", "unavailable", "not_run"} and self.result not in {
            "blocked", "unavailable", "not_run"
        }:
            raise ValueError("non-measured recovery observations cannot claim a completed result")
        return self


class ReleaseAssessment(_ReleaseModel):
    decision: ReleaseDecision
    evaluated_at: datetime
    evidence_complete: bool
    critical_safety_failures: bool
    blocking_conditions: tuple[Reason, ...] = Field(default=(), max_length=20)
    limitations: tuple[Reason, ...] = Field(default=(), max_length=20)
    production_ready: Literal[False] = False

    @model_validator(mode="after")
    def validate_assessment(self) -> ReleaseAssessment:
        if self.decision == "blocked" and not self.blocking_conditions:
            raise ValueError("blocked release assessments require blocking conditions")
        if self.decision == "release_candidate" and (
            not self.evidence_complete
            or self.critical_safety_failures
            or self.blocking_conditions
            or self.limitations
        ):
            raise ValueError("release_candidate requires complete evidence without limitations or blockers")
        if self.decision == "release_candidate_with_limitations" and not self.limitations:
            raise ValueError("candidate with limitations requires explicit limitations")
        return self


class ReleaseEvidence(_ReleaseModel):
    """Complete, explicit evidence package for one limited-release decision."""

    evidence_version: Literal["limited-release-evidence-v1"] = RELEASE_EVIDENCE_VERSION
    generated_at: datetime
    evidence_plan: EvidencePlanIdentity
    benchmark: BenchmarkIdentity
    source_revision: SourceRevisionIdentity
    sessions: tuple[SessionEvidenceIdentity, ...] = Field(default=(), max_length=MAX_RELEASE_SESSIONS)
    repository_counts: RepositoryCountMetadata
    provider_mode_coverage: tuple[ProviderModeCoverage, ...] = Field(
        min_length=6, max_length=6
    )
    platform_coverage: tuple[PlatformCoverage, ...] = Field(default=(), max_length=50)
    artifacts: tuple[ArtifactIdentity, ...] = Field(default=(), max_length=MAX_RELEASE_ARTIFACTS)
    kpis: tuple[KPIObservation, ...] = Field(default=(), max_length=MAX_RELEASE_OBSERVATIONS)
    product_issues: tuple[ProductIssueObservation, ...] = Field(default=(), max_length=MAX_RELEASE_ISSUES)
    rollback_observations: tuple[RecoveryObservation, ...] = Field(default=(), max_length=20)
    cleanup_observations: tuple[RecoveryObservation, ...] = Field(default=(), max_length=20)
    limitations: tuple[Reason, ...] = Field(default=(), max_length=30)
    assessment: ReleaseAssessment

    @model_validator(mode="after")
    def validate_evidence(self) -> ReleaseEvidence:
        if tuple(item.mode for item in self.provider_mode_coverage) != MODES:
            raise ValueError("provider mode coverage must contain all six modes in order")
        if len({item.mode for item in self.provider_mode_coverage}) != len(self.provider_mode_coverage):
            raise ValueError("provider mode coverage must be unique")

        session_ids = tuple(item.session_id for item in self.sessions)
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("session identifiers must be unique")
        repository_ids = {item.repository_identity_hash for item in self.sessions}
        if self.repository_counts.observation.status in {"measured", "contract_only"}:
            if self.repository_counts.observed_repositories != len(repository_ids):
                raise ValueError("repository count does not match session repository identities")
        if len(self.artifacts) != len({item.artifact_id for item in self.artifacts}):
            raise ValueError("artifact identities must be unique")
        if len(self.kpis) != len({item.metric_name for item in self.kpis}):
            raise ValueError("KPI metric names must be unique")
        if len(self.product_issues) != len({item.issue_code for item in self.product_issues}):
            raise ValueError("product issue codes must be unique")
        if set(item.metric_group for item in self.kpis) != REQUIRED_KPI_GROUPS:
            raise ValueError("KPI evidence must cover all required metric groups")
        if len(self.kpis) > MAX_RELEASE_OBSERVATIONS:
            raise ValueError("KPI evidence exceeds its fixed bound")
        if self.assessment.decision == "release_candidate" and (
            any(item.observation.status in {"blocked", "unavailable", "not_run"} for item in self.kpis)
            or any(item.observation.status == "contract_only" for item in self.kpis)
            or any(item.observation.status in {"blocked", "unavailable", "not_run"} for item in self.artifacts)
        ):
            raise ValueError("release_candidate cannot contain contract-only, blocked, unavailable, or not-run required evidence")

        payload = self.model_dump(mode="json")
        if not repair_payload_is_safe(payload):
            raise ValueError("release evidence contains secret-like material")
        return self


def _read_json(path: str | Path) -> dict[str, object]:
    source = Path(path).expanduser().absolute()
    try:
        stat = source.stat()
    except OSError as error:
        raise ValueError("release evidence must be an accessible regular file") from error
    if source.is_symlink() or not source.is_file() or stat.st_nlink != 1:
        raise ValueError("release evidence must be an unlinked regular file")
    with source.open("rb") as stream:
        content = stream.read(MAX_RELEASE_EVIDENCE_BYTES + 1)
    if len(content) > MAX_RELEASE_EVIDENCE_BYTES:
        raise ValueError("release evidence exceeds its fixed input bound")

    def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("release evidence contains duplicate keys")
            result[key] = value
        return result

    try:
        payload = json.loads(content, object_pairs_hook=unique_keys)
    except json.JSONDecodeError as error:
        raise ValueError("release evidence is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("release evidence must be a JSON object")
    return payload


def load_release_evidence(path: str | Path) -> ReleaseEvidence:
    """Load explicitly selected release evidence without discovering inputs."""
    payload = _read_json(path)
    try:
        evidence = ReleaseEvidence.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("release evidence is invalid") from error
    if not repair_payload_is_safe(evidence.model_dump(mode="json")):
        raise ValueError("release evidence failed the existing secret audit")
    return evidence


__all__ = [
    "ArtifactIdentity",
    "BenchmarkIdentity",
    "EvidencePlanIdentity",
    "ExecutionMode",
    "KPIObservation",
    "MODES",
    "PlatformCoverage",
    "ProductIssueCode",
    "ProductIssueObservation",
    "RecoveryObservation",
    "ReleaseAssessment",
    "ReleaseDecision",
    "ReleaseEvidence",
    "ReleaseObservation",
    "RepositoryCountMetadata",
    "SessionEvidenceIdentity",
    "SourceRevisionIdentity",
    "load_release_evidence",
]
