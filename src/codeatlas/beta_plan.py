"""Privacy-safe, bounded controlled-beta evidence-plan contracts.

The plan is a declaration of what an operator may collect.  It is not a
telemetry collector and it is not evidence that a beta has happened.  The
contract deliberately contains policy and bounded counts only; repositories,
prompts, provider material, credentials, approval tokens, and reviewer names
are not valid plan data.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from codeatlas.orchestrator.repair_models import repair_payload_is_safe

EVIDENCE_PLAN_VERSION: Literal["controlled-beta-evidence-plan-v1"] = (
    "controlled-beta-evidence-plan-v1"
)
BENCHMARK_VERSION: Literal["wp7-controlled-beta-v1"] = "wp7-controlled-beta-v1"
MAX_EVIDENCE_PLAN_BYTES = 32_768
MAX_COHORT_SIZE = 50
MAX_CASES = 50
MAX_OBSERVATION_DAYS = 90
MAX_RETENTION_DAYS = 90

PlanStatus = Literal["configured", "blocked", "invalid", "not_evaluated"]
PlanStatusReason = Literal[
    "ready_for_authorized_collection",
    "repository_or_session_not_authorized",
    "consent_not_obtained",
    "required_ground_truth_unavailable",
    "release_stop_triggered",
    "schema_validation_failed",
    "no_observations_available",
]

Language = Literal["python", "javascript_typescript", "go", "java", "rust"]
CaseCategory = Literal[
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
]
ThresholdMetric = Literal[
    "review_completion_rate",
    "finding_usefulness_rate",
    "fix_proposal_acceptance_rate",
    "validation_success_rate",
    "secret_redaction_success_rate",
    "unsafe_action_rejection_rate",
    "unauthorized_apply_rejection_rate",
    "cross_repository_isolation_rate",
    "consent_compliance_rate",
    "unexpected_workspace_mutation_rate",
]
ThresholdDirection = Literal["minimum", "maximum"]

REQUIRED_LANGUAGES: frozenset[str] = frozenset(
    {"python", "javascript_typescript", "go", "java", "rust"}
)
REQUIRED_CASE_CATEGORIES: frozenset[str] = frozenset(
    {
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
    }
)
THRESHOLD_DIRECTIONS: dict[str, ThresholdDirection] = {
    "review_completion_rate": "minimum",
    "finding_usefulness_rate": "minimum",
    "fix_proposal_acceptance_rate": "minimum",
    "validation_success_rate": "minimum",
    "secret_redaction_success_rate": "minimum",
    "unsafe_action_rejection_rate": "minimum",
    "unauthorized_apply_rejection_rate": "minimum",
    "cross_repository_isolation_rate": "minimum",
    "consent_compliance_rate": "minimum",
    "unexpected_workspace_mutation_rate": "maximum",
}


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CohortSize(_PlanModel):
    """Participant and session bounds for one explicitly authorized cohort."""

    minimum_participants: int = Field(ge=1, le=MAX_COHORT_SIZE)
    target_participants: int = Field(ge=1, le=MAX_COHORT_SIZE)
    maximum_participants: int = Field(ge=1, le=MAX_COHORT_SIZE)
    maximum_sessions: int = Field(ge=1, le=MAX_COHORT_SIZE)

    @model_validator(mode="after")
    def validate_bounds(self) -> CohortSize:
        if not (
            self.minimum_participants
            <= self.target_participants
            <= self.maximum_participants
        ):
            raise ValueError("cohort participant bounds must be ordered")
        if self.maximum_sessions < self.maximum_participants:
            raise ValueError("maximum_sessions must cover the participant bound")
        return self


class RepositorySelectionCriteria(_PlanModel):
    """Allowlist for repositories; no repository path or source is recorded."""

    authorization_mode: Literal["explicit_owner_or_maintainer_authorization"]
    repository_scope: Literal["controlled_repositories_only"]
    revision_policy: Literal["immutable_base_and_head"]
    worktree_policy: Literal["clean_worktree_required"]
    source_handling: Literal["metadata_only_no_source_persistence"]
    minimum_repositories: int = Field(ge=1, le=MAX_COHORT_SIZE)
    target_repositories: int = Field(ge=1, le=MAX_COHORT_SIZE)
    maximum_repositories: int = Field(ge=1, le=MAX_COHORT_SIZE)
    allowed_repository_classes: tuple[
        Literal[
            "explicitly_authorized_private",
            "explicitly_authorized_public",
            "sanitized_controlled_fixture",
        ],
        ...,
    ] = Field(min_length=1, max_length=3)
    allowed_languages: tuple[Language, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_selection(self) -> RepositorySelectionCriteria:
        if not (
            self.minimum_repositories
            <= self.target_repositories
            <= self.maximum_repositories
        ):
            raise ValueError("repository bounds must be ordered")
        if len(set(self.allowed_repository_classes)) != len(self.allowed_repository_classes):
            raise ValueError("allowed repository classes must be unique")
        if len(set(self.allowed_languages)) != len(self.allowed_languages):
            raise ValueError("allowed languages must be unique")
        return self


class SessionSelectionCriteria(_PlanModel):
    """Explicit session selection; nothing is discovered or collected silently."""

    authorization_mode: Literal["explicit_operator_opt_in"]
    collection_mode: Literal["explicit_artifact_export_only"]
    unsolicited_collection: Literal[False] = False
    automatic_collection: Literal[False] = False
    maximum_sessions: int = Field(ge=1, le=MAX_COHORT_SIZE)


class DistributionTarget(_PlanModel):
    """A bounded minimum/target/maximum allocation."""

    minimum_cases: int = Field(ge=0, le=MAX_CASES)
    target_cases: int = Field(ge=0, le=MAX_CASES)
    maximum_cases: int = Field(ge=0, le=MAX_CASES)

    @model_validator(mode="after")
    def validate_order(self) -> DistributionTarget:
        if not self.minimum_cases <= self.target_cases <= self.maximum_cases:
            raise ValueError("distribution bounds must be ordered")
        return self


class LanguageDistribution(_PlanModel):
    language: Language
    minimum_cases: int = Field(ge=1, le=MAX_CASES)
    target_cases: int = Field(ge=1, le=MAX_CASES)
    maximum_cases: int = Field(ge=1, le=MAX_CASES)

    @model_validator(mode="after")
    def validate_order(self) -> LanguageDistribution:
        if not self.minimum_cases <= self.target_cases <= self.maximum_cases:
            raise ValueError("language distribution bounds must be ordered")
        return self


class ReviewCaseDistribution(_PlanModel):
    category: CaseCategory
    allocation: DistributionTarget


class ObservationPeriod(_PlanModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_period(self) -> ObservationPeriod:
        duration = (self.end_date - self.start_date).days + 1
        if duration < 1 or duration > MAX_OBSERVATION_DAYS:
            raise ValueError("observation period must be between one and 90 days")
        return self


class SessionDurationPolicy(_PlanModel):
    target_minutes: int = Field(ge=1, le=180)
    maximum_minutes: int = Field(ge=1, le=240)
    idle_timeout_minutes: int = Field(ge=1, le=60)
    maximum_stage_minutes: int = Field(ge=1, le=120)
    stop_on_timeout: Literal[True] = True
    automatic_retry: Literal[False] = False

    @model_validator(mode="after")
    def validate_duration(self) -> SessionDurationPolicy:
        if self.target_minutes > self.maximum_minutes:
            raise ValueError("target session duration cannot exceed maximum")
        if self.idle_timeout_minutes > self.maximum_minutes:
            raise ValueError("idle timeout cannot exceed maximum session duration")
        if self.maximum_stage_minutes > self.maximum_minutes:
            raise ValueError("maximum stage duration cannot exceed session duration")
        return self


class GroundTruthRequirements(_PlanModel):
    benchmark_version: str = Field(min_length=1, max_length=64)
    source_persistence: Literal["metadata_only"]
    expected_labels_hidden_until_submission: Literal[True] = True
    immutable_revisions_required: Literal[True] = True
    tests_are_not_ground_truth: Literal[True] = True
    minimum_independent_adjudicators: int = Field(ge=1, le=2)
    minimum_ground_truth_cases: int = Field(ge=1, le=MAX_CASES)


class ReviewerProtocol(_PlanModel):
    protocol_version: Literal["human-review-v1"]
    case_order: Literal["randomized_per_reviewer"]
    reviewer_identity_mode: Literal["pseudonymous_hash_only"]
    outcomes: Literal["enum_only"]
    reason_codes: Literal["bounded_codes_only"]
    expected_labels_hidden_until_submission: Literal[True] = True
    one_record_per_case_and_run: Literal[True] = True
    independent_adjudication: Literal[True] = True
    free_form_comments_collected: Literal[False] = False
    maximum_reviewers: int = Field(ge=1, le=MAX_COHORT_SIZE)


class RetentionPolicy(_PlanModel):
    retention_days: int = Field(ge=1, le=MAX_RETENTION_DAYS)
    metadata_only: Literal[True] = True
    raw_repository_source_retained: Literal[False] = False
    raw_prompts_retained: Literal[False] = False
    raw_provider_transcripts_retained: Literal[False] = False
    credentials_retained: Literal[False] = False
    approval_tokens_retained: Literal[False] = False
    unrestricted_environment_retained: Literal[False] = False
    retention_basis: Literal["minimum_needed_for_predeclared_analysis"]


class DeletionProcess(_PlanModel):
    deletion_mode: Literal["explicit_operator_action"]
    deletion_trigger: Literal["retention_expiry_or_withdrawal"]
    temporary_checkouts_deleted: Literal[True] = True
    exported_artifacts_deleted: Literal[True] = True
    pseudonymous_reviewer_records_deleted: Literal[True] = True
    verification_required: Literal[True] = True
    automatic_persisted_path_deletion: Literal[False] = False
    foreign_process_termination: Literal[False] = False


class ConsentRequirements(_PlanModel):
    consent_mode: Literal["explicit_opt_in_before_collection"]
    repository_owner_authorization_required: Literal[True] = True
    session_operator_opt_in_required: Literal[True] = True
    reviewer_consent_required: Literal[True] = True
    reviewer_identity_consent_required: Literal[True] = True
    withdrawal_allowed: Literal[True] = True
    collection_before_consent: Literal[False] = False
    silent_collection: Literal[False] = False
    consent_record_mode: Literal["bounded_status_only"]


class CollectionPrivacyPolicy(_PlanModel):
    """Fields make the absence of raw collection auditable in the plan."""

    collection_mode: Literal["explicit_artifact_export_only"]
    raw_repository_source: Literal[False] = False
    raw_prompts: Literal[False] = False
    raw_provider_transcripts: Literal[False] = False
    credentials: Literal[False] = False
    approval_tokens: Literal[False] = False
    unrestricted_environment: Literal[False] = False
    reviewer_identity_without_consent: Literal[False] = False
    automatic_apply: Literal[False] = False
    automatic_approval: Literal[False] = False
    automatic_commit: Literal[False] = False
    automatic_merge: Literal[False] = False
    automatic_push: Literal[False] = False
    arbitrary_commands: Literal[False] = False
    dependency_installation: Literal[False] = False
    unrestricted_network_access: Literal[False] = False


class SuccessThreshold(_PlanModel):
    metric: ThresholdMetric
    direction: ThresholdDirection
    target: float = Field(ge=0, le=1, allow_inf_nan=False)
    minimum_observations: int = Field(ge=1, le=MAX_CASES)
    unavailable_or_not_evaluated: Literal["release_stop"] = "release_stop"

    @model_validator(mode="after")
    def validate_direction(self) -> SuccessThreshold:
        expected = THRESHOLD_DIRECTIONS[self.metric]
        if self.direction != expected:
            raise ValueError("threshold direction contradicts the metric")
        return self


class ReleaseStopCondition(_PlanModel):
    condition: Literal[
        "missing_or_withdrawn_consent",
        "unauthorized_repository_or_session",
        "ground_truth_or_benchmark_drift",
        "secret_or_sensitive_material_in_artifact",
        "unsafe_boundary_failure",
        "cross_repository_scope_failure",
        "unexpected_workspace_mutation",
        "required_threshold_not_met",
        "required_evidence_not_evaluated",
        "unbounded_collection_or_retention",
    ]
    action: Literal["stop_collection_and_hold_release"]


InclusionCriterion = Literal[
    "explicit_repository_authorization",
    "explicit_session_opt_in",
    "immutable_base_and_head",
    "clean_controlled_worktree",
    "supported_language",
    "ground_truth_available",
    "reviewer_consent_recorded",
    "metadata_only_export_path",
]
ExclusionCriterion = Literal[
    "missing_repository_authorization",
    "missing_session_opt_in",
    "mutable_or_unpinned_revision",
    "raw_source_or_prompt_required",
    "credential_or_token_material_present",
    "unbounded_environment_required",
    "unsupported_language_or_toolchain",
    "ground_truth_unavailable",
    "withdrawn_consent",
]


class BetaEvidencePlan(_PlanModel):
    """The complete, versioned plan for an authorized controlled-beta cohort."""

    plan_version: Literal["controlled-beta-evidence-plan-v1"] = EVIDENCE_PLAN_VERSION
    status: PlanStatus = "configured"
    status_reason: PlanStatusReason = "ready_for_authorized_collection"
    beta_mode: Literal[True] = True
    production_ready: Literal[False] = False
    benchmark_version: str = Field(min_length=1, max_length=64)
    cohort_size: CohortSize
    repository_selection: RepositorySelectionCriteria
    session_selection: SessionSelectionCriteria
    language_distribution: tuple[LanguageDistribution, ...] = Field(
        min_length=5, max_length=5
    )
    review_case_distribution: tuple[ReviewCaseDistribution, ...] = Field(
        min_length=10, max_length=10
    )
    fix_proposal_case_distribution: tuple[ReviewCaseDistribution, ...] = Field(
        min_length=1, max_length=10
    )
    observation_period: ObservationPeriod
    session_duration_policy: SessionDurationPolicy
    inclusion_criteria: tuple[InclusionCriterion, ...] = Field(
        min_length=1, max_length=8
    )
    exclusion_criteria: tuple[ExclusionCriterion, ...] = Field(
        min_length=1, max_length=9
    )
    ground_truth_requirements: GroundTruthRequirements
    reviewer_protocol: ReviewerProtocol
    retention_policy: RetentionPolicy
    deletion_process: DeletionProcess
    consent_requirements: ConsentRequirements
    collection_privacy: CollectionPrivacyPolicy
    success_thresholds: tuple[SuccessThreshold, ...] = Field(
        min_length=10, max_length=10
    )
    release_stop_conditions: tuple[ReleaseStopCondition, ...] = Field(
        min_length=10, max_length=10
    )
    release_assessment_status: Literal["not_evaluated"] = "not_evaluated"

    @model_validator(mode="after")
    def validate_plan(self) -> BetaEvidencePlan:
        allowed_status_reasons: dict[PlanStatus, frozenset[PlanStatusReason]] = {
            "configured": frozenset({"ready_for_authorized_collection"}),
            "blocked": frozenset(
                {
                    "repository_or_session_not_authorized",
                    "consent_not_obtained",
                    "required_ground_truth_unavailable",
                    "release_stop_triggered",
                }
            ),
            "invalid": frozenset({"schema_validation_failed"}),
            "not_evaluated": frozenset({"no_observations_available"}),
        }
        if self.status_reason not in allowed_status_reasons[self.status]:
            raise ValueError("plan status and status reason are contradictory")
        if self.benchmark_version != BENCHMARK_VERSION:
            raise ValueError("benchmark version is not supported by this plan contract")
        if self.ground_truth_requirements.benchmark_version != self.benchmark_version:
            raise ValueError("ground-truth benchmark version must match the plan")
        if self.session_selection.maximum_sessions != self.cohort_size.maximum_sessions:
            raise ValueError("session and cohort maximums must match")
        if self.repository_selection.maximum_repositories > MAX_COHORT_SIZE:
            raise ValueError("repository count exceeds the cohort bound")
        if self.reviewer_protocol.maximum_reviewers > self.cohort_size.maximum_participants:
            raise ValueError("reviewer count exceeds the cohort participant bound")

        languages = tuple(item.language for item in self.language_distribution)
        if len(set(languages)) != len(languages) or set(languages) != REQUIRED_LANGUAGES:
            raise ValueError("language distribution must cover each required language once")
        if any(language not in self.repository_selection.allowed_languages for language in languages):
            raise ValueError("language distribution contains a language outside repository selection")

        review_categories = tuple(item.category for item in self.review_case_distribution)
        if len(set(review_categories)) != len(review_categories) or set(review_categories) != REQUIRED_CASE_CATEGORIES:
            raise ValueError("review case distribution must cover each required category once")
        review_by_category = {
            item.category: item.allocation for item in self.review_case_distribution
        }
        fix_categories = tuple(item.category for item in self.fix_proposal_case_distribution)
        if len(set(fix_categories)) != len(fix_categories):
            raise ValueError("fix-proposal categories must be unique")
        for item in self.fix_proposal_case_distribution:
            review = review_by_category[item.category]
            if (
                item.allocation.minimum_cases > review.target_cases
                or item.allocation.target_cases > review.target_cases
                or item.allocation.maximum_cases > review.maximum_cases
            ):
                raise ValueError("fix-proposal cases must be a bounded review-case subset")

        language_totals = (
            sum(item.minimum_cases for item in self.language_distribution),
            sum(item.target_cases for item in self.language_distribution),
            sum(item.maximum_cases for item in self.language_distribution),
        )
        review_totals = (
            sum(item.allocation.minimum_cases for item in self.review_case_distribution),
            sum(item.allocation.target_cases for item in self.review_case_distribution),
            sum(item.allocation.maximum_cases for item in self.review_case_distribution),
        )
        if language_totals != review_totals:
            raise ValueError("language and review-case distributions contradict each other")
        if review_totals[2] > MAX_CASES or review_totals[1] < self.ground_truth_requirements.minimum_ground_truth_cases:
            raise ValueError("case distribution does not cover the ground-truth requirement")
        if sum(item.allocation.target_cases for item in self.fix_proposal_case_distribution) > review_totals[1]:
            raise ValueError("fix-proposal target exceeds review-case target")

        inclusion = set(self.inclusion_criteria)
        exclusion = set(self.exclusion_criteria)
        if inclusion.intersection(exclusion):
            raise ValueError("inclusion and exclusion criteria contradict each other")
        required_inclusion = {
            "explicit_repository_authorization",
            "explicit_session_opt_in",
            "immutable_base_and_head",
            "ground_truth_available",
            "metadata_only_export_path",
        }
        if not required_inclusion.issubset(inclusion):
            raise ValueError("required authorization and evidence criteria are missing")
        if self.collection_privacy.collection_mode != self.session_selection.collection_mode:
            raise ValueError("collection mode must match session selection")
        if self.retention_policy.retention_days > MAX_RETENTION_DAYS:
            raise ValueError("retention period exceeds the fixed bound")

        thresholds = tuple(item.metric for item in self.success_thresholds)
        if len(set(thresholds)) != len(thresholds):
            raise ValueError("success thresholds must not contain contradictory duplicate metrics")
        if set(thresholds) != set(THRESHOLD_DIRECTIONS):
            raise ValueError("success thresholds must cover the complete predeclared set")
        if any(item.minimum_observations > MAX_CASES for item in self.success_thresholds):
            raise ValueError("threshold observation counts exceed the fixed bound")

        stop_conditions = tuple(item.condition for item in self.release_stop_conditions)
        if len(set(stop_conditions)) != len(stop_conditions):
            raise ValueError("release-stop conditions must be unique")
        required_stops = {
            "missing_or_withdrawn_consent",
            "unauthorized_repository_or_session",
            "ground_truth_or_benchmark_drift",
            "secret_or_sensitive_material_in_artifact",
            "unsafe_boundary_failure",
            "cross_repository_scope_failure",
            "unexpected_workspace_mutation",
            "required_threshold_not_met",
            "required_evidence_not_evaluated",
            "unbounded_collection_or_retention",
        }
        if set(stop_conditions) != required_stops:
            raise ValueError("release-stop conditions must cover every mandatory stop")

        payload = self.model_dump(mode="json")
        if not repair_payload_is_safe(payload):
            raise ValueError("evidence plan contains secret-like material")
        return self


# Short aliases keep the contract easy to discover without duplicating models.
ControlledBetaEvidencePlan = BetaEvidencePlan
EvidencePlan = BetaEvidencePlan


class EvidencePlanValidation(_PlanModel):
    """Safe validation result; invalid input is never reflected in the result."""

    status: PlanStatus
    valid: bool
    error_code: Literal["none", "schema_validation_failed"] = "none"
    plan: BetaEvidencePlan | None = None


def validate_evidence_plan(payload: object) -> EvidencePlanValidation:
    """Validate a plan while keeping invalid payload details out of diagnostics."""
    try:
        plan = BetaEvidencePlan.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return EvidencePlanValidation(
            status="invalid", valid=False, error_code="schema_validation_failed"
        )
    return EvidencePlanValidation(
        status=plan.status, valid=plan.status == "configured", plan=plan
    )


def _read_plan_json(path: str | Path) -> dict[str, object]:
    source = Path(path).expanduser().absolute()
    try:
        stat = source.stat()
    except OSError as error:
        raise ValueError("evidence plan must be an accessible regular file") from error
    if source.is_symlink() or not source.is_file() or stat.st_nlink != 1:
        raise ValueError("evidence plan must be an unlinked regular file")
    with source.open("rb") as stream:
        content = stream.read(MAX_EVIDENCE_PLAN_BYTES + 1)
    if len(content) > MAX_EVIDENCE_PLAN_BYTES:
        raise ValueError("evidence plan exceeds the fixed input bound")

    def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("evidence plan contains duplicate keys")
            result[key] = value
        return result

    try:
        payload = json.loads(content, object_pairs_hook=unique_keys)
    except json.JSONDecodeError as error:
        raise ValueError("evidence plan is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("evidence plan must be a JSON object")
    return payload


def load_evidence_plan(path: str | Path) -> BetaEvidencePlan:
    """Load one explicitly selected plan without scanning or collecting anything."""
    payload = _read_plan_json(path)
    try:
        return BetaEvidencePlan.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as error:
        # Do not reflect validation details: the rejected payload may contain a
        # secret-like value or an unrestricted environment value.
        raise ValueError("evidence plan is invalid") from error


def validate_evidence_plan_file(path: str | Path) -> EvidencePlanValidation:
    """Return an explicit status for a plan file without reflecting its contents."""
    try:
        return EvidencePlanValidation(
            status=(plan := load_evidence_plan(path)).status,
            valid=plan.status == "configured",
            plan=plan,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return EvidencePlanValidation(
            status="invalid", valid=False, error_code="schema_validation_failed"
        )


__all__ = [
    "BENCHMARK_VERSION",
    "EVIDENCE_PLAN_VERSION",
    "MAX_EVIDENCE_PLAN_BYTES",
    "BetaEvidencePlan",
    "CollectionPrivacyPolicy",
    "ConsentRequirements",
    "ControlledBetaEvidencePlan",
    "CohortSize",
    "DeletionProcess",
    "EvidencePlan",
    "EvidencePlanValidation",
    "GroundTruthRequirements",
    "load_evidence_plan",
    "validate_evidence_plan",
    "validate_evidence_plan_file",
]
