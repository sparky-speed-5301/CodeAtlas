"""Patch planning, proposal modeling, policy gating, and isolated sandbox validation."""

from __future__ import annotations

from .apply import apply_patch_in_isolated_sandbox
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
    normalize_diff,
    verify_approval_token,
)
from .sandbox import PatchSandbox, temporary_patch_sandbox
from .validator import validate_patch_proposal

__all__ = [
    "PatchStatus",
    "VALID_STATUS_TRANSITIONS",
    "PatchHunk",
    "PatchFile",
    "PatchRedactionAudit",
    "PatchValidationResult",
    "PatchProposal",
    "normalize_diff",
    "compute_patch_hash",
    "audit_patch_redaction",
    "generate_approval_token",
    "verify_approval_token",
    "create_patch_proposal",
    "parse_unified_diff",
    "DEFAULT_PATCH_CONFIG",
    "is_workflow_path",
    "is_lockfile_path",
    "is_dependency_manifest_path",
    "PatchPolicyDecision",
    "evaluate_patch_policy",
    "PatchSandbox",
    "temporary_patch_sandbox",
    "apply_patch_in_isolated_sandbox",
    "validate_patch_proposal",
    "MockFixer",
]
