"""Patch proposal constructors, deterministic hashing, and approval token verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from .models import PatchProposal, PatchRedactionAudit, PatchStatus

_SECRET_PATTERNS = [
    re.compile(r"(?i)(AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"(?i)ghp_[A-Za-z0-9_]{36}"),
    re.compile(r"(?i)github_pat_[A-Za-z0-9_]{22}_[A-Za-z0-9_]{59}"),
    re.compile(r"(?i)(?:postgres|postgresql|mysql|mongodb|redis)://[^:\s]+:[^@\s]+@[^\s]+"),
    re.compile(r"(?i)(?:api[_-]?key|password|token|secret)\s*[:=]\s*['\"][A-Za-z0-9_\-+=/.~]{12,}['\"]"),
]


def normalize_diff(diff_text: str) -> str:
    """Normalize line endings and whitespace for deterministic hashing."""
    if not diff_text or not diff_text.strip():
        return ""
    lines = [line.rstrip() for line in diff_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    # Strip trailing empty lines
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def compute_patch_hash(diff_text: str) -> str:
    """Compute SHA-256 digest of normalized unified diff."""
    norm = normalize_diff(diff_text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def audit_patch_redaction(diff_text: str) -> PatchRedactionAudit:
    """Scan unified diff text for secrets, credentials, or sensitive tokens."""
    raw_matches = 0
    failed: list[str] = []

    for pat in _SECRET_PATTERNS:
        matches = pat.findall(diff_text)
        if matches:
            raw_matches += len(matches)
            failed.append(f"secret_pattern:{pat.pattern[:25]}")

    return PatchRedactionAudit(
        safe=raw_matches == 0,
        raw_value_matches=raw_matches,
        rules_applied=["regex_secret_scrubbing", "credential_pattern_check"],
        failed_checks=failed,
    )


def generate_approval_token(
    proposal_id: str,
    base_commit: str,
    patch_hash: str,
    allowed_paths: Sequence[str],
    run_id: str = "default",
    *,
    finding_id: str | None = None,
    repository_identity: str | None = None,
    repository: str | None = None,
    head_commit: str | None = None,
    operation: str | None = None,
) -> str:
    """Generate a scoped deterministic approval token for a patch operation.

    The original Phase 7 token shape remains stable for existing
    ``PatchProposal`` callers.  FixProposal validation supplies the additional
    identity fields and therefore receives the stronger Phase 11C-C scope.
    """
    paths_str = ",".join(sorted(p.replace("\\", "/").strip("/") for p in allowed_paths))
    repository_identity = repository_identity if repository_identity is not None else repository
    if any(value is not None for value in (finding_id, repository_identity, head_commit, operation)):
        if not all(value is not None and str(value).strip() for value in (
            finding_id, repository_identity, head_commit, operation,
        )):
            raise ValueError("Complete FixProposal validation scope is required")
        return generate_validation_approval_token(
            proposal_id=proposal_id,
            finding_id=str(finding_id),
            run_id=run_id,
            repository_identity=str(repository_identity),
            base_commit=base_commit,
            head_commit=str(head_commit),
            patch_hash=patch_hash,
            target_files=allowed_paths,
            operation=str(operation),
        )
    raw = f"{proposal_id}:{base_commit}:{patch_hash}:{paths_str}:{run_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"CAT-APP-{digest}"


def _validation_scope_payload(
    *,
    proposal_id: str,
    finding_id: str,
    run_id: str,
    repository_identity: str,
    base_commit: str,
    head_commit: str,
    patch_hash: str,
    target_files: Sequence[str],
    operation: str,
) -> str:
    """Canonical, labelled scope used by the FixProposal validation token."""
    paths = sorted(p.replace("\\", "/").strip("/") for p in target_files)
    return json.dumps(
        {
            "operation": operation,
            "proposal_id": proposal_id,
            "finding_id": finding_id,
            "run_id": run_id,
            "repository_identity": repository_identity,
            "base_commit": base_commit,
            "head_commit": head_commit,
            "patch_hash": patch_hash,
            "target_files": paths,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def generate_validation_approval_token(
    *,
    proposal_id: str,
    finding_id: str,
    run_id: str,
    repository_identity: str,
    base_commit: str,
    head_commit: str,
    patch_hash: str,
    target_files: Sequence[str],
    operation: str = "validate",
) -> str:
    """Generate the complete approval scope for a FixProposal operation.

    Only two operations exist: ``validate`` (run the proposal in an isolated
    sandbox worktree) and ``apply`` (Phase 11C-D, write the validated patch to
    the original workspace).  The operation is part of the hashed scope, so a
    validate token can never authorize an apply and vice versa.  This is a
    pure existing-mechanism helper for an operator or test harness; the service
    never mints this value as part of a provider response or HTTP response.
    """
    if operation not in {"validate", "apply"}:
        raise ValueError("FixProposal approval is only valid for operation 'validate' or 'apply'")
    raw = _validation_scope_payload(
        proposal_id=proposal_id,
        finding_id=finding_id,
        run_id=run_id,
        repository_identity=repository_identity,
        base_commit=base_commit,
        head_commit=head_commit,
        patch_hash=patch_hash,
        target_files=target_files,
        operation=operation,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"CAT-APP-{digest}"


def verify_validation_approval_token(
    token: str | None,
    *,
    proposal_id: str,
    finding_id: str,
    run_id: str,
    repository_identity: str,
    base_commit: str,
    head_commit: str,
    patch_hash: str,
    target_files: Sequence[str],
    operation: str = "validate",
) -> tuple[bool, str | None]:
    """Verify every FixProposal validation binding before sandbox creation."""
    if not token or not token.strip():
        return False, "Approval token is empty or missing"
    try:
        expected = generate_validation_approval_token(
            proposal_id=proposal_id,
            finding_id=finding_id,
            run_id=run_id,
            repository_identity=repository_identity,
            base_commit=base_commit,
            head_commit=head_commit,
            patch_hash=patch_hash,
            target_files=target_files,
            operation=operation,
        )
    except ValueError as err:
        return False, str(err)
    if not hmac.compare_digest(token.strip(), expected):
        return False, "Approval token does not match the FixProposal validation scope"
    return True, None


def verify_approval_token(
    token: str,
    proposal: PatchProposal,
    base_commit: str,
    run_id: str = "default",
    *,
    validation_scope: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Verify that an approval token matches the proposal scope, base commit, and patch hash."""
    if validation_scope is not None:
        if (validation_scope.get("proposal_id") != proposal.proposal_id
                or validation_scope.get("finding_id") != proposal.finding_id
                or validation_scope.get("run_id") != run_id
                or validation_scope.get("head_commit") != base_commit
                or validation_scope.get("patch_hash") != proposal.patch_hash
                or validation_scope.get("target_files") != proposal.target_files):
            return False, "Approval scope does not match the PatchProposal"
        return verify_validation_approval_token(token, **validation_scope)
    if not token or not token.strip():
        return False, "Approval token is empty or missing"

    clean_token = token.strip()
    expected = generate_approval_token(
        proposal_id=proposal.proposal_id,
        base_commit=base_commit,
        patch_hash=proposal.patch_hash,
        allowed_paths=proposal.target_files,
        run_id=run_id,
    )

    if not hmac.compare_digest(clean_token, expected):
        return (
            False,
            f"Approval token does not match expected token for proposal '{proposal.proposal_id}' "
            f"at base commit '{base_commit}'",
        )

    return True, None


