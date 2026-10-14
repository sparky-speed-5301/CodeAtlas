"""Explicit data-only projections; live execution state is never serialized."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from codeatlas.orchestrator.repair_models import repair_payload_is_safe
from codeatlas.patching.proposal import compute_patch_hash
from codeatlas.service.models import (
    FixApplyEvent,
    FixProposalResponse,
    ReviewCreateRequest,
    ReviewStatusResponse,
)
from codeatlas.service.persistence import (
    KINDS,
    PersistenceError,
    canonical_json,
    repository_id,
)

FINDING_FIELDS = frozenset(["id", "severity", "category", "claim", "file", "line", "start_line", "end_line", "confidence", "evidence_strength", "status", "impact", "limitations", "evidence", "reviewer_evidence", "tools_consulted", "quality_version", "evidence_sources", "deterministic_support", "reviewer_support", "changed_line_support", "repository_context_support", "test_support", "ambiguity_score", "truncation_penalty", "unsupported_flow", "abstention_reason", "duplicate_group_id", "suppressed_finding_ids", "quality_decision", "quality_score", "quality_limitations", "fixability", "feedback"])
VALIDATION_FIELDS = frozenset(["proposal_id", "base_commit", "patch_hash", "valid", "applies_cleanly", "syntax_valid", "type_check_status", "tests_status", "build_status", "security_check_status", "changed_files", "rejected_files", "execution_allowed", "sandbox_id", "approval_verified", "resulting_diff_hash", "cleanup_status", "sandbox_retained", "duration_ms", "test_runner", "tests_run", "test_exit_code", "test_duration_ms", "test_execution_attempted", "network_allowed", "dependency_install_allowed", "test_failure_count", "test_pass_count", "test_skip_count", "test_output_truncated", "network_policy_requested", "network_policy_enforced", "network_isolation_verified", "full_suite_requested", "full_suite_policy_opted_in", "full_suite_status", "approval_scope", "patch_applied_in_isolated_sandbox"])
FIX_FIELDS = frozenset(FixProposalResponse.model_fields) - {"policy_decision", "review_packet", "human_approval_manifest", "validation_result", "recovery_state"}
REVIEW_FIELDS = frozenset(ReviewStatusResponse.model_fields)
REQUEST_FIELDS = frozenset(ReviewCreateRequest.model_fields)
PROVIDER_FIELDS = frozenset({"provider_name", "provider_version", "model_name", "provider_latency_ms", "provider_retries", "provider_cost"})
CAPABILITY_FIELDS = frozenset(["language", "language_version", "parser", "syntax_validation", "formatter", "linter", "type_checker", "test_runner", "protected_generated_files", "limitations"])
OPERATION_FIELDS = frozenset({"name", "proposal_id", "finding_id"})
APPLY_FIELDS = frozenset({"status", "patch_hash", "head_commit", "branch_ref", "applied_at",
                         "reverted_at", "resulting_diff_hash", "validated_resulting_diff_hash", "files_changed"})
MANIFEST_FIELDS = frozenset({"run_id", "repository", "base_commit", "head_commit", "base_ref", "head_ref"})
PATCH_FIELDS = frozenset(["proposal_id", "finding_id", "provider_name", "provider_version", "base_commit", "target_files", "unified_diff", "rationale", "expected_behavior", "risk_level", "requested_action", "status", "approval_required", "created_at", "limitations", "patch_hash"])

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ERROR_CODES = frozenset(["abstained", "suppressed_duplicate", "low_evidence", "ambiguous", "unsupported_flow", "invalid_location", "review_only_without_fix_eligibility", "finding_not_fix_eligible", "quality_gate_failed", "language_review_only", "unsupported_language", "protected_or_generated_target", "stale_run", "stale_finding", "finding_not_in_run", "invalid_run_finding", "incompatible_repository_state", "repository_state_stale", "unreadable_or_unbounded_target", "stale_or_truncated_context", "missing_required_context", "required_context_exceeds_bounds", "unsafe_or_unbounded_context", "source_parse_failed", "packet_redaction_failed", "invalid_provider_metadata", "offline_provider_required", "provider_abstained_or_failed", "invalid_provider_output", "unsafe_or_unbounded_provider_output", "provider_output_redaction_failed", "provider_output_exceeds_bounds", "provider_failed", "provider_scope_or_explanation_invalid", "exactly_one_finding_required", "exactly_one_proposal_required", "invalid_input", "patch_parse_failed", "patch_scope_invalid", "language_constraint_failed", "patch_preview_failed", "patch_preview_exceeds_bounds", "empty_repair", "syntax_validation_failed", "patch_validation_failed", "repository_state_changed_during_repair", "run_not_ready", "run_failed", "packet_unavailable", "generation_in_progress", "not_regenerable", "not_rejectable", "proposal_not_found", "scope_mismatch", "validation_not_approved", "validation_not_available", "validation_scope_invalid", "validation_token_invalid", "validation_redaction_failed", "apply_not_validated", "apply_validation_failed", "apply_evidence_missing", "apply_evidence_identity_mismatch", "apply_scope_invalid", "apply_policy_version_incompatible", "apply_not_available", "apply_confirmation_invalid", "apply_redaction_failed", "patch_hash_mismatch", "already_applied", "workspace_dirty", "apply_failed", "resulting_diff_mismatch", "revert_not_available", "revert_confirmation_invalid", "revert_state_changed", "revert_failed", "review_queue_full", "persistence_failed", "recovered_operation_unavailable", "review_not_retryable", "operation_interrupted", "operation_failed", "review_failed", "validation_failed", "persisted_identity_invalid"])
_SAFE_WARNING_CODES = frozenset({"validation_limitations_omitted"})
_FEEDBACK_LABELS = frozenset({
    "useful", "not_useful", "false_positive", "accepted", "dismissed", "needs_more_context",
})
_CAPABILITY_STATUSES = frozenset({"available", "unavailable", "unsupported", "not_run", "passed", "failed", "blocked"})
_LIFECYCLE_STATES = frozenset({
    "idle", "preparing", "indexing", "analyzing", "reviewing", "findings_ready", "patch_proposed",
    "approval_required", "validating", "tests_running", "completed", "failed", "cancelled",
    "pending_recovery", "recovery_failed", "expired",
})
_RECOVERY_STATES = frozenset({"not_required", "pending_recovery", "recovered", "recovery_failed", "expired"})
_OPERATION_NAMES = frozenset({"generation", "validation", "apply", "revert"})
_FINDING_SEVERITIES = frozenset({"info", "low", "medium", "high", "blocker"})
_EVIDENCE_STRENGTHS = frozenset({"none", "weak", "supported", "strong", "reproduced"})
_QUALITY_DECISIONS = frozenset({
    "report", "report_with_uncertainty", "review_only", "abstain", "suppress_duplicate", "suppress_low_evidence",
})
_PATCH_STATUSES = frozenset({
    "proposed", "rejected", "requires_human_approval", "approved", "applied_in_isolated_worktree",
    "test_execution_started", "tests_passed", "tests_failed", "test_timeout", "test_blocked", "test_error",
    "validated", "applied", "failed_validation", "abstained",
})


def safe_leaf(value: Any) -> Any:
    """Strings are scrubbed before bounding; structured free-form data is omitted."""
    if value is None or type(value) in {bool, int, float}:
        return value
    if isinstance(value, str):
        return value[:4000] if repair_payload_is_safe(value) else "[REDACTED]"
    if isinstance(value, (list, tuple)):
        return [safe_leaf(item) for item in value[:200] if not isinstance(item, (dict, list))]
    return None


def project(value: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {key: safe_leaf(value[key]) for key in sorted(fields & value.keys())}


def _safe_error_list(value: Any, *, fallback: str | None = None) -> list[str]:
    """Keep only fixed reason codes; provider/tool diagnostics never cross this boundary."""
    if value is None or value == [] or value == "":
        return []
    candidates = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    safe = [item for item in candidates if type(item) is str and item in _SAFE_ERROR_CODES]
    if safe:
        return list(dict.fromkeys(safe[:20]))
    return [fallback] if fallback is not None else []


def _safe_review_errors(record: Any) -> list[str]:
    code = getattr(record, "safe_error_code", None)
    if type(code) is str and code in _SAFE_ERROR_CODES:
        return [code]
    return _safe_error_list(getattr(record, "errors", []), fallback="review_failed")


def _is_text(value: Any, *, nullable: bool = False, max_length: int = 4000, nonempty: bool = False) -> bool:
    if value is None:
        return nullable
    return (
        type(value) is str
        and len(value) <= max_length
        and (not nonempty or bool(value))
    )


def _is_number(value: Any, *, nullable: bool = False, minimum: float | None = None, maximum: float | None = None) -> bool:
    if value is None:
        return nullable
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def _is_bool(value: Any, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return type(value) is bool


def _is_int(value: Any, *, nullable: bool = False, minimum: int | None = None, maximum: int | None = None) -> bool:
    if value is None:
        return nullable
    if type(value) is not int:
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def _is_text_list(value: Any, *, max_length: int = 200, item_max_length: int = 4000) -> bool:
    return (
        type(value) is list
        and len(value) <= max_length
        and all(_is_text(item, max_length=item_max_length) for item in value)
    )


def _is_hash(value: Any, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return type(value) is str and bool(_HASH_RE.fullmatch(value))


def _is_timestamp(value: Any, *, nullable: bool = False, allow_empty: bool = False) -> bool:
    if value == "" and allow_empty:
        return True
    if value is None:
        return nullable
    if not _is_text(value, max_length=64, nonempty=True):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _check_dict(value: Any, fields: frozenset[str]) -> bool:
    return type(value) is dict and not (set(value) - fields)


def _validate_request(request: dict[str, Any]) -> None:
    if not _is_text(request.get("repo"), max_length=4096, nonempty=True):
        raise ValueError
    if not _is_text(request.get("base"), max_length=512, nonempty=True) or not _is_text(request.get("head"), max_length=512, nonempty=True):
        raise ValueError
    if "review_provider" in request and request["review_provider"] not in {None, "mock", "live"}:
        raise ValueError
    if "provider_model" in request and not _is_text(request["provider_model"], nullable=True, max_length=512):
        raise ValueError
    if "provider_timeout" in request and not _is_number(request["provider_timeout"], nullable=True, minimum=0.1, maximum=300):
        raise ValueError
    for field in ("index_repository", "assemble_review_packet", "allow_patch_suggestions"):
        if field in request and not _is_bool(request[field]):
            raise ValueError
    if "max_findings" in request and not _is_int(request["max_findings"], minimum=1, maximum=200):
        raise ValueError


def _validate_status(status: dict[str, Any]) -> None:
    required = {"run_id", "status", "repository", "base", "head", "created_at"}
    if not required <= set(status):
        raise ValueError
    if not _is_text(status["run_id"], max_length=128, nonempty=True) or status["status"] not in _LIFECYCLE_STATES:
        raise ValueError
    if not _is_text(status["repository"], max_length=4096, nonempty=True):
        raise ValueError
    if not _is_text(status["base"], max_length=512, nonempty=True) or not _is_text(status["head"], max_length=512, nonempty=True):
        raise ValueError
    for field in ("base_commit", "head_commit", "retry_of"):
        if field in status and not _is_text(status[field], nullable=True, max_length=128):
            raise ValueError
    if "progress_text" in status and not _is_text(status["progress_text"], max_length=4000):
        raise ValueError
    if "policy_decision" in status and not _is_text(status["policy_decision"], max_length=128):
        raise ValueError
    if "finding_counts" in status:
        counts = status["finding_counts"]
        if type(counts) is not dict or set(counts) - {"total", "blocker", "high", "medium", "low", "info", "abstained"}:
            raise ValueError
        if not all(_is_int(value, minimum=0, maximum=200) for value in counts.values()):
            raise ValueError
    for field in ("test_status", "full_suite_status"):
        if field in status and not _is_text(status[field], nullable=True, max_length=128):
            raise ValueError
    if "patch_validation_status" in status and not _is_text(status["patch_validation_status"], max_length=128):
        raise ValueError
    if "truncated" in status and not _is_bool(status["truncated"]):
        raise ValueError
    if "limitations" in status and not _is_text_list(status["limitations"], max_length=20):
        raise ValueError
    if "errors" in status and (
        type(status["errors"]) is not list
        or any(type(item) is not str or item not in _SAFE_ERROR_CODES for item in status["errors"])
    ):
        raise ValueError
    if "created_at" in status and not _is_timestamp(status["created_at"]):
        raise ValueError
    if "completed_at" in status and not _is_timestamp(status["completed_at"], allow_empty=True):
        raise ValueError
    if "recovery_state" in status and status["recovery_state"] not in _RECOVERY_STATES:
        raise ValueError


def _validate_operation(operation: Any, findings: set[str], proposals: set[str] | None = None) -> None:
    if operation is None:
        return
    if not _check_dict(operation, OPERATION_FIELDS) or operation.get("name") not in _OPERATION_NAMES:
        raise ValueError
    for field in ("proposal_id", "finding_id"):
        if field in operation and not _is_text(operation[field], max_length=256):
            raise ValueError
    if operation.get("finding_id") and operation["finding_id"] not in findings:
        raise ValueError
    if operation.get("proposal_id") and proposals is not None and operation["proposal_id"] not in proposals:
        raise ValueError


def _validate_finding(payload: dict[str, Any], finding_id: str) -> None:
    if not _is_text(payload.get("id"), max_length=128, nonempty=True) or payload["id"] != finding_id:
        raise ValueError
    text_fields = {
        "id", "category", "claim", "file", "evidence_strength", "status", "impact", "quality_version",
        "abstention_reason", "duplicate_group_id", "quality_decision", "fixability",
    }
    if not all(_is_text(payload[key], nullable=key in {"abstention_reason", "duplicate_group_id"}) for key in text_fields & payload.keys()):
        raise ValueError
    if payload.get("severity") not in _FINDING_SEVERITIES or payload.get("evidence_strength") not in _EVIDENCE_STRENGTHS:
        raise ValueError
    if "status" in payload and payload["status"] not in {"detected", "review_only", "suggested", "validated", "rejected", "abstained"}:
        raise ValueError
    if "quality_decision" in payload and payload["quality_decision"] not in _QUALITY_DECISIONS:
        raise ValueError
    if "fixability" in payload and payload["fixability"] not in {
        "unknown", "not_fixable", "review_required", "suggested", "validated",
    }:
        raise ValueError
    if "feedback" in payload and payload["feedback"] not in _FEEDBACK_LABELS | {None}:
        raise ValueError
    for field in ("line", "start_line", "end_line"):
        if field in payload and not _is_int(payload[field], minimum=1):
            raise ValueError
    if "start_line" in payload and "end_line" in payload and payload["end_line"] < payload["start_line"]:
        raise ValueError
    for field in ("confidence", "deterministic_support", "reviewer_support", "changed_line_support",
                  "repository_context_support", "test_support", "ambiguity_score", "truncation_penalty", "quality_score"):
        if field in payload and not _is_number(payload[field], minimum=0, maximum=1):
            raise ValueError
    if "unsupported_flow" in payload and not _is_bool(payload["unsupported_flow"]):
        raise ValueError
    for field in ("evidence", "reviewer_evidence", "tools_consulted", "evidence_sources", "limitations",
                  "suppressed_finding_ids", "quality_limitations"):
        if field in payload and not _is_text_list(payload[field], max_length=200):
            raise ValueError


def _validate_feedback(payload: dict[str, Any]) -> None:
    if "label" not in payload or payload["label"] not in _FEEDBACK_LABELS | {None}:
        raise ValueError
    if not _is_bool(payload.get("dismissed")):
        raise ValueError


def _validate_validation_payload(
    payload: dict[str, Any], *, proposal_id: str, patch_hash: str, base_commit: str, run_id: str,
) -> None:
    text_fields = {
        "proposal_id", "base_commit", "type_check_status", "tests_status", "build_status",
        "security_check_status", "sandbox_id", "resulting_diff_hash", "cleanup_status", "test_runner",
        "network_policy_requested", "full_suite_status", "approval_scope",
    }
    if not _check_dict(payload, VALIDATION_FIELDS | {"errors", "warnings"}):
        raise ValueError
    for field in text_fields & payload.keys():
        nullable = field in {"sandbox_id", "resulting_diff_hash", "cleanup_status", "test_runner", "network_policy_requested", "full_suite_status", "approval_scope"}
        if not _is_text(payload[field], nullable=nullable, max_length=512):
            raise ValueError
    if payload.get("proposal_id") != proposal_id or payload.get("base_commit") != base_commit:
        raise ValueError
    if payload.get("patch_hash") != patch_hash or not _is_hash(payload.get("patch_hash")):
        raise ValueError
    if payload.get("resulting_diff_hash") is not None and not _is_hash(payload["resulting_diff_hash"]):
        raise ValueError
    if payload.get("approval_scope") is not None and payload["approval_scope"] != run_id:
        raise ValueError
    for field in ("valid", "applies_cleanly", "execution_allowed", "test_execution_attempted",
                  "network_allowed", "dependency_install_allowed", "full_suite_requested",
                  "full_suite_policy_opted_in", "patch_applied_in_isolated_sandbox"):
        if field in payload and not _is_bool(payload[field]):
            raise ValueError
    for field in ("syntax_valid", "approval_verified", "test_output_truncated",
                  "network_policy_enforced", "network_isolation_verified"):
        if field in payload and not _is_bool(payload[field], nullable=True):
            raise ValueError
    for field in ("duration_ms", "test_duration_ms"):
        if field in payload and not _is_number(payload[field], nullable=True, minimum=0):
            raise ValueError
    if "test_exit_code" in payload and not _is_int(payload["test_exit_code"], nullable=True):
        raise ValueError
    for field in ("test_failure_count", "test_pass_count", "test_skip_count"):
        if field in payload and not _is_int(payload[field], nullable=True, minimum=0):
            raise ValueError
    for field in ("changed_files", "rejected_files", "tests_run"):
        if field in payload and not _is_text_list(payload[field], max_length=200, item_max_length=512):
            raise ValueError
    for field in ("errors", "warnings"):
        if field in payload and (
            type(payload[field]) is not list
            or any(type(item) is not str or item not in (_SAFE_ERROR_CODES | _SAFE_WARNING_CODES) for item in payload[field])
        ):
            raise ValueError


def _validate_apply_state(payload: dict[str, Any], patch_hash: str) -> None:
    if not _check_dict(payload, APPLY_FIELDS) or payload.get("patch_hash") != patch_hash:
        raise ValueError
    if "status" in payload and payload["status"] not in {"not_applied", "applied", "reverted"}:
        raise ValueError
    if "patch_hash" in payload and not _is_hash(payload["patch_hash"]):
        raise ValueError
    for field in ("head_commit", "branch_ref"):
        if field in payload and not _is_text(payload[field], max_length=512):
            raise ValueError
    for field in ("applied_at", "reverted_at"):
        if field in payload and not _is_timestamp(payload[field], nullable=True):
            raise ValueError
    for field in ("resulting_diff_hash", "validated_resulting_diff_hash"):
        if field in payload and not _is_hash(payload[field], nullable=True):
            raise ValueError
    if "files_changed" in payload and not _is_text_list(payload["files_changed"], max_length=20, item_max_length=512):
        raise ValueError


def _validate_apply_event(payload: dict[str, Any], *, run_id: str, finding_id: str, proposal_id: str, patch_hash: str) -> None:
    if not _check_dict(payload, frozenset(FixApplyEvent.model_fields)):
        raise ValueError
    if payload.get("event") not in {"applied", "reverted", "apply_rejected", "revert_failed"}:
        raise ValueError
    if (payload.get("run_id") != run_id or payload.get("finding_id") != finding_id
            or payload.get("proposal_id") != proposal_id or payload.get("patch_hash") != patch_hash):
        raise ValueError
    if not _is_hash(payload.get("patch_hash")) or not _is_hash(payload.get("resulting_diff_hash"), nullable=True):
        raise ValueError
    if not _is_text(payload.get("head_commit"), max_length=512) or not _is_text(payload.get("branch_ref"), max_length=512):
        raise ValueError
    if not _is_text_list(payload.get("files"), max_length=20, item_max_length=512):
        raise ValueError
    if payload.get("reason") is not None and payload["reason"] not in _SAFE_ERROR_CODES:
        raise ValueError
    if not _is_timestamp(payload.get("at")):
        raise ValueError


def _validate_fix_payload(payload: dict[str, Any], *, run_id: str, finding_id: str, proposal_id: str, patch_hash: str, base_commit: str) -> None:
    if not _check_dict(payload, FIX_FIELDS | {"validation_result", "apply_state", "recovery_state"}):
        raise ValueError
    text_fields = {
        "proposal_id", "finding_id", "run_id", "repository", "base_commit", "head_commit", "diagnosis",
        "explanation", "expected_behavior_change", "risk_level", "evidence_strength", "quality_decision",
        "generation_status", "rejection_reason", "rejection_explanation", "created_at", "schema_version",
        "validation_operation", "validation_status", "recovery_state",
    }
    for field in text_fields & payload.keys():
        nullable = field in {"rejection_reason", "rejection_explanation"}
        if not _is_text(payload[field], nullable=nullable, max_length=4000):
            raise ValueError
    if (payload.get("proposal_id") != proposal_id or payload.get("finding_id") != finding_id
            or payload.get("run_id") != run_id or payload.get("base_commit") != base_commit
            or payload.get("patch_hash") != patch_hash or not _is_hash(payload.get("patch_hash"))):
        raise ValueError
    if not _is_hash(patch_hash) or not _is_text(payload.get("head_commit"), max_length=128, nonempty=True):
        raise ValueError
    if payload.get("evidence_strength") not in _EVIDENCE_STRENGTHS or payload.get("quality_decision") not in _QUALITY_DECISIONS:
        raise ValueError
    if payload.get("generation_status") not in {"not_eligible", "generating", "generation_failed", "rejected_by_policy", "draft_ready", "rejected", "regeneration_requested", "applied"}:
        raise ValueError
    if payload.get("validation_operation") != "validate" or payload.get("validation_status") not in {
        "not_requested", "approval_required", "approved_for_validation", "validating", "applied_in_isolated_worktree",
        "tests_running", "validated", "applied", "reverted", "validation_failed", "cleanup_failed",
    }:
        raise ValueError
    if payload.get("recovery_state") not in _RECOVERY_STATES:
        raise ValueError
    if not _is_text(payload.get("patch_text"), max_length=128000, nonempty=True) or len(payload["patch_text"].encode()) > 64_000:
        raise ValueError
    if compute_patch_hash(payload["patch_text"]) != patch_hash or not repair_payload_is_safe(payload["patch_text"]):
        raise ValueError
    if not _is_bool(payload.get("approval_required")) or payload["approval_required"] is not True:
        raise ValueError
    if not _is_number(payload.get("confidence"), minimum=0, maximum=1):
        raise ValueError
    if not _is_text_list(payload.get("target_files"), max_length=8, item_max_length=512):
        raise ValueError
    for field in ("assumptions", "evidence_sources", "limitations", "validation_history"):
        if field in payload and not _is_text_list(payload[field], max_length=50):
            raise ValueError
    if "created_at" in payload and not _is_timestamp(payload["created_at"], allow_empty=True):
        raise ValueError
    if payload.get("validation_result") is not None:
        _validate_validation_payload(
            payload["validation_result"], proposal_id=proposal_id, patch_hash=patch_hash,
            base_commit=payload["head_commit"], run_id=run_id,
        )
    if payload.get("apply_state") is not None:
        _validate_apply_state(payload["apply_state"], patch_hash)


def _validate_patch_payload(payload: dict[str, Any], *, proposal_id: str, finding_id: str, patch_hash: str, base_commit: str) -> None:
    if not _check_dict(payload, PATCH_FIELDS):
        raise ValueError
    if (payload.get("proposal_id") != proposal_id or payload.get("finding_id") != finding_id
            or payload.get("base_commit") != base_commit or payload.get("patch_hash") != patch_hash
            or not _is_hash(payload.get("patch_hash"))):
        raise ValueError
    for field in ("provider_name", "provider_version", "base_commit", "rationale", "expected_behavior", "risk_level", "requested_action", "status", "created_at"):
        if not _is_text(payload.get(field), max_length=4000, nonempty=True):
            raise ValueError
    if payload.get("status") not in _PATCH_STATUSES or payload.get("requested_action") != "validate":
        raise ValueError
    if not _is_bool(payload.get("approval_required")) or payload["approval_required"] is not True:
        raise ValueError
    if not _is_text_list(payload.get("target_files"), max_length=8, item_max_length=512):
        raise ValueError
    if not _is_text(payload.get("unified_diff"), max_length=128000, nonempty=True) or len(payload["unified_diff"].encode()) > 64_000:
        raise ValueError
    if compute_patch_hash(payload["unified_diff"]) != patch_hash or not repair_payload_is_safe(payload["unified_diff"]):
        raise ValueError
    if not _is_text_list(payload.get("limitations"), max_length=50) or not _is_timestamp(payload.get("created_at")):
        raise ValueError


def _validate_manifest(manifest: dict[str, Any], *, snapshot_path: str, snapshot_id: str, status: dict[str, Any]) -> None:
    if not _check_dict(manifest, MANIFEST_FIELDS) or not {"run_id", "repository", "base_ref", "head_ref"} <= set(manifest):
        raise ValueError
    if not _is_text(manifest.get("run_id"), max_length=128, nonempty=True) or not _is_text(manifest.get("repository"), max_length=4096, nonempty=True):
        raise ValueError
    if not _is_text(manifest.get("base_ref"), max_length=512, nonempty=True) or not _is_text(manifest.get("head_ref"), max_length=512, nonempty=True):
        raise ValueError
    if manifest["base_ref"] != status.get("base") or manifest["head_ref"] != status.get("head"):
        raise ValueError
    if Path(manifest["repository"]).expanduser().resolve() != Path(snapshot_path).expanduser().resolve():
        raise ValueError
    if repository_id(manifest["repository"]) != snapshot_id:
        raise ValueError
    for field in ("base_commit", "head_commit"):
        if not _is_text(manifest[field], nullable=True, max_length=128):
            raise ValueError
        if manifest[field] != status.get(field):
            raise ValueError


def validation_projection(value: dict[str, Any]) -> dict[str, Any]:
    result = project(value, VALIDATION_FIELDS)
    # Arbitrary diagnostics are not a metadata contract, even after redaction.
    # A small fixed-code subset is retained for recovery and UI state.
    result["errors"] = _safe_error_list(value.get("errors"), fallback="validation_failed")
    result["warnings"] = ["validation_limitations_omitted"] if value.get("warnings") else []
    return result


def proposal_projection(value: dict[str, Any]) -> dict[str, Any]:
    patch = value.get("patch_text", "")
    if (not isinstance(patch, str) or len(patch.encode()) > 64_000
            or not repair_payload_is_safe(patch) or compute_patch_hash(patch) != value.get("patch_hash")):
        raise PersistenceError("persisted_patch_invalid")
    result = project(value, FIX_FIELDS)
    # Preserve exact patch bytes. Generic text bounding must never edit a patch.
    result["patch_text"] = patch
    result["approval_required"] = True
    if value.get("validation_result") is not None:
        result["validation_result"] = validation_projection(value["validation_result"])
    return result


def _apply_event_projection(value: dict[str, Any]) -> dict[str, Any]:
    result = project(value, frozenset(FixApplyEvent.model_fields))
    if result.get("reason") not in _SAFE_ERROR_CODES:
        result.pop("reason", None)
    return result


def make_snapshot(manager: Any, record: Any) -> dict[str, Any]:
    status = manager.get_review_status(record.run_id).model_dump(mode="json")
    status["errors"] = _safe_review_errors(record)
    status["progress_text"] = "Review " + record.status
    status = project(status, REVIEW_FIELDS)
    counts = {"total": len(record.findings), "blocker": 0, "high": 0, "medium": 0,
              "low": 0, "info": 0, "abstained": 0}
    for finding in record.findings:
        severity = str(finding.get("severity", "info")).lower()
        if severity in counts:
            counts[severity] += 1
        if finding.get("status") == "abstained":
            counts["abstained"] += 1
    status["finding_counts"] = counts
    status["truncated"] = bool(status.get("truncated") or len(record.findings) > 200)
    request = project(record.request.model_dump(mode="json"), REQUEST_FIELDS)
    request["repo"] = str(record.repo_path.resolve())
    rid = repository_id(record.repo_path)
    entities: list[dict[str, Any]] = []

    def add(kind: str, key: str, payload: dict[str, Any], *, finding: str = "", proposal: str = "", patch_hash: str = "") -> None:
        entities.append({"kind": kind, "id": key, "finding_id": finding, "proposal_id": proposal,
                         "patch_hash": patch_hash, "payload": payload})

    for finding in record.findings[:200]:
        fid = str(finding["id"])
        add("findings", fid, project(finding, FINDING_FIELDS), finding=fid)
        add("finding_feedback", fid, {"label": record.feedback.get(fid), "dismissed": fid in record.dismissed_finding_ids}, finding=fid)
    valid_findings = {item["id"] for item in entities if item["kind"] == "findings"}
    for (run_id, pid), patch in manager._legacy_proposals.items():
        if run_id != record.run_id or patch.finding_id not in valid_findings:
            continue
        value = project(patch.model_dump(mode="json"), PATCH_FIELDS)
        value["unified_diff"] = patch.unified_diff
        add("patch_proposals", pid, value, finding=patch.finding_id, proposal=pid, patch_hash=patch.patch_hash)
        validation = manager._legacy_validations.get((run_id, pid))
        if validation is not None:
            value = validation_projection(validation.model_dump(mode="json"))
            add("validation_evidence", pid, value, finding=patch.finding_id, proposal=pid, patch_hash=patch.patch_hash)
            key = hashlib.sha256(canonical_json(value).encode()).hexdigest()
            add("validation_attempts", pid + ":" + key, value, finding=patch.finding_id, proposal=pid, patch_hash=patch.patch_hash)
    latest: dict[str, str] = {}
    for fix in manager._all_fix_records():
        response = fix.response
        if response.run_id != record.run_id or response.finding_id not in valid_findings:
            continue
        pid, fid, ph = response.proposal_id, response.finding_id, response.patch_hash
        proposal_payload = proposal_projection(response.model_dump(mode="json"))
        proposal_payload["recovery_state"] = response.recovery_state
        if fix.apply_state is not None:
            # Preserve only identity and outcome metadata. Exact pre-apply
            # bytes are deliberately session-local and never enter SQLite.
            apply_state = fix.apply_state
            proposal_payload["apply_state"] = {
                key: safe_leaf(apply_state.get(key))
                for key in (
                    "status", "patch_hash", "head_commit", "branch_ref", "applied_at",
                    "reverted_at", "resulting_diff_hash", "validated_resulting_diff_hash",
                    "files_changed",
                ) if key in apply_state
            }
        add("fix_proposals", pid, proposal_payload, finding=fid, proposal=pid, patch_hash=ph)
        if record.fix_states.get(fid, {}).get("proposal_id") == pid:
            latest[fid] = pid
        result = response.validation_result
        if result:
            value = validation_projection(result)
            key = hashlib.sha256(canonical_json(value).encode()).hexdigest()
            add("validation_attempts", pid + ":" + key, value, finding=fid, proposal=pid, patch_hash=ph)
            add("validation_evidence", pid, value, finding=fid, proposal=pid, patch_hash=ph)
        for index, entry in enumerate(fix.apply_events[-50:]):
            value = _apply_event_projection(entry.model_dump(mode="json"))
            kind = "revert_events" if entry.event in {"reverted", "revert_failed"} else "apply_events"
            add(kind, pid + ":" + str(index), value, finding=fid, proposal=pid, patch_hash=ph)
    manifest = record.result.manifest if record.result is not None else None
    if manifest is not None:
        provider = project({key: getattr(manifest, key, None) for key in PROVIDER_FIELDS}, PROVIDER_FIELDS)
        add("provider_metadata", "review", provider)
    elif record.persisted_provider:
        add("provider_metadata", "review", project(record.persisted_provider, PROVIDER_FIELDS))
    for index, report in enumerate(record.capability_snapshot):
        add("capability_snapshots", str(index), project(report, CAPABILITY_FIELDS))
    safe_error_codes = _safe_review_errors(record)
    if safe_error_codes:
        add("safe_errors", "review", {"code": safe_error_codes[0]})
    operation = project(record.pending_operation, OPERATION_FIELDS) if isinstance(record.pending_operation, dict) else None
    artifacts: dict[str, Any] = {}
    if record.result is not None:
        artifacts["manifest"] = project(record.result.manifest.model_dump(mode="json"), MANIFEST_FIELDS)
    return {"repository_id": rid, "repository_path": str(record.repo_path.resolve()), "run_id": record.run_id,
            "review": {"status": status, "request": request, "latest": latest, "operation": operation},
            "entities": entities, "artifacts": artifacts}


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    """Validate scope and an allowlist independently on writes AND reads."""
    try:
        if type(snapshot) is not dict or set(snapshot) != {"repository_id", "repository_path", "run_id", "review", "entities", "artifacts"}:
            raise ValueError
        if not _is_text(snapshot["repository_id"], max_length=72, nonempty=True) or not re.fullmatch(r"repo-[a-f0-9]{64}", snapshot["repository_id"]):
            raise ValueError
        if not _is_text(snapshot["repository_path"], max_length=4096, nonempty=True) or not _is_text(snapshot["run_id"], max_length=128, nonempty=True):
            raise ValueError
        if repository_id(snapshot["repository_path"]) != snapshot["repository_id"]:
            raise ValueError
        review = snapshot["review"]
        if type(review) is not dict or set(review) != {"status", "request", "latest", "operation"}:
            raise ValueError
        if type(review["status"]) is not dict or type(review["request"]) is not dict:
            raise ValueError
        if set(review["status"]) - REVIEW_FIELDS or set(review["request"]) - REQUEST_FIELDS:
            raise ValueError
        _validate_status(review["status"])
        _validate_request(review["request"])
        status = ReviewStatusResponse.model_validate(review["status"])
        request = ReviewCreateRequest.model_validate(review["request"])
        if (status.run_id != snapshot["run_id"] or repository_id(status.repository) != snapshot["repository_id"]
                or repository_id(request.repo) != snapshot["repository_id"]):
            raise ValueError
        base_commit = status.base_commit or ""
        head_commit = status.head_commit or ""
        if type(review["latest"]) is not dict or len(review["latest"]) > 200:
            raise ValueError
        if not all(_is_text(fid, max_length=128, nonempty=True) and _is_text(pid, max_length=256, nonempty=True)
                   for fid, pid in review["latest"].items()):
            raise ValueError
        if type(snapshot["entities"]) is not list or len(snapshot["entities"]) > 5000:
            raise ValueError
        if type(snapshot["artifacts"]) is not dict or set(snapshot["artifacts"]) - {"manifest"}:
            raise ValueError

        findings: set[str] = set()
        proposals: dict[str, dict[str, Any]] = {}
        for item in snapshot["entities"]:
            if type(item) is not dict or set(item) != {"kind", "id", "finding_id", "proposal_id", "patch_hash", "payload"}:
                raise ValueError
            if (not _is_text(item["kind"], max_length=64, nonempty=True) or item["kind"] not in KINDS
                    or not _is_text(item["id"], max_length=256, nonempty=True)
                    or not _is_text(item["finding_id"], max_length=128)
                    or not _is_text(item["proposal_id"], max_length=256)
                    or not _is_text(item["patch_hash"], max_length=64)):
                raise ValueError
            if type(item["payload"]) is not dict:
                raise ValueError
            if item["kind"] == "findings":
                if item["id"] in findings or item["finding_id"] != item["id"] or item["proposal_id"] or item["patch_hash"]:
                    raise ValueError
                findings.add(item["id"])
            if item["kind"] in {"fix_proposals", "patch_proposals"}:
                if not item["proposal_id"] or item["proposal_id"] != item["id"] or not _is_hash(item["patch_hash"]):
                    raise ValueError
                if item["proposal_id"] in proposals:
                    raise ValueError
                proposals[item["proposal_id"]] = item

        if proposals and (not _is_text(base_commit, max_length=256, nonempty=True)
                          or not _is_text(head_commit, max_length=256, nonempty=True)):
            raise ValueError
        _validate_operation(review["operation"], findings, set(proposals))
        if review["operation"] and review["operation"].get("proposal_id"):
            operation_proposal = proposals[review["operation"]["proposal_id"]]
            if (review["operation"].get("finding_id")
                    and review["operation"]["finding_id"] != operation_proposal["finding_id"]):
                raise ValueError

        manifest_data = snapshot["artifacts"].get("manifest")
        if manifest_data is not None:
            if type(manifest_data) is not dict:
                raise ValueError
            _validate_manifest(
                manifest_data, snapshot_path=snapshot["repository_path"],
                snapshot_id=snapshot["repository_id"], status=review["status"],
            )

        seen: set[tuple[str, str]] = set()
        for item in snapshot["entities"]:
            kind, payload = item["kind"], item["payload"]
            if (kind, item["id"]) in seen:
                raise ValueError
            seen.add((kind, item["id"]))
            if item["finding_id"] and item["finding_id"] not in findings:
                raise ValueError
            if kind == "finding_feedback" and item["id"] != item["finding_id"]:
                raise ValueError
            if item.get("proposal_id"):
                proposal = proposals[item["proposal_id"]]
                if item["finding_id"] != proposal["finding_id"] or item["patch_hash"] != proposal["patch_hash"]:
                    raise ValueError
            allowed = {
                "findings": FINDING_FIELDS, "finding_feedback": {"label", "dismissed"},
                "fix_proposals": FIX_FIELDS | {"validation_result", "apply_state", "recovery_state"},
                "validation_attempts": VALIDATION_FIELDS | {"errors", "warnings"},
                "validation_evidence": VALIDATION_FIELDS | {"errors", "warnings"},
                "apply_events": set(FixApplyEvent.model_fields), "revert_events": set(FixApplyEvent.model_fields),
                "provider_metadata": PROVIDER_FIELDS, "capability_snapshots": CAPABILITY_FIELDS,
                "safe_errors": {"code"}, "lifecycle_events": {"event", "status", "operation", "at"},
                "patch_proposals": PATCH_FIELDS,
            }[kind]
            if set(payload) - allowed:
                raise ValueError
            if kind == "findings":
                _validate_finding(payload, item["finding_id"])
            elif kind == "finding_feedback":
                _validate_feedback(payload)
            elif kind == "safe_errors":
                if set(payload) != {"code"} or payload["code"] not in _SAFE_ERROR_CODES:
                    raise ValueError
            elif kind == "provider_metadata":
                if not _check_dict(payload, PROVIDER_FIELDS):
                    raise ValueError
                for field in ("provider_name", "provider_version", "model_name"):
                    if field in payload and not _is_text(payload[field], nullable=True, max_length=512):
                        raise ValueError
                for field in ("provider_latency_ms", "provider_cost"):
                    if field in payload and not _is_number(payload[field], nullable=True, minimum=0):
                        raise ValueError
                if "provider_retries" in payload and not _is_int(payload["provider_retries"], nullable=True, minimum=0, maximum=1000):
                    raise ValueError
            elif kind == "capability_snapshots":
                if not _check_dict(payload, CAPABILITY_FIELDS):
                    raise ValueError
                for field in ("language", "language_version"):
                    if field in payload and not _is_text(payload[field], nullable=field == "language_version", max_length=128):
                        raise ValueError
                for field in CAPABILITY_FIELDS - {"language", "language_version", "limitations"}:
                    if field in payload and payload[field] not in _CAPABILITY_STATUSES:
                        raise ValueError
                if "limitations" in payload and not _is_text_list(payload["limitations"], max_length=32):
                    raise ValueError
            elif kind == "lifecycle_events":
                if not _check_dict(payload, frozenset({"event", "status", "operation", "at"})):
                    raise ValueError
                if not _is_text(payload.get("event"), max_length=128, nonempty=True) or payload.get("status") not in _LIFECYCLE_STATES:
                    raise ValueError
                if not _is_timestamp(payload.get("at")):
                    raise ValueError
                _validate_operation(payload.get("operation"), findings, set(proposals))
            elif kind == "patch_proposals":
                _validate_patch_payload(
                    payload, proposal_id=item["proposal_id"], finding_id=item["finding_id"],
                    patch_hash=item["patch_hash"], base_commit=head_commit,
                )
            elif kind == "fix_proposals":
                _validate_fix_payload(
                    payload, run_id=snapshot["run_id"], finding_id=item["finding_id"],
                    proposal_id=item["proposal_id"], patch_hash=item["patch_hash"],
                    base_commit=base_commit,
                )
                if payload.get("repository") != Path(snapshot["repository_path"]).name[:128]:
                    raise ValueError
            elif kind in {"validation_attempts", "validation_evidence"}:
                _validate_validation_payload(
                    payload, proposal_id=item["proposal_id"], patch_hash=item["patch_hash"],
                    base_commit=head_commit, run_id=snapshot["run_id"],
                )
            elif kind in {"apply_events", "revert_events"}:
                _validate_apply_event(
                    payload, run_id=snapshot["run_id"], finding_id=item["finding_id"],
                    proposal_id=item["proposal_id"], patch_hash=item["patch_hash"],
                )
            elif kind == "validation_attempts":
                raise ValueError
        for fid, pid in review["latest"].items():
            if pid not in proposals or proposals[pid]["finding_id"] != fid:
                raise ValueError
        if not repair_payload_is_safe(snapshot):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, OSError, RuntimeError, RecursionError):
        raise PersistenceError("persisted_identity_invalid") from None
