"""Run manifest model."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunManifest(BaseModel):
    """Serializable metadata for one review run.

    ``base_commit`` and ``head_commit`` retain the schema contract; refs and
    detected languages carry the additional Phase 2 run context.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    base_commit: str | None = None
    head_commit: str | None = None
    base_ref: str
    head_ref: str
    detected_languages: list[str] = Field(default_factory=list)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    analyzed_files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    unresolved_files: list[str] = Field(default_factory=list)
    tools_run: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    model: str | None = None
    model_version: str | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = Field(default=0, ge=0)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    cost: float | None = Field(default=None, ge=0)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    quality_version: str = "11B.1"
    quality_summary: dict[str, Any] = Field(default_factory=dict)
    quality_limitations: list[str] = Field(default_factory=list)
    quality_decisions: list[str] = Field(default_factory=list)
    analyzers: list[str] = Field(default_factory=list)
    analyzer_failures: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    index_version: str | None = None
    indexed_files: int | None = None
    indexed_symbols: int | None = None
    indexed_imports: int | None = None
    index_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    changed_symbols: list[dict[str, Any]] = Field(default_factory=list)
    context_candidates: list[dict[str, Any]] = Field(default_factory=list)
    index_duration_ms: float | None = None
    retrieval_duration_ms: float | None = None
    review_packet_id: str | None = None
    packet_bytes: int | None = None
    packet_files: int | None = None
    packet_lines: int | None = None
    packet_truncated: bool | None = None
    packet_limitations: list[str] = Field(default_factory=list)
    redaction_audit: dict[str, Any] | None = None
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    provider_name: str | None = None
    provider_version: str | None = None
    provider_output_valid: bool | None = None
    provider_validation_errors: list[str] = Field(default_factory=list)
    live_provider_enabled: bool | None = None
    model_name: str | None = None
    provider_request_id: str | None = None
    provider_latency_ms: float | None = None
    provider_retries: int | None = None
    provider_usage: dict[str, Any] = Field(default_factory=dict)
    provider_cost: float | None = None
    provider_abstentions: list[str] = Field(default_factory=list)
    provider_safety_events: list[str] = Field(default_factory=list)
    merged_findings: list[dict[str, Any]] = Field(default_factory=list)
    patch_proposals: list[dict[str, Any]] = Field(default_factory=list)
    patch_validation: dict[str, Any] | None = None
    patch_suggestions_received: int | None = None
    patch_suggestions_accepted: int | None = None
    patch_suggestions_rejected: int | None = None
    patch_proposal_ids: list[str] = Field(default_factory=list)
    patch_proposal_statuses: list[str] = Field(default_factory=list)
    patch_rejection_reasons: list[str] = Field(default_factory=list)
    patch_suggestion_redaction_audit: dict[str, Any] | None = None
    patch_policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    automatic_application_attempted: bool | None = None
    automatic_application_blocked: bool | None = None
    approval_verified: bool | None = None
    approval_scope: str | None = None
    proposal_id: str | None = None
    patch_hash: str | None = None
    test_evidence_attached: bool = False
    test_evidence_summary: str = Field(default="", max_length=4000)
    validation_limitations: list[str] = Field(default_factory=list)
    isolated_validation_attempted: bool | None = None
    isolated_validation_status: str | None = None
    sandbox_id: str | None = None
    applies_cleanly: bool | None = None
    syntax_valid: bool | None = None
    tests_status: str | None = None
    build_status: str | None = None
    execution_allowed: bool | None = None
    resulting_diff_hash: str | None = None
    cleanup_status: str | None = None
    validation_warnings: list[str] = Field(default_factory=list)
    test_plan: dict[str, Any] | None = None
    test_runner: str | None = None
    test_discovery_reason: str | None = None
    test_exit_code: int | None = None
    test_duration_ms: float | None = None
    test_failures: list[str] = Field(default_factory=list)
    test_timeout: float | None = None
    test_output_redaction_audit: dict[str, Any] | None = None
    network_allowed: bool | None = None
    dependency_install_allowed: bool | None = None
    resource_limits: dict[str, Any] | None = None
    test_execution_attempted: bool | None = None
    test_execution_blocked_reason: str | None = None
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
    full_suite_requested: bool | None = None
    full_suite_policy_opted_in: bool | None = None
    full_suite_status: str | None = None
    full_suite_blocked_reason: str | None = None
    full_suite_command: list[str] = Field(default_factory=list)
    full_suite_result: dict[str, Any] | None = None


__all__ = ["RunManifest"]