def create_patch_proposal(
    *,
    finding_id: str,
    provider_name: str,
    provider_version: str,
    base_commit: str,
    target_files: Sequence[str],
    unified_diff: str,
    rationale: str,
    expected_behavior: str,
    risk_level: str = "low",
    requested_action: str = "validate",
    proposal_id: str | None = None,
    created_at: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> PatchProposal:
    """Construct a validated PatchProposal with hash and redaction checks."""
    norm_diff = normalize_diff(unified_diff)
    p_hash = compute_patch_hash(norm_diff)
    clean_target_files = [p.replace("\\", "/").strip("/") for p in target_files]

    if not proposal_id:
        seed = f"{finding_id}:{base_commit}:{p_hash}:{','.join(sorted(clean_target_files))}"
        proposal_id = f"prop-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"

    if not created_at:
        created_at = datetime.now(UTC).isoformat()

    redaction = audit_patch_redaction(norm_diff)

    return PatchProposal(
        proposal_id=proposal_id,
        finding_id=finding_id,
        provider_name=provider_name,
        provider_version=provider_version,
        base_commit=base_commit,
        target_files=clean_target_files,
        unified_diff=norm_diff,
        rationale=rationale,
        expected_behavior=expected_behavior,
        risk_level=risk_level,
        requested_action=requested_action,
        status=PatchStatus.PROPOSED,
        policy_decision=None,
        created_at=created_at,
        limitations=[] if redaction.safe else ["Patch contains potentially unredacted sensitive content"],
        provenance=provenance or {},
        patch_hash=p_hash,
        redaction_audit=redaction,
    )


__all__ = [
    "audit_patch_redaction",
    "compute_patch_hash",
    "create_patch_proposal",
    "generate_approval_token",
    "generate_validation_approval_token",
    "normalize_diff",
    "verify_approval_token",
    "verify_validation_approval_token",
]
