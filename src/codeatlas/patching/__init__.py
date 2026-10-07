"""Patch planning, proposal modeling, policy gating, and isolated sandbox validation."""

from __future__ import annotations

from .apply import (
    WorktreeApplyResult,
    apply_patch_in_isolated_sandbox,
    apply_patch_to_worktree,
    capture_worktree_file_state,
    restore_worktree_files,
)
from .mock import MockFixer
from .models import (
    VALID_STATUS_TRANSITIONS,
    PatchFile,
    PatchHunk,
    PatchProposal,
    PatchRedactionAudit,
    PatchStatus,
    PatchValidationResult,
)
from .parser import parse_unified_diff
from .policy import (
    DEFAULT_PATCH_CONFIG,
    PatchPolicyDecision,
    evaluate_patch_policy,
    is_dependency_manifest_path,
    is_lockfile_path,
    is_workflow_path,
)
from .proposal import (
    audit_patch_redaction,
    compute_patch_hash,
    create_patch_proposal,
    generate_approval_token,
    generate_validation_approval_token,
    normalize_diff,
    verify_approval_token,
    verify_validation_approval_token,
)
from .sandbox import PatchSandbox, temporary_patch_sandbox
from .validator import validate_patch_proposal

__all__ = [
    "DEFAULT_PATCH_CONFIG",
    "VALID_STATUS_TRANSITIONS",
    "MockFixer",
    "PatchFile",
    "PatchHunk",
    "PatchPolicyDecision",
    "PatchProposal",
    "PatchRedactionAudit",
    "PatchSandbox",
    "PatchStatus",
    "PatchValidationResult",
    "apply_patch_in_isolated_sandbox",
    "audit_patch_redaction",
    "compute_patch_hash",
    "create_patch_proposal",
    "evaluate_patch_policy",
    "generate_approval_token",
    "generate_validation_approval_token",
    "is_dependency_manifest_path",
    "is_lockfile_path",
    "is_workflow_path",
    "normalize_diff",
    "parse_unified_diff",
    "temporary_patch_sandbox",
    "validate_patch_proposal",
    "verify_approval_token",
    "verify_validation_approval_token",
]
