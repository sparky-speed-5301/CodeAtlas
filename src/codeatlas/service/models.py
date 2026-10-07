"""Typed request and response models for the CodeAtlas local service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Review lifecycle states
LifecycleState = Literal[
    "idle",
    "preparing",
    "indexing",
    "analyzing",
    "reviewing",
    "findings_ready",
    "patch_proposed",
    "approval_required",
    "validating",
    "tests_running",
    "completed",
    "failed",
    "cancelled",
]


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    service: str = "codeatlas-service"
    version: str = "0.1.0"
    timestamp: str
    pid: int
    active_reviews: int = 0
    provider: Literal["mock", "live", "deterministic", "mixed"] = "deterministic"


class ReviewCreateRequest(BaseModel):
    """Request to initiate a new review run."""
    repo: str = Field(description="Filesystem path to the target Git repository.")
    base: str = Field(default="main", description="Base reference or commit.")
    head: str = Field(default="HEAD", description="Head reference or commit.")
    review_provider: Literal["mock", "live"] | None = Field(default=None, description="Review provider (mock or live).")
    provider_model: str | None = Field(default=None, description="Optional provider model.")
    provider_timeout: float | None = Field(default=None, ge=0.1, le=300, description="Provider timeout in seconds.")
    index_repository: bool = Field(default=True, description="Whether to index repository symbols.")
    assemble_review_packet: bool = Field(default=True, description="Whether to assemble review packet.")
    allow_patch_suggestions: bool = Field(default=False, description="Whether to allow patch suggestions.")
    max_findings: int = Field(default=50, ge=1, le=200, description="Max findings to return.")


class FindingSummary(BaseModel):
    """Concise finding row for list views and decorations."""
    id: str
    severity: str
    category: str
    claim: str
    file: str
    line: int
    start_line: int
    end_line: int
    confidence: float
    evidence_strength: str
    status: str
    dismissed: bool = False
    quality_version: str = "11B.1"
    quality_decision: str = "review_only"
    quality_score: float = Field(default=0.0, ge=0, le=1)
    ambiguity_score: float = Field(default=0.0, ge=0, le=1)
    unsupported_flow: bool = False
    feedback: str | None = None


class ReviewStatusResponse(BaseModel):
    """Review progress and summary."""
    run_id: str
    status: LifecycleState
    progress_text: str = ""
    repository: str
    base: str
    head: str
    # Resolved revisions, populated once the run has produced its manifest.
    # Absent (None) until then; older clients ignore the extra fields.
    base_commit: str | None = None
    head_commit: str | None = None
    policy_decision: str = "unknown"
    finding_counts: dict[str, int] = Field(default_factory=dict)
    test_status: str | None = "not_run"
    full_suite_status: str | None = "not_run"
    patch_validation_status: str = "not_run"
    truncated: bool = False
    limitations: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""


class FindingsListResponse(BaseModel):
    """List of findings for a review run."""
    run_id: str
    total: int
    filtered_total: int
    findings: list[FindingSummary] = Field(default_factory=list)


class FindingContext(BaseModel):
    """Detailed context for a selected finding."""
    changed_lines: list[int] = Field(default_factory=list)
    changed_snippet: str = ""
    containing_symbol: dict[str, Any] | None = None
    relevant_imports: list[dict[str, Any]] = Field(default_factory=list)
    relevant_references: list[dict[str, Any]] = Field(default_factory=list)
    related_tests: list[str] = Field(default_factory=list)
    retrieved_context_candidates: list[dict[str, Any]] = Field(default_factory=list)
    truncation_status: bool = False
    evidence_sources: list[str] = Field(default_factory=list)


class FindingDetailResponse(BaseModel):
    """Comprehensive finding detail view."""
    run_id: str
    id: str
    claim: str
    severity: str
    category: str
    file: str
    line: int
    start_line: int
    end_line: int
    confidence: float
    evidence_strength: str
    status: str
    impact: str = ""
    limitations: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    deterministic_evidence: list[str] = Field(default_factory=list)
    reviewer_evidence: list[str] = Field(default_factory=list)
    policy_decision: str = "unknown"
    test_result: str = "not_run"
    patch_status: str = "none"
    dismissed: bool = False
    quality_version: str = "11B.1"
    evidence_sources: list[str] = Field(default_factory=list)
    deterministic_support: float = Field(default=0.0, ge=0, le=1)
    reviewer_support: float = Field(default=0.0, ge=0, le=1)
    changed_line_support: float = Field(default=0.0, ge=0, le=1)
    repository_context_support: float = Field(default=0.0, ge=0, le=1)
    test_support: float = Field(default=0.0, ge=0, le=1)
    ambiguity_score: float = Field(default=0.0, ge=0, le=1)
    truncation_penalty: float = Field(default=0.0, ge=0, le=1)
    unsupported_flow: bool = False
    abstention_reason: str | None = None
    duplicate_group_id: str | None = None
    suppressed_finding_ids: list[str] = Field(default_factory=list)
    quality_decision: str = "review_only"
    quality_score: float = Field(default=0.0, ge=0, le=1)
    score_components: dict[str, float] = Field(default_factory=dict)
    quality_limitations: list[str] = Field(default_factory=list)
    feedback: str | None = None
    context: FindingContext = Field(default_factory=FindingContext)


FeedbackLabel = Literal[
    "useful",
    "not_useful",
    "false_positive",
    "accepted",
    "dismissed",
    "needs_more_context",
]


class FindingFeedbackRequest(BaseModel):
    """Explicit, reversible repository-scoped feedback for one finding."""

    feedback: FeedbackLabel | None = None


class FindingFeedbackResponse(BaseModel):
    run_id: str
    finding_id: str
    repository: str
    feedback: FeedbackLabel | None = None
    previous_feedback: FeedbackLabel | None = None
    reversed: bool = False


class CancelResponse(BaseModel):
    """Response to review cancellation request."""
    run_id: str
    status: LifecycleState
    message: str


class ExplainRequest(BaseModel):
    """Request to explain a finding."""
    run_id: str | None = None


class ExplainResponse(BaseModel):
    """Safe, deterministic explanation of a finding."""
    finding_id: str
    category: str
    severity: str
    claim: str
    explanation: str
    impact: str
    remediation_advice: str
    limitations: list[str] = Field(default_factory=list)


class PatchProposalRequest(BaseModel):
    """Request to generate a draft fix for a finding."""
    run_id: str | None = None


class PatchProposalResponse(BaseModel):
    """Draft fix proposal details."""
    proposal_id: str
    finding_id: str
    status: str
    target_files: list[str] = Field(default_factory=list)
    unified_diff: str
    rationale: str
    risk_level: str
    approval_required: bool = True


class ValidateProposalRequest(BaseModel):
    """Request to validate an approved patch proposal."""
    approval_token: str = Field(description="Cryptographically scoped approval token.")
    run_tests: bool = Field(default=False, description="Run targeted verification tests.")
    run_full_suite: bool = Field(default=False, description="Run full test suite.")


class ValidateProposalResponse(BaseModel):
    """Outcome of patch validation in isolated sandbox."""
    proposal_id: str
    valid: bool
    status: str
    approval_verified: bool | None = False
    applies_cleanly: bool = False
    syntax_valid: bool | None = None
    tests_status: str | None = "not_run"
    full_suite_status: str | None = "not_run"
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 11C-B: FixProposal integration (bounded AI fix proposals, preview only)
# ---------------------------------------------------------------------------

FixProposalLifecycle = Literal[
    "not_eligible",
    "generating",
    "generation_failed",
    "rejected_by_policy",
    "draft_ready",
    "rejected",
    "regeneration_requested",
    "applied",
]

FIX_PROPOSAL_SCHEMA_VERSION = "11C-D.1"


class FixEligibilityResponse(BaseModel):
    """Safe eligibility decision for showing the Generate Fix affordance."""

    run_id: str
    finding_id: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    explanations: list[str] = Field(default_factory=list)


class FixProposalRequest(BaseModel):
    """Request a bounded fix proposal for one finding in one run."""

    run_id: str


class FixRejectRequest(BaseModel):
    """Reject one fix proposal; the ID must match the run and finding."""

    run_id: str
    proposal_id: str


class FixProposalResponse(BaseModel):
    """Typed, redacted FixProposal contract and validation state."""

    proposal_id: str
    finding_id: str
    run_id: str
    repository: str
    base_commit: str
    head_commit: str
    target_files: list[str] = Field(default_factory=list)
    patch_text: str = ""
    patch_hash: str = ""
    diagnosis: str = ""
    explanation: str = ""
    expected_behavior_change: str = ""
    assumptions: list[str] = Field(default_factory=list)
    risk_level: str = "medium"
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence_strength: str = "none"
    evidence_sources: list[str] = Field(default_factory=list)
    quality_decision: str = "review_only"
    policy_decision: dict[str, Any] = Field(default_factory=dict)
    generation_status: FixProposalLifecycle = "generating"
    approval_required: bool = True
    limitations: list[str] = Field(default_factory=list)
    rejection_reason: str | None = None
    rejection_explanation: str | None = None
    created_at: str = ""
    schema_version: str = FIX_PROPOSAL_SCHEMA_VERSION
    validation_operation: Literal["validate"] = "validate"
    validation_status: Literal[
        "not_requested",
        "approval_required",
        "approved_for_validation",
        "validating",
        "applied_in_isolated_worktree",
        "tests_running",
        "validated",
        "applied",
        "reverted",
        "validation_failed",
        "cleanup_failed",
    ] = "not_requested"
    validation_result: dict[str, Any] | None = None
    review_packet: dict[str, Any] | None = None
    human_approval_manifest: dict[str, Any] | None = None
    validation_history: list[str] = Field(default_factory=list)


class FixValidationApprovalRequest(BaseModel):
    """Operator-supplied approval for one FixProposal validation operation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    finding_id: str
    proposal_id: str
    approval_token: str = Field(min_length=1, max_length=256)


