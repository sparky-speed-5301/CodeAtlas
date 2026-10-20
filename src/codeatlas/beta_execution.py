"""Metadata-only aggregation of explicitly supplied controlled-beta evidence."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from codeatlas.beta_feedback import BetaFeedbackExport
    from codeatlas.beta_incidents import BetaIncidentReport
    from codeatlas.beta_session import BetaSession
    from codeatlas.onboarding import OnboardingReport

Mode = Literal["offline", "live", "fallback", "unavailable", "blocked", "not_run"]
MODES: tuple[Mode, ...] = ("offline", "live", "fallback", "unavailable", "blocked", "not_run")
MAX_EXECUTION_SESSIONS = 50


def load_feedback_evidence(path: str | Path) -> BetaFeedbackExport:
    from codeatlas.beta import MAX_BETA_REPORT_BYTES, _is_linked_path
    from codeatlas.beta_feedback import BetaFeedbackExport
    from codeatlas.orchestrator.repair_models import repair_payload_is_safe

    source = Path(path).expanduser().absolute()
    if _is_linked_path(source) or not source.is_file():
        raise ValueError("feedback evidence must be an unlinked regular file")
    with source.open("rb") as stream:
        payload = stream.read(MAX_BETA_REPORT_BYTES + 1)
    if len(payload) > MAX_BETA_REPORT_BYTES:
        raise ValueError("feedback evidence exceeds its fixed bound")
    value = BetaFeedbackExport.model_validate_json(payload)
    if not repair_payload_is_safe(value.model_dump(mode="json")):
        raise ValueError("feedback evidence failed the existing secret audit")
    return value


def load_onboarding_evidence(path: str | Path) -> OnboardingReport:
    from codeatlas.beta import MAX_BETA_REPORT_BYTES, _is_linked_path
    from codeatlas.onboarding import OnboardingReport
    from codeatlas.orchestrator.repair_models import repair_payload_is_safe

    source = Path(path).expanduser().absolute()
    if _is_linked_path(source) or not source.is_file():
        raise ValueError("onboarding evidence must be an unlinked regular file")
    with source.open("rb") as stream:
        payload = stream.read(MAX_BETA_REPORT_BYTES + 1)
    if len(payload) > MAX_BETA_REPORT_BYTES:
        raise ValueError("onboarding evidence exceeds its fixed bound")
    value = OnboardingReport.model_validate_json(payload)
    if not repair_payload_is_safe(value.model_dump(mode="json")):
        raise ValueError("onboarding evidence failed the existing secret audit")
    return value


class EvidenceRate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["measured", "contract_only", "unavailable", "not_run"] = "not_run"
    numerator: int = Field(default=0, ge=0, le=500_000)
    denominator: int = Field(default=0, ge=0, le=500_000)
    value: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def consistent(self) -> EvidenceRate:
        if self.numerator > self.denominator:
            raise ValueError("rate numerator exceeds denominator")
        expected = self.numerator / self.denominator if self.denominator else None
        if self.value != expected:
            raise ValueError("rate does not match its evidence counts")
        if self.status in {"not_run", "unavailable"} and self.denominator:
            raise ValueError("unavailable rates cannot claim measured counts")
        return self


def rate(numerator: int, denominator: int, basis: str = "measured") -> EvidenceRate:
    return EvidenceRate.model_validate({
        "status": basis if denominator else "not_run", "numerator": numerator,
        "denominator": denominator, "value": numerator / denominator if denominator else None,
    })


class ExecutionBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_mode: Mode
    sessions: int = Field(default=0, ge=0, le=MAX_EXECUTION_SESSIONS)
    session_completion: EvidenceRate = Field(default_factory=EvidenceRate)
    review_completion: EvidenceRate = Field(default_factory=EvidenceRate)
    finding_acceptance: EvidenceRate = Field(default_factory=EvidenceRate)
    false_positive_rate: EvidenceRate = Field(default_factory=lambda: EvidenceRate(status="unavailable"))
    abstention: EvidenceRate = Field(default_factory=EvidenceRate)
    fix_proposal_acceptance: EvidenceRate = Field(default_factory=lambda: EvidenceRate(status="unavailable"))
    validation_success: EvidenceRate = Field(default_factory=EvidenceRate)
    test_success: EvidenceRate = Field(default_factory=EvidenceRate)
    apply_count: int = Field(default=0, ge=0, le=MAX_EXECUTION_SESSIONS)
    revert_count: int = Field(default=0, ge=0, le=MAX_EXECUTION_SESSIONS)
    provider_failures: int = Field(default=0, ge=0, le=MAX_EXECUTION_SESSIONS)
    timeouts: int = Field(default=0, ge=0, le=MAX_EXECUTION_SESSIONS)
    cancellations: int = Field(default=0, ge=0, le=MAX_EXECUTION_SESSIONS)
    github_dry_run: dict[Literal["passed", "failed", "blocked", "unavailable", "not_run"], int] = Field(default_factory=dict, max_length=5)


class BetaExecutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    summary_version: Literal["beta-execution-v1"] = "beta-execution-v1"
    evidence_basis: Literal["measured", "contract_only", "unavailable"] = "unavailable"
    session_count: int = Field(default=0, ge=0, le=MAX_EXECUTION_SESSIONS)
    repository_count: int = Field(default=0, ge=0, le=MAX_EXECUTION_SESSIONS)
    onboarding_status: Literal["ready", "ready_with_limitations", "blocked", "unavailable", "not_run"] = "not_run"
    extension_health: Literal["blocked", "unavailable", "not_run"] = "not_run"
    buckets: tuple[ExecutionBucket, ...] = Field(min_length=6, max_length=6)
    incident_count: int = Field(default=0, ge=0, le=1000)
    incident_occurrences: int = Field(default=0, ge=0, le=1_000_000)
    recovery_events: int = Field(default=0, ge=0, le=1_000_000)
    incidents_truncated: bool = False
    session_history_truncated: bool = False
    correctness_established: Literal[False] = False
    production_ready: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self) -> BetaExecutionSummary:
        if tuple(bucket.provider_mode for bucket in self.buckets) != MODES:
            raise ValueError("execution modes must be complete, distinct, and ordered")
        if sum(bucket.sessions for bucket in self.buckets) != self.session_count:
            raise ValueError("session totals do not match mode buckets")
        return self


def summarize_execution(
    sessions: tuple[BetaSession, ...] = (), *,
    onboarding: OnboardingReport | None = None,
    incidents: BetaIncidentReport | None = None,
    feedback: tuple[BetaFeedbackExport, ...] = (),
    evidence_basis: Literal["measured", "contract_only"] = "measured",
) -> BetaExecutionSummary:
    """Count supplied records only; never execute stages or infer fallback/correctness."""
    from codeatlas.beta_session import BetaSession
    from codeatlas.onboarding import OnboardingReport

    if len(sessions) > MAX_EXECUTION_SESSIONS:
        raise ValueError("too many execution sessions")
    sessions = tuple(BetaSession.model_validate(item.model_dump()) for item in sessions)
    if len({item.session_id for item in sessions}) != len(sessions):
        raise ValueError("duplicate sessions cannot inflate metrics")
    repositories = {item.repository_identity_hash for item in sessions}
    onboarding_status = "not_run"
    extension_health = "not_run"
    if onboarding is not None:
        onboarding = OnboardingReport.model_validate(onboarding.model_dump())
        if repositories and repositories != {onboarding.repository_identity}:
            raise ValueError("onboarding and session repository scopes differ")
        onboarding_status = onboarding.status
        extension = next((item for item in onboarding.checks if item.name == "extension_api"), None)
        if extension is not None:
            extension_health = "blocked" if extension.status == "blocked" else "unavailable" if extension.status == "unavailable" else "not_run"
    from codeatlas.beta_feedback import BetaFeedbackExport

    if len(feedback) > 50:
        raise ValueError("too many feedback inputs")
    feedback = tuple(BetaFeedbackExport.model_validate(item.model_dump()) for item in feedback)
    if len({(item.repository_identity_hash, item.run_id) for item in feedback}) != len(feedback):
        raise ValueError("duplicate feedback runs")
    if repositories and any(item.repository_identity_hash not in repositories for item in feedback):
        raise ValueError("feedback repository scope mismatch")
    incident_count = incident_occurrences = recovery_events = 0
    incidents_truncated = False
    if incidents is not None:
        from codeatlas.beta_incidents import BetaIncidentReport

        incident_report = BetaIncidentReport.model_validate(incidents.model_dump())
        if repositories and {incident_report.repository_identity_hash} != repositories:
            raise ValueError("incident and session repository scopes differ")
        incident_count = len(incident_report.incidents)
        incident_occurrences = incident_count
        recovery_events = sum(item.component == "recovery" for item in incident_report.incidents)
    buckets = []
    for mode in MODES:
        selected = [item for item in sessions if item.provider_mode == mode]
        reviews = [item for item in selected if item.review_duration_ms is not None or item.failure_code in {"offline_review_failed", "provider_failed", "provider_unavailable"}]
        github = {state: sum(item.github_dry_run_result == state for item in selected) for state in ("passed", "failed", "blocked", "unavailable", "not_run")}
        evaluated = sum(item.accepted_count + item.rejected_count for item in selected)
        findings = sum(item.finding_count for item in selected)
        feedback_items = [item for report in feedback if report.provider_mode == mode for item in report.items]
        finding_feedback = [item for item in feedback_items if item.target == "finding" and item.outcome in {"accepted", "rejected"}]
        proposal_feedback = [item for item in feedback_items if item.target == "fix_proposal" and item.outcome in {"accepted", "rejected"}]
        buckets.append(ExecutionBucket.model_validate({
            "provider_mode": mode, "sessions": len(selected),
            "session_completion": rate(sum(item.lifecycle_status == "completed" for item in selected), len(selected), evidence_basis),
            "review_completion": rate(sum(item.review_duration_ms is not None and item.failure_code not in {"offline_review_failed", "provider_failed", "provider_unavailable"} for item in reviews), len(reviews), evidence_basis),
            "finding_acceptance": rate(sum(item.outcome == "accepted" for item in finding_feedback), len(finding_feedback), evidence_basis) if feedback else rate(sum(item.accepted_count for item in selected), evaluated, evidence_basis),
            "fix_proposal_acceptance": rate(sum(item.outcome == "accepted" for item in proposal_feedback), len(proposal_feedback), evidence_basis) if feedback else EvidenceRate(status="unavailable"),
            "abstention": rate(sum(item.abstained_count for item in selected), findings, evidence_basis),
            "validation_success": rate(sum(item.validation_result == "passed" for item in selected), sum(item.validation_result in {"passed", "failed"} for item in selected), evidence_basis),
            "test_success": rate(sum(item.test_result == "passed" for item in selected), sum(item.test_result in {"passed", "failed"} for item in selected), evidence_basis),
            "apply_count": sum(item.apply_status in {"applied", "reverted"} for item in selected),
            "revert_count": sum(item.revert_status == "reverted" for item in selected),
            "provider_failures": sum(item.failure_code in {"provider_failed", "provider_unavailable"} for item in selected),
            "timeouts": sum(item.timed_out for item in selected),
            "cancellations": sum(item.cancelled for item in selected), "github_dry_run": github,
        }))
    return BetaExecutionSummary.model_validate({
        "evidence_basis": evidence_basis if sessions or onboarding else "unavailable",
        "session_count": len(sessions), "repository_count": len(repositories),
        "onboarding_status": onboarding_status, "extension_health": extension_health,
        "buckets": tuple(buckets), "incident_count": incident_count,
        "incident_occurrences": incident_occurrences, "recovery_events": recovery_events,
        "incidents_truncated": incidents_truncated,
        "session_history_truncated": any(item.events_truncated for item in sessions),
    })
