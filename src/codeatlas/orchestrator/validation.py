"""Attach existing isolated validation results to human-review artifacts.

This consumes the trusted in-process validation report; it never runs tests or
accepts provider-supplied observations as execution evidence.
"""

from __future__ import annotations

from pydantic import ValidationError

from codeatlas.evidence import EvidenceLogger
from codeatlas.patching.models import PatchProposal, PatchStatus, PatchValidationResult
from codeatlas.review.packet import ReviewPacket, attach_observed_test_evidence
from codeatlas.review.policy import evaluate_policy
from codeatlas.verification.models import TestResult
from .manifest import RunManifest


def build_validation_review(
    proposal: PatchProposal,
    validation: PatchValidationResult,
    *,
    repository: str,
    run_id: str = "default",
    packet: ReviewPacket | None = None,
    evidence: EvidenceLogger | None = None,
) -> tuple[ReviewPacket, RunManifest]:
    """Build a packet/manifest for human review using the same attachment gate."""
    packet = packet or ReviewPacket(
        packet_id=f"pkt-{proposal.proposal_id}",
        repository=repository,
        base_commit=proposal.base_commit,
        changed_files=list(proposal.target_files),
        proposal_id=proposal.proposal_id,
        sandbox_id=validation.sandbox_id,
        limitations=["Validation review contains proposal metadata and observed tests; source context requires the original review packet."],
    )
    result = validation.test_result
    plan = None
    invalid_record = False
    if validation.full_suite_result is not None:
        try:
            result = TestResult.model_validate(validation.full_suite_result)
        except ValidationError:
            result = None
            invalid_record = True
        plan = validation.full_suite_test_plan
    scope_matches = validation.approval_scope == run_id
    if not scope_matches:
        validation = validation.model_copy(update={"approval_verified": False})
    packet = attach_observed_test_evidence(packet, proposal, result, validation, test_plan=plan)
    if invalid_record:
        packet.test_evidence_exclusion_reason = "unbounded_or_invalid_result"
    observed = packet.observed_test_evidence
    # Findings remain unchanged: a green test alone does not reproduce the
    # linked finding or establish its semantics under existing evidence rules.
    if observed and observed.status in {"failed", "timed_out", "error"}:
        proposal.status = PatchStatus.FAILED_VALIDATION
    decision = evaluate_policy(packet, packet.deterministic_findings, config=packet.configuration)
    packet.policy_summary = decision.model_dump(mode="json")
    summary = f"targeted={packet.tests_status}; full_suite={packet.full_suite_status}; evidence="
    if observed:
        summary += (
            f"{observed.status}; runner={observed.runner}; scope={'full-suite' if observed.full_suite else 'targeted'}; "
            f"passed={observed.tests_passed}, failed={observed.tests_failed}, skipped={observed.tests_skipped}; "
            f"duration_ms={observed.duration_ms:.2f}; redaction=passed; output_truncated={str(observed.output_truncated).lower()}"
        )
    else:
        summary += f"excluded ({packet.test_evidence_exclusion_reason})"
    limits = list(dict.fromkeys(packet.limitations + (observed.limitations if observed else [])))
    approval_bound = (
        validation.approval_verified is True
        and validation.proposal_id == proposal.proposal_id
        and validation.base_commit == proposal.base_commit
        and validation.patch_hash == proposal.patch_hash
    )
    manifest = RunManifest(
        run_id=run_id, repository=repository,
        base_ref=proposal.base_commit, head_ref=proposal.base_commit,
        base_commit=proposal.base_commit,
        proposal_id=proposal.proposal_id, patch_hash=proposal.patch_hash or None,
        approval_scope=validation.approval_scope,
        approval_operation="validate",
        approval_verified=approval_bound,
        sandbox_id=validation.sandbox_id,
        test_evidence_attached=observed is not None,
        test_evidence_summary=summary,
        validation_limitations=limits,
        network_isolation_verified=False,
        network_policy_requested=observed.network_policy_requested if observed else validation.network_policy_requested,
        network_policy_enforced=observed.network_policy_enforced if observed else validation.network_policy_enforced,
        tests_status=packet.tests_status, full_suite_status=packet.full_suite_status,
        full_suite_requested=validation.full_suite_requested,
        full_suite_policy_opted_in=validation.full_suite_policy_opted_in,
        tests_run=observed.targets if observed else [],
        test_runner=observed.runner if observed else None,
        test_duration_ms=observed.duration_ms if observed else None,
        test_pass_count=observed.tests_passed if observed else None,
        test_failure_count=observed.tests_failed if observed else None,
        test_skip_count=observed.tests_skipped if observed else None,
        test_output_truncated=observed.output_truncated if observed else None,
        diagnostic_summary=observed.failure_summary if observed else None,
        failed_test_names=observed.failed_test_names if observed else [],
        diagnostic_limitations=observed.limitations if observed else [],
        test_output_redaction_audit=observed.redaction_audit.model_dump() if observed else None,
        execution_allowed=validation.execution_allowed,
        applies_cleanly=validation.applies_cleanly, syntax_valid=validation.syntax_valid,
        resulting_diff_hash=validation.resulting_diff_hash,
        cleanup_status=validation.cleanup_status,
        isolated_validation_attempted=validation.sandbox_id is not None,
        isolated_validation_status=proposal.status,
        review_packet_id=packet.packet_id,
        packet_limitations=packet.limitations,
        policy_decisions=[decision.model_dump(mode="json")],
        patch_proposal_ids=[proposal.proposal_id], patch_proposal_statuses=[proposal.status],
        automatic_application_attempted=False,
    )
    if evidence is not None:
        evidence.emit(
            "observed_test_evidence_attached" if observed else "observed_test_evidence_excluded",
            proposal_id=proposal.proposal_id, sandbox_id=validation.sandbox_id,
            status=observed.status if observed else "excluded",
            reason=packet.test_evidence_exclusion_reason,
            network_isolation_verified=False,
        )
    return packet, manifest