class FixValidationRequest(BaseModel):
    """Request to run an already approved FixProposal in isolation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    finding_id: str
    proposal_id: str
    approval_token: str = Field(min_length=1, max_length=256)
    run_tests: bool = False
    run_full_suite: bool = False


class FixValidationApprovalResponse(BaseModel):
    """Approval outcome; the token is deliberately never returned."""

    proposal_id: str
    finding_id: str
    run_id: str
    operation: Literal["validate"] = "validate"
    approval_verified: bool = False
    validation_status: Literal["approval_required", "approved_for_validation"] = "approval_required"
    errors: list[str] = Field(default_factory=list)


class FixValidationResponse(BaseModel):
    """Bound validation evidence for one FixProposal and one review run."""

    proposal_id: str
    finding_id: str
    run_id: str
    operation: Literal["validate"] = "validate"
    status: str
    validation_status: Literal[
        "approval_required",
        "approved_for_validation",
        "validating",
        "applied_in_isolated_worktree",
        "tests_running",
        "validated",
        "validation_failed",
        "cleanup_failed",
    ] = "validation_failed"
    approval_verified: bool = False
    valid: bool = False
    applies_cleanly: bool = False
    syntax_valid: bool | None = None
    tests_status: str = "not_run"
    full_suite_status: str = "not_run"
    sandbox_id: str | None = None
    cleanup_status: str | None = None
    resulting_diff_hash: str | None = None
    commands_run: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    test_plan: dict[str, Any] | None = None
    test_result: dict[str, Any] | None = None
    full_suite_result: dict[str, Any] | None = None
    validation_result: dict[str, Any] = Field(default_factory=dict)
    observed_test_evidence: dict[str, Any] | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    review_packet: dict[str, Any] | None = None
    human_approval_manifest: dict[str, Any] | None = None
    validation_history: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", strict=True)


# ---------------------------------------------------------------------------
# Phase 11C-D/E: explicit apply of a validated FixProposal, revert, and history
# ---------------------------------------------------------------------------

FixApplyStatus = Literal["not_applied", "applied", "reverted"]


class FixApplyRequest(BaseModel):
    """Explicit user confirmation to apply one validated FixProposal.

    ``approval_token`` must be minted for operation ``apply`` (a validate
    token is rejected), and ``confirmed_patch_hash`` must be the complete
    current patch hash of the exact proposal being applied.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    finding_id: str
    proposal_id: str
    approval_token: str = Field(min_length=1, max_length=256)
    confirmed_patch_hash: str = Field(min_length=64, max_length=64)


