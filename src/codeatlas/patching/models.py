"""Typed models for patch proposals, files, hunks, validation, and lifecycle state."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from codeatlas.verification.models import TestPlan, TestResult


class PatchStatus:
    """Enumeration of allowed patch lifecycle statuses."""

    PROPOSED = "proposed"
    REJECTED = "rejected"
    REQUIRES_HUMAN_APPROVAL = "requires_human_approval"
    APPROVED = "approved"
    APPLIED_IN_ISOLATED_WORKTREE = "applied_in_isolated_worktree"
    TEST_EXECUTION_STARTED = "test_execution_started"
    TESTS_PASSED = "tests_passed"
    TESTS_FAILED = "tests_failed"
    TEST_TIMEOUT = "test_timeout"
    TEST_BLOCKED = "test_blocked"
    TEST_ERROR = "test_error"
    VALIDATED = "validated"
    FAILED_VALIDATION = "failed_validation"
    ABSTAINED = "abstained"

    ALL = {
        PROPOSED,
        REJECTED,
        REQUIRES_HUMAN_APPROVAL,
        APPROVED,
        APPLIED_IN_ISOLATED_WORKTREE,
        TEST_EXECUTION_STARTED,
        TESTS_PASSED,
        TESTS_FAILED,
        TEST_TIMEOUT,
        TEST_BLOCKED,
        TEST_ERROR,
        VALIDATED,
        FAILED_VALIDATION,
        ABSTAINED,
    }


VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    PatchStatus.PROPOSED: {
        PatchStatus.REJECTED,
        PatchStatus.REQUIRES_HUMAN_APPROVAL,
        PatchStatus.APPROVED,
        PatchStatus.ABSTAINED,
    },
    PatchStatus.REQUIRES_HUMAN_APPROVAL: {
        PatchStatus.APPROVED,
        PatchStatus.REJECTED,
        PatchStatus.ABSTAINED,
    },
    PatchStatus.APPROVED: {
        PatchStatus.APPLIED_IN_ISOLATED_WORKTREE,
        PatchStatus.FAILED_VALIDATION,
        PatchStatus.REJECTED,
    },
    PatchStatus.APPLIED_IN_ISOLATED_WORKTREE: {
        PatchStatus.VALIDATED,
        PatchStatus.FAILED_VALIDATION,
        PatchStatus.TEST_EXECUTION_STARTED,
        PatchStatus.TEST_BLOCKED,
    },
    PatchStatus.TEST_EXECUTION_STARTED: {
        PatchStatus.TESTS_PASSED,
        PatchStatus.TESTS_FAILED,
        PatchStatus.TEST_TIMEOUT,
        PatchStatus.TEST_BLOCKED,
        PatchStatus.TEST_ERROR,
        PatchStatus.FAILED_VALIDATION,
    },
    PatchStatus.TESTS_PASSED: {
        PatchStatus.VALIDATED,
        PatchStatus.TEST_EXECUTION_STARTED,
        PatchStatus.TEST_BLOCKED,
        PatchStatus.FAILED_VALIDATION,
    },
    PatchStatus.TESTS_FAILED: {
        PatchStatus.FAILED_VALIDATION,
    },
    PatchStatus.TEST_TIMEOUT: {
        PatchStatus.FAILED_VALIDATION,
    },
    PatchStatus.TEST_BLOCKED: {
        PatchStatus.FAILED_VALIDATION,
    },
    PatchStatus.TEST_ERROR: {
        PatchStatus.FAILED_VALIDATION,
    },
    PatchStatus.REJECTED: set(),
    PatchStatus.VALIDATED: set(),
    PatchStatus.FAILED_VALIDATION: set(),
    PatchStatus.ABSTAINED: set(),
}


class PatchHunk(BaseModel):
    """Single hunk within a unified diff file patch."""

    model_config = ConfigDict(extra="ignore")

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: list[str] = Field(default_factory=list)
    header: str = ""


class PatchFile(BaseModel):
    """File-level patch specification derived from a unified diff."""

    model_config = ConfigDict(extra="ignore")

    path: str
    operation: str = Field(description="modify, add, delete, rename")
    hunks: list[PatchHunk] = Field(default_factory=list)
    original_hash: str | None = None
    proposed_hash: str | None = None
    old_path: str | None = None


class PatchRedactionAudit(BaseModel):
    """Redaction checks and audit results for a patch proposal."""

    model_config = ConfigDict(extra="ignore")

    safe: bool = True
    raw_value_matches: int = 0
    rules_applied: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)


class PatchValidationResult(BaseModel):
    """Validation report for patch syntax, applicability, and safety checks."""

    model_config = ConfigDict(extra="ignore")

    valid: bool = False
    applies_cleanly: bool = False
    syntax_valid: bool | None = None
    type_check_status: str = "not_run"
    tests_status: str = "not_run"
    build_status: str = "not_run"
    security_check_status: str = "not_run"
    changed_files: list[str] = Field(default_factory=list)
    rejected_files: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)
    execution_allowed: bool = False
    sandbox_id: str | None = None
    # Phase 7C isolated-validation report fields.
    proposal_id: str | None = None
    approval_verified: bool | None = None
    base_commit: str | None = None
    patch_hash: str | None = None
    resulting_diff_hash: str | None = None
    policy_decision: dict[str, Any] | None = None
    redaction_audit: dict[str, Any] | None = None
    cleanup_status: str | None = Field(
        default=None,
        description="completed, failed, retained, or not_applicable",
    )
    sandbox_retained: bool = False
    duration_ms: float | None = None
    # Phase 8A sandboxed test execution fields.
    test_plan: dict[str, Any] | None = None
    test_runner: str | None = None
    tests_run: list[str] = Field(default_factory=list)
    test_discovery_reason: str | None = None
    test_exit_code: int | None = None
    test_duration_ms: float | None = None
    test_failures: list[str] = Field(default_factory=list)
    test_timeout: float | None = None
    test_output_redaction_audit: dict[str, Any] | None = None
    network_allowed: bool = False
    dependency_install_allowed: bool = False
    resource_limits: dict[str, Any] | None = None
    test_execution_attempted: bool = False
    test_execution_blocked_reason: str | None = None
    test_stdout_summary: str | None = None
    test_stderr_summary: str | None = None
    # Phase 8B-1 structured diagnostics & network isolation
    diagnostic_summary: str | None = None
    failed_test_names: list[str] = Field(default_factory=list)
    test_failure_count: int | None = None
    test_pass_count: int | None = None
    test_skip_count: int | None = None
    test_output_truncated: bool | None = None
    network_policy_requested: str | None = None
    network_policy_enforced: bool | None = None
    network_isolation_verified: bool | None = None
    diagnostic_limitations: list[str] = Field(default_factory=list)
    stack_trace_summary: str | None = None
    # Phase 8B-2 selective full-suite execution fields
    full_suite_requested: bool = False
    full_suite_policy_opted_in: bool = False
    full_suite_status: str | None = None
    full_suite_blocked_reason: str | None = None
    full_suite_command: list[str] | None = None
    full_suite_result: dict[str, Any] | None = None
    # Phase 8C: retain the actual execution records, never reconstruct observations.
    test_result: TestResult | None = None
    full_suite_test_plan: TestPlan | None = None
    approval_scope: str | None = None
    patch_applied_in_isolated_sandbox: bool = False


class PatchProposal(BaseModel):
    """Deterministic, typed proposal for a code remediation patch."""

    model_config = ConfigDict(extra="ignore")

    proposal_id: str
    finding_id: str
    provider_name: str
    provider_version: str
    base_commit: str
    target_files: list[str] = Field(default_factory=list)
    unified_diff: str
    rationale: str
    expected_behavior: str
    risk_level: str = "low"
    requested_action: str = "validate"
    status: str = PatchStatus.PROPOSED
    policy_decision: dict[str, Any] | None = None
    created_at: str
    limitations: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    patch_hash: str = ""
    redaction_audit: PatchRedactionAudit = Field(default_factory=PatchRedactionAudit)

    def transition_to(self, new_status: str, *, reason: str | None = None) -> None:
        """Safely transition to a new lifecycle status or raise ValueError."""
        if new_status not in PatchStatus.ALL:
            raise ValueError(f"Unknown patch status '{new_status}'")
        allowed = VALID_STATUS_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid patch status transition from '{self.status}' to '{new_status}'"
                + (f": {reason}" if reason else "")
            )
        self.status = new_status


__all__ = [
    "PatchStatus",
    "VALID_STATUS_TRANSITIONS",
    "PatchHunk",
    "PatchFile",
    "PatchRedactionAudit",
    "PatchValidationResult",
    "PatchProposal",
]
