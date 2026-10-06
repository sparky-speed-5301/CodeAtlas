"""Comprehensive validation orchestrator for patch proposals."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from codeatlas.evidence import EvidenceLogger

from .apply import apply_patch_in_isolated_sandbox
from .models import PatchProposal, PatchStatus, PatchValidationResult
from .parser import parse_unified_diff, unsafe_diff_path_reason
from .policy import evaluate_patch_policy
from .proposal import compute_patch_hash


def _patch_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    section = config.get("patch")
    return section if isinstance(section, dict) else {}


def _emit(evidence: EvidenceLogger | None, event: str, **details: Any) -> None:
    if evidence is not None:
        evidence.emit(event, **details)


def validate_patch_proposal(
    proposal: PatchProposal,
    repository_root: Path,
    *,
    config: dict[str, Any] | None = None,
    approval_token: str | None = None,
    allow_isolated_apply: bool = True,
    evidence: EvidenceLogger | None = None,
    run_id: str = "default",
    retain_sandbox_on_failure: bool = False,
    run_tests: bool = False,
    run_full_suite: bool = False,
    test_timeout: float = 30.0,
    max_output_bytes: int = 100_000,
    test_config: dict[str, Any] | None = None,
    validation_scope: dict[str, Any] | None = None,
) -> PatchValidationResult:
    """Validate a patch proposal against schema, paths, policy, redaction, and isolated application.

    Guarantees:
    - Never modifies repository_root.
    - Rejects unauthorized or policy-violating proposals.
    - Applies only inside ephemeral isolated sandboxes.
    - Rejected or already-applied/validated proposals fail closed unless
      policy explicitly allows revalidation.
    """
    started = time.perf_counter()
    t_cfg = test_config or {}
    fs_opt_in = bool(
        t_cfg.get("allow_full_suite")
        or t_cfg.get("full_suite_opt_in")
        or (config.get("allow_full_suite") if isinstance(config, dict) else False)
        or (config.get("full_suite_opt_in") if isinstance(config, dict) else False)
        or (config.get("test", {}).get("allow_full_suite") if isinstance(config, dict) and isinstance(config.get("test"), dict) else False)
        or (config.get("test", {}).get("full_suite_opt_in") if isinstance(config, dict) and isinstance(config.get("test"), dict) else False)
    )

    def early(
        *,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
        valid: bool = False,
        rejected: bool = True,
        approval_verified: bool = False,
        security_failed: bool = False,
        changed_files: list[str] | None = None,
    ) -> PatchValidationResult:
        """Terminal pre-sandbox outcome with full report fields and evidence."""
        errors = errors or []
        warnings = warnings or []
        _emit(
            evidence,
            "patch_validation_requested",
            proposal_id=proposal.proposal_id,
            status="requested",
        )
        event = "patch_validation_rejected" if rejected else "patch_validation_completed"
        _emit(
            evidence,
            event,
            proposal_id=proposal.proposal_id,
            reason=(errors or warnings or ["ok"])[0],
            status="rejected" if rejected else ("ok" if valid else "failed"),
            valid=valid,
        )
        return PatchValidationResult(
            valid=valid,
            applies_cleanly=False,
            syntax_valid=None,
            proposal_id=proposal.proposal_id,
            approval_verified=approval_verified,
            approval_scope=run_id,
            patch_hash=proposal.patch_hash or None,
            base_commit=proposal.base_commit,
            network_isolation_verified=False,
            security_check_status="failed" if security_failed else "not_run",
            changed_files=changed_files or [],
            errors=errors,
            warnings=warnings or [],
            execution_allowed=False,
            cleanup_status="not_applicable",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            full_suite_requested=run_full_suite,
            full_suite_policy_opted_in=fs_opt_in,
            full_suite_status="blocked" if (run_full_suite and not valid) else ("not_run" if not run_full_suite else None),
            full_suite_blocked_reason=errors[0] if (errors and run_full_suite) else None,
        )

    # 0. Lifecycle state guards.
    patch_cfg = _patch_config(config)
    allow_revalidation = bool(patch_cfg.get("allow_revalidation", False))
    if proposal.status == PatchStatus.REJECTED:
        return early(errors=["Proposal has been rejected; no further transitions are allowed"])
    if proposal.status in {PatchStatus.APPLIED_IN_ISOLATED_WORKTREE, PatchStatus.VALIDATED} and not allow_revalidation:
        return early(errors=[
            f"Proposal status is '{proposal.status}'; isolated validation cannot run again "
            "unless policy explicitly allows revalidation"
        ])

    # 1. Parse unified diff
    parsed_files, parse_errors = parse_unified_diff(proposal.unified_diff)
    if parse_errors:
        proposal.status = PatchStatus.REJECTED
        proposal.policy_decision = {
            "allowed": False,
            "decision": PatchStatus.REJECTED,
            "reasons": parse_errors,
            "rules_evaluated": ["diff_parser_validation"],
        }
        return early(errors=parse_errors)

    # 1b. Both diff header sides must stay inside the repository.
    unsafe_path = unsafe_diff_path_reason(proposal.unified_diff)
    if unsafe_path:
        proposal.status = PatchStatus.REJECTED
        proposal.policy_decision = {
            "allowed": False,
            "decision": PatchStatus.REJECTED,
            "reasons": [unsafe_path],
            "rules_evaluated": ["diff_header_path_safety"],
        }
        return early(errors=[unsafe_path])

    # 2. Verify redaction safety
    if not proposal.redaction_audit.safe:
        proposal.status = PatchStatus.REJECTED
        proposal.policy_decision = {
            "allowed": False,
            "decision": PatchStatus.REJECTED,
            "reasons": ["Patch contains unredacted credentials or sensitive tokens"],
            "rules_evaluated": ["patch_redaction_safe"],
        }
        return early(
            errors=["Patch contains unredacted credentials or sensitive tokens"],
            security_failed=True,
        )

    # 3. Policy evaluation
    policy_dec = evaluate_patch_policy(proposal, parsed_files, config=config)
    proposal.policy_decision = policy_dec.model_dump()

    if policy_dec.decision == PatchStatus.REJECTED:
        proposal.status = PatchStatus.REJECTED
        return early(errors=policy_dec.reasons)

    # 4. Hash integrity
    computed_hash = compute_patch_hash(proposal.unified_diff)
    if proposal.patch_hash and computed_hash != proposal.patch_hash:
        proposal.status = PatchStatus.FAILED_VALIDATION
        return early(errors=[
            f"Patch hash integrity check failed: expected '{proposal.patch_hash}', got '{computed_hash}'"
        ])

    # Update proposal status according to policy
    if policy_dec.decision == PatchStatus.REQUIRES_HUMAN_APPROVAL:
        proposal.status = PatchStatus.REQUIRES_HUMAN_APPROVAL
    elif policy_dec.decision == PatchStatus.APPROVED:
        proposal.status = PatchStatus.APPROVED

    # 5. Isolated sandbox application
    if not allow_isolated_apply:
        return early(
            valid=True,
            rejected=False,
            warnings=["Isolated application skipped by request"],
            changed_files=[f.path for f in parsed_files],
        )

    # If approval is required and no approval token was supplied:
    if proposal.status == PatchStatus.REQUIRES_HUMAN_APPROVAL and not approval_token:
        if run_tests or run_full_suite:
            err_msg = "Patch application requires explicit human approval token"
            return early(
                valid=False,
                rejected=True,
                errors=[err_msg],
                changed_files=[f.path for f in parsed_files],
            )
        warning = (
            "Patch is syntactically valid and satisfies policy gates, but requires human approval token for isolated application"
        )
        return early(
            valid=True,
            rejected=False,
            warnings=[warning],
            changed_files=[f.path for f in parsed_files],
        )

    # Execute isolated application in detached sandbox
    apply_result = apply_patch_in_isolated_sandbox(
        proposal,
        repository_root,
        approval_token=approval_token,
        config=config,
        evidence=evidence,
        run_id=run_id,
        retain_sandbox_on_failure=retain_sandbox_on_failure,
        run_tests=run_tests,
        run_full_suite=run_full_suite,
        test_timeout=test_timeout,
        max_output_bytes=max_output_bytes,
        test_config=test_config,
        validation_scope=validation_scope,
    )

    if proposal.status not in {PatchStatus.VALIDATED, PatchStatus.FAILED_VALIDATION}:
        if apply_result.valid:
            proposal.status = PatchStatus.VALIDATED
        else:
            proposal.status = PatchStatus.FAILED_VALIDATION

    return apply_result


__all__ = [
    "validate_patch_proposal",
]
