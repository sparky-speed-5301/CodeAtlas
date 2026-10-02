"""Patch proposal constructors, deterministic hashing, and approval token verification."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timezone
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
) -> str:
    """Generate a scoped deterministic approval token for a specific patch proposal."""
    paths_str = ",".join(sorted(p.replace("\\", "/").strip("/") for p in allowed_paths))
    raw = f"{proposal_id}:{base_commit}:{patch_hash}:{paths_str}:{run_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"CAT-APP-{digest}"


def verify_approval_token(
    token: str,
    proposal: PatchProposal,
    base_commit: str,
    run_id: str = "default",
) -> tuple[bool, str | None]:
    """Verify that an approval token matches the proposal scope, base commit, and patch hash."""
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

    if clean_token != expected:
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
        created_at = datetime.now(timezone.utc).isoformat()

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
    "normalize_diff",
    "compute_patch_hash",
    "audit_patch_redaction",
    "generate_approval_token",
    "verify_approval_token",
    "create_patch_proposal",
]
