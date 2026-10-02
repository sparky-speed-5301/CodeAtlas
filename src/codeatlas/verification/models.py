"""Typed models for Phase 8A test planning, execution, validation, and redaction."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class CommandValidation(BaseModel):
    """Validation report for an approved test command."""

    model_config = ConfigDict(extra="ignore")

    allowed: bool = False
    rejection_reason: str | None = None
    normalized_command: list[str] = Field(default_factory=list)
    executable: str | None = None
    rules_evaluated: list[str] = Field(default_factory=list)


class ResourceLimits(BaseModel):
    """Execution sandbox resource limits and safety policies."""

    model_config = ConfigDict(extra="ignore")

    timeout_seconds: float = 30.0
    max_output_bytes: int = 100_000
    max_memory_bytes: int | None = None
    max_processes: int | None = None
    network_allowed: bool = False
    dependency_install_allowed: bool = False


class OutputRedactionAudit(BaseModel):
    """Audit records of redactions applied to test execution outputs."""

    model_config = ConfigDict(extra="ignore")

    safe: bool = True
    raw_value_matches: int = 0
    rules_applied: list[str] = Field(default_factory=list)
    redacted_items_count: int = 0
    targets_audited: list[str] = Field(default_factory=list)


class TestPlan(BaseModel):
    """Deterministic, policy-validated test execution plan."""

    __test__ = False
    model_config = ConfigDict(extra="ignore")

    language: str
    test_files: list[str] = Field(default_factory=list)
    test_targets: list[str] = Field(default_factory=list)
    exact_command: list[str] = Field(default_factory=list)
    working_directory: str = "."
    environment_policy: str = "sanitized"
    timeout: float = 30.0
    network_policy: str = "disabled"
    dependency_install_policy: str = "forbidden"
    discovery_reason: str = ""
    confidence: float = 1.0
    limitations: list[str] = Field(default_factory=list)
    full_suite: bool = False


class TestResult(BaseModel):
    """Outcome and execution audit for sandboxed repository tests."""

    __test__ = False
    model_config = ConfigDict(extra="ignore")

    status: str = "not_run"  # passed, failed, timed_out, blocked, not_run, error
    runner: str = ""
    language: str = ""
    command: list[str] = Field(default_factory=list)
    working_directory: str = "."
    tests_run: list[str] = Field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    tests_skipped: int = 0
    failed_test_names: list[str] = Field(default_factory=list)
    failure_summary: str = ""
    stack_trace_summary: str = ""
    exit_code: int | None = None
    duration_ms: float = 0.0
    timeout: float = 30.0
    stdout_summary: str = ""
    stderr_summary: str = ""
    output_truncated: bool = False
    redaction_audit: dict[str, Any] = Field(default_factory=dict)
    network_policy_requested: str = "disabled"
    network_policy_enforced: bool = True
    network_isolation_verified: bool = False
    dependency_install_allowed: bool = False
    execution_allowed: bool = False
    network_allowed: bool = False
    commands_run: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    sandbox_id: str | None = None
    diagnostics_limitations: list[str] = Field(default_factory=list)
    full_suite: bool = False


__all__ = [
    "CommandValidation",
    "ResourceLimits",
    "OutputRedactionAudit",
    "TestPlan",
    "TestResult",
]