class FixRevertRequest(BaseModel):
    """Explicit user confirmation to revert one applied FixProposal."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    finding_id: str
    proposal_id: str
    confirmed_patch_hash: str = Field(min_length=64, max_length=64)


class FixApplyEvent(BaseModel):
    """One bounded, redacted entry in a proposal's apply history."""

    model_config = ConfigDict(extra="forbid")

    event: Literal["applied", "reverted", "apply_rejected", "revert_failed"]
    proposal_id: str
    finding_id: str
    run_id: str
    patch_hash: str
    resulting_diff_hash: str | None = None
    head_commit: str = ""
    branch_ref: str = ""
    files: list[str] = Field(default_factory=list)
    reason: str | None = None
    at: str


class FixApplyResponse(BaseModel):
    """Bounded outcome of one apply or revert operation; tokens never appear."""

    model_config = ConfigDict(extra="forbid", strict=True)

    proposal_id: str
    finding_id: str
    run_id: str
    operation: Literal["apply", "revert"]
    apply_status: FixApplyStatus = "not_applied"
    approval_verified: bool = False
    head_commit: str = ""
    branch_ref: str = ""
    patch_hash: str = ""
    resulting_diff_hash: str | None = None
    validated_resulting_diff_hash: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    files_restored: list[str] = Field(default_factory=list)
    apply_history: list[FixApplyEvent] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class FixApplyHistoryResponse(BaseModel):
    """Bounded apply history and revert availability for one FixProposal."""

    model_config = ConfigDict(extra="forbid", strict=True)

    proposal_id: str
    finding_id: str
    run_id: str
    apply_status: FixApplyStatus = "not_applied"
    revert_available: bool = False
    head_commit_at_apply: str = ""
    branch_ref_at_apply: str = ""
    patch_hash: str = ""
    resulting_diff_hash: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    events: list[FixApplyEvent] = Field(default_factory=list)
