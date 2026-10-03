"""Typed request and response models for the CodeAtlas local service."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field

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


class ReviewStatusResponse(BaseModel):
    """Review progress and summary."""
    run_id: str
    status: LifecycleState
    progress_text: str = ""
    repository: str
    base: str
    head: str
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
    context: FindingContext = Field(default_factory=FindingContext)


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
