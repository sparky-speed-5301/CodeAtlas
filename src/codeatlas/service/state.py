"""State management, lifecycle tracking, and review orchestration for CodeAtlas service."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import threading
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from codeatlas.adapters import select_language_adapter
from codeatlas.findings.models import Finding
from codeatlas.git.executable import run_git
from codeatlas.git.refs import resolve_ref
from codeatlas.git.repository import validate_repository
from codeatlas.git.snapshot import temporary_snapshot
from codeatlas.orchestrator.repair import RepairOrchestrator, record_repair_result
from codeatlas.orchestrator.repair_models import (
    REPAIR_POLICY_VERSION,
    RepairLimits,
    RepairRepositoryState,
    repair_payload_is_safe,
)
from codeatlas.orchestrator.review import ReviewResult, run_review
from codeatlas.orchestrator.validation import build_validation_review
from codeatlas.patching.apply import (
    apply_patch_to_worktree,
    capture_worktree_file_state,
    restore_worktree_files,
)
from codeatlas.patching.models import PatchProposal, PatchStatus, PatchValidationResult
from codeatlas.patching.parser import parse_unified_diff
from codeatlas.patching.policy import evaluate_patch_policy
from codeatlas.patching.proposal import (
    audit_patch_redaction,
    compute_patch_hash,
    create_patch_proposal,
    verify_validation_approval_token,
)
from codeatlas.patching.validator import validate_patch_proposal
from codeatlas.review.mock import MockRepairReviewer
from codeatlas.review.packet import ReviewPacket
from codeatlas.service.models import (
    FIX_PROPOSAL_SCHEMA_VERSION,
    FeedbackLabel,
    FindingContext,
    FindingDetailResponse,
    FindingFeedbackResponse,
    FindingSummary,
    FixApplyEvent,
    FixApplyHistoryResponse,
    FixApplyResponse,
    FixEligibilityResponse,
    FixProposalLifecycle,
    FixProposalResponse,
    FixValidationApprovalResponse,
    FixValidationResponse,
    LifecycleState,
    PatchProposalResponse,
    ReviewCreateRequest,
    ReviewStatusResponse,
    ValidateProposalResponse,
)

# Bounded, user-safe explanations for every fix-generation rejection code.
# Values are fixed strings; raw orchestrator/provider text is never shown.
FIX_REJECTION_EXPLANATIONS: dict[str, str] = {
    "abstained": "This finding was abstained during quality evaluation, so no fix can be proposed.",
    "suppressed_duplicate": "This finding is a suppressed duplicate of another finding.",
    "low_evidence": "This finding lacks sufficient deterministic evidence for a fix proposal.",
    "ambiguous": "The finding location is ambiguous; fix proposals need an exact location.",
    "unsupported_flow": "The flagged data flow is not supported by the repair adapters.",
    "invalid_location": "The finding location is invalid or outside the reviewed change.",
    "review_only_without_fix_eligibility": "This finding is review-only and fix generation was not explicitly requested.",
    "finding_not_fix_eligible": "This finding is not classified as fixable.",
    "quality_gate_failed": "This finding did not pass the quality gates required for fix proposals.",
    "language_review_only": "The language adapter classifies this finding as review-only.",
    "unsupported_language": "The target file language is not supported for fix proposals.",
    "protected_or_generated_target": "The target file is protected or generated; fixes are never proposed for it.",
    "stale_run": "The review run is no longer current.",
    "stale_finding": "The finding no longer matches its review run record.",
    "finding_not_in_run": "This finding does not belong to the review run.",
    "invalid_run_finding": "The review run record for this finding is invalid.",
    "incompatible_repository_state": "The repository state no longer matches the review run.",
    "repository_state_stale": "The repository has changed since the review run; start a new review.",
    "unreadable_or_unbounded_target": "The target file could not be read within the bounded size limits.",
    "stale_or_truncated_context": "The reviewed code context is stale or truncated; start a new review.",
    "missing_required_context": "The reviewed code context is missing; start a new review.",
    "required_context_exceeds_bounds": "The code context exceeds the bounded size limits.",
    "unsafe_or_unbounded_context": "The code context could not be safely bounded.",
    "source_parse_failed": "The target file could not be parsed.",
    "packet_redaction_failed": "The review packet failed redaction checks.",
    "invalid_provider_metadata": "The fix provider metadata was invalid.",
    "offline_provider_required": "Fix generation requires an offline provider; none is available.",
    "provider_abstained_or_failed": "The fix provider could not produce a proposal for this finding.",
    "invalid_provider_output": "The fix provider returned an invalid proposal.",
    "unsafe_or_unbounded_provider_output": "The provider output exceeded bounds or failed safety checks.",
    "provider_output_redaction_failed": "The provider output failed redaction safety checks.",
    "provider_output_exceeds_bounds": "The provider output exceeded the size bounds.",
    "provider_failed": "The fix provider failed to produce a proposal.",
    "provider_scope_or_explanation_invalid": "The provider proposal did not match the finding scope.",
    "exactly_one_finding_required": "Fix generation handles exactly one finding at a time.",
    "exactly_one_proposal_required": "The provider returned more than one proposal.",
    "invalid_input": "The fix generation request was invalid.",
    "patch_parse_failed": "The proposed patch could not be parsed.",
    "patch_scope_invalid": "The proposed patch does not stay within the finding location.",
    "language_constraint_failed": "The proposed patch violates the language constraints.",
    "patch_preview_failed": "The proposed patch could not be previewed against the file.",
    "patch_preview_exceeds_bounds": "The previewed result exceeds the size bounds.",
    "empty_repair": "The proposal would not change the target file.",
    "syntax_validation_failed": "The proposed patch does not preserve valid syntax.",
    "patch_validation_failed": "The proposed patch failed static validation or patch policy.",
    "repository_state_changed_during_repair": "The repository changed during generation; start a new review.",
    # Service-level request errors.
    "run_not_ready": "The review run has not completed, so fix proposals are unavailable.",
    "run_failed": "The review run failed; fix proposals are unavailable.",
    "packet_unavailable": "The review packet is unavailable; fix proposals are unavailable.",
    "generation_in_progress": "A fix proposal is already being generated for this finding.",
    "not_regenerable": "Fix regeneration is not available in the current proposal state.",
    "not_rejectable": "Only a draft-ready fix proposal can be rejected.",
    "proposal_not_found": "No fix proposal exists for this finding in this run.",
    "scope_mismatch": "The fix proposal does not belong to the requested run and finding.",
    "validation_not_approved": "Approve this FixProposal for sandbox validation before running it.",
    "validation_not_available": "This FixProposal is not available for sandbox validation in its current state.",
    "validation_scope_invalid": "The FixProposal validation scope no longer matches the review run.",
    "validation_token_invalid": "The approval token does not match this FixProposal validation scope.",
    "validation_redaction_failed": "Validation evidence failed redaction checks and was not returned.",
    # Phase 11C-D: explicit apply of a validated FixProposal.
    "apply_not_validated": "Run isolated sandbox validation on this FixProposal before applying it.",
    "apply_validation_failed": "This FixProposal failed sandbox validation and cannot be applied.",
    "apply_evidence_missing": "Validation evidence for this FixProposal is missing; revalidate before applying.",
    "apply_evidence_identity_mismatch": "Validation evidence does not match this FixProposal; revalidate before applying.",
    "apply_scope_invalid": "The FixProposal apply scope no longer matches the review run.",
    "apply_policy_version_incompatible": "The FixProposal was generated under an incompatible repair policy version.",
    "apply_not_available": "This FixProposal is not available for application in its current state.",
    "apply_confirmation_invalid": "The apply confirmation does not match the current FixProposal patch hash.",
    "apply_redaction_failed": "The patch failed redaction checks and cannot be applied.",
    "patch_hash_mismatch": "The patch hash no longer matches the FixProposal content.",
    "already_applied": "This FixProposal has already been applied.",
    "workspace_dirty": "The workspace has uncommitted changes; resolve them before applying a fix.",
    "apply_failed": "The FixProposal could not be applied; no files were changed.",
    "resulting_diff_mismatch": "The applied result did not match the validated diff; the workspace was restored.",
    # Phase 11C-E: safe revert and apply history.
    "revert_not_available": "This FixProposal is not available for revert in its current state.",
    "revert_confirmation_invalid": "The revert confirmation does not match the applied FixProposal patch hash.",
    "revert_state_changed": "The target files changed after the fix was applied; revert is unavailable.",
    "revert_failed": "The revert could not be completed; the applied files were left unchanged.",
}

# Reasons that fail closed before any provider/patch work: nothing was wrong
# with generation itself, the finding or state is simply not eligible.
_ELIGIBILITY_REASONS = frozenset({
    "abstained", "suppressed_duplicate", "low_evidence", "ambiguous", "unsupported_flow",
    "invalid_location", "review_only_without_fix_eligibility", "finding_not_fix_eligible",
    "quality_gate_failed", "language_review_only", "unsupported_language",
    "protected_or_generated_target", "stale_run", "stale_finding", "finding_not_in_run",
    "invalid_run_finding", "incompatible_repository_state", "repository_state_stale",
    "unreadable_or_unbounded_target", "stale_or_truncated_context", "missing_required_context",
    "required_context_exceeds_bounds", "unsafe_or_unbounded_context", "source_parse_failed",
    "packet_redaction_failed", "invalid_provider_metadata", "offline_provider_required",
    "exactly_one_finding_required", "invalid_input", "repository_state_changed_during_repair",
})

# Provider output that failed redaction, bounds, or static patch policy.
_POLICY_REASONS = frozenset({
    "patch_validation_failed", "provider_output_redaction_failed",
    "unsafe_or_unbounded_provider_output", "provider_output_exceeds_bounds",
})


def _lifecycle_for_reason(reason: str) -> FixProposalLifecycle:
    if reason in _ELIGIBILITY_REASONS:
        return "not_eligible"
    if reason in _POLICY_REASONS:
        return "rejected_by_policy"
    return "generation_failed"


def _safe_finding_path(path: str) -> bool:
    return (
        bool(path) and len(path) <= 512 and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


class ServiceStateError(ValueError):
    """A fix-proposal request failed closed with a stable, safe reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.explanation = FIX_REJECTION_EXPLANATIONS.get(reason, "The fix request was rejected.")
        super().__init__(self.explanation)


class FixProposalRecord:
    """In-memory FixProposal plus the underlying existing PatchProposal."""

    def __init__(self, response: FixProposalResponse, patch_proposal: PatchProposal | None) -> None:
        self.response = response
        self.patch_proposal = patch_proposal
        self.operation_lock = threading.Lock()
        # Phase 11C-D/E: apply state and bounded audit history. The captured
        # pre-apply bytes live only in memory, so revert availability is
        # scoped to this service session.
        self.apply_state: dict[str, Any] | None = None
        self.apply_events: list[FixApplyEvent] = []


class ServicePathError(ValueError):
    """Raised when a repository path is invalid, inaccessible, or unsafe."""


def validate_service_repo_path(raw_path: str | Path) -> Path:
    """Validate that raw_path is a legitimate, safe, existing Git repository."""
    if not raw_path or not str(raw_path).strip():
        raise ServicePathError("Repository path cannot be empty")

    path_str = str(raw_path).strip()
    if "\x00" in path_str:
        raise ServicePathError("Null byte in repository path")

    try:
        resolved = Path(path_str).expanduser().resolve()
    except Exception as err:
        raise ServicePathError(f"Cannot resolve path '{path_str}': {err}") from err

    if not resolved.exists():
        raise ServicePathError(f"Repository path does not exist: '{resolved}'")
    if not resolved.is_dir():
        raise ServicePathError(f"Repository path is not a directory: '{resolved}'")

    # Disallow root filesystems or standard sensitive OS directories
    prohibited_roots = {
        Path("/"),
        Path("/etc"),
        Path("/root"),
        Path("/bin"),
        Path("/usr"),
        Path("/var"),
        Path("C:\\"),
        Path("C:\\Windows"),
        Path("C:\\Program Files"),
    }
    if resolved in prohibited_roots:
        raise ServicePathError(f"Access to protected system path is prohibited: '{resolved}'")

    try:
        repo = validate_repository(resolved)
        if repo.root != resolved and not (resolved / ".git").exists():
            raise ServicePathError(f"Path is not a Git repository root: '{resolved}'")
    except Exception as err:
        raise ServicePathError(f"Path is not a valid Git repository: '{resolved}' ({err})") from err

    return resolved


class ReviewRunRecord:
    """Tracks one review run in memory."""

    def __init__(self, run_id: str, request: ReviewCreateRequest, repo_path: Path) -> None:
        self.run_id = run_id
        self.request = request
        self.repo_path = repo_path
        self.status: LifecycleState = "idle"
        self.progress_text = "Review initialized"
        self.policy_decision = "unknown"
        self.result: ReviewResult | None = None
        self.packet: ReviewPacket | None = None
        self.findings: list[dict[str, Any]] = []
        self.dismissed_finding_ids: set[str] = set()
        self.feedback: dict[str, str] = {}
        self.cancel_requested = threading.Event()
        self.errors: list[str] = []
        self.limitations: list[str] = []
        self.created_at = datetime.now(UTC).isoformat()
        self.completed_at = ""
        self.test_status = "not_run"
        self.full_suite_status = "not_run"
        self.patch_validation_status = "not_run"
        self.truncated = False
        self.temp_packet_file: Path | None = None
        # Phase 11C-B: latest fix-proposal state per finding id.
        self.fix_states: dict[str, dict[str, Any]] = {}


class ReviewStateManager:
    """Thread-safe state manager for reviews, findings, proposals, and validations."""

    def __init__(
        self,
        *,
        repair_provider: Any = None,
        repair_limits: RepairLimits | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._reviews: dict[str, ReviewRunRecord] = {}
        self._proposals: dict[str, tuple[PatchProposal, Path]] = {}
        self._validations: dict[str, PatchValidationResult] = {}
        self._fix_proposals: dict[str, FixProposalRecord] = {}
        # Offline repair provider at the existing provider boundary; network-capable
        # providers are rejected by the orchestrator before invocation.
        self.repair_provider = repair_provider or MockRepairReviewer()
        self.repair_limits = repair_limits or RepairLimits()

    def get_review(self, run_id: str) -> ReviewRunRecord | None:
        with self._lock:
            return self._reviews.get(run_id)

    def health_summary(self) -> dict[str, Any]:
        """Return only bounded service metadata, never repository or provider content."""
        with self._lock:
            active = [record for record in self._reviews.values()
                      if not record.completed_at and record.status not in {"completed", "failed", "cancelled"}]
            providers = {record.request.review_provider or "deterministic" for record in active}
            if not providers and self._reviews:
                latest = next(reversed(self._reviews.values()))
                providers = {latest.request.review_provider or "deterministic"}
            provider = next(iter(providers)) if len(providers) == 1 else "mixed" if providers else "deterministic"
            return {"active_reviews": len(active), "provider": provider}

    def get_proposal(self, proposal_id: str) -> tuple[PatchProposal, Path] | None:
        with self._lock:
            return self._proposals.get(proposal_id)

    def start_review(self, request: ReviewCreateRequest) -> ReviewStatusResponse:
        repo_path = validate_service_repo_path(request.repo)

        import uuid
        run_id = f"rev-{uuid.uuid4().hex[:12]}"

        record = ReviewRunRecord(run_id, request, repo_path)
        record.status = "preparing"
        record.progress_text = "Preparing repository snapshot"

        with self._lock:
            self._reviews[run_id] = record

        # Run review in background worker thread
        thread = threading.Thread(
            target=self._execute_review_run,
            args=(record,),
            name=f"codeatlas-review-{run_id}",
            daemon=True,
        )
        thread.start()

        return self.get_review_status(run_id)

    def _execute_review_run(self, record: ReviewRunRecord) -> None:
        temp_dir = tempfile.TemporaryDirectory(prefix=f"codeatlas-srv-{record.run_id}-")
        try:
            packet_path = Path(temp_dir.name) / "packet.json"
            record.temp_packet_file = packet_path

            if record.cancel_requested.is_set():
                record.status = "cancelled"
                record.progress_text = "Review cancelled by operator"
                record.completed_at = datetime.now(UTC).isoformat()
                return

            record.status = "indexing"
            record.progress_text = "Indexing symbols and changed files"

            record.status = "analyzing"
            record.progress_text = "Running deterministic analyzers"

            record.status = "reviewing"
            record.progress_text = "Assembling review packet and evaluating policy"

            res = run_review(
                repo=record.repo_path,
                base=record.request.base,
                head=record.request.head,
                packet_output=packet_path,
                index_repository=record.request.index_repository,
                assemble_review_packet=record.request.assemble_review_packet,
                review_provider=record.request.review_provider,
                provider_model=record.request.provider_model,
                provider_timeout=record.request.provider_timeout,
                allow_patch_suggestions=record.request.allow_patch_suggestions,
            )

            if record.cancel_requested.is_set():
                record.status = "cancelled"
                record.progress_text = "Review cancelled by operator"
                record.completed_at = datetime.now(UTC).isoformat()
                return

            record.result = res
            manifest = res.manifest

            record.policy_decision = (
                manifest.policy_decisions[0].get("decision", "unknown")
                if manifest.policy_decisions
                else "unknown"
            )
            record.errors = list(manifest.errors)
            record.limitations = list(manifest.packet_limitations or [])
            record.truncated = bool(manifest.packet_truncated)
            record.test_status = manifest.tests_status or "not_run"
            record.full_suite_status = manifest.full_suite_status or "not_run"

            # Load full ReviewPacket if written
            if packet_path.exists():
                try:
                    data = json.loads(packet_path.read_text(encoding="utf-8"))
                    record.packet = ReviewPacket.model_validate(data)
                except Exception as err:
                    record.errors.append(f"Failed to load review packet: {err}")

            raw_findings = list(manifest.findings)
            record.findings = raw_findings

            record.status = "findings_ready"
            record.progress_text = f"Review ready with {len(raw_findings)} findings"

            # Transition to completed if no errors or completed with warnings
            record.status = "completed" if not manifest.errors else "failed"
            record.completed_at = datetime.now(UTC).isoformat()

        except Exception as err:
            record.status = "failed"
            record.progress_text = f"Review failed: {err}"
            record.errors.append(str(err))
            record.completed_at = datetime.now(UTC).isoformat()
        finally:
            try:
                temp_dir.cleanup()
            except Exception:
                pass

    def cancel_review(self, run_id: str) -> bool:
        record = self.get_review(run_id)
        if record is None:
            return False
        record.cancel_requested.set()
        if record.status not in {"completed", "failed"}:
            record.status = "cancelled"
            record.progress_text = "Review cancelled by operator"
            record.completed_at = datetime.now(UTC).isoformat()
        return True

    def get_review_status(self, run_id: str) -> ReviewStatusResponse:
        record = self.get_review(run_id)
        if record is None:
            raise KeyError(f"Review run '{run_id}' not found")

        finding_counts: dict[str, int] = {
            "total": len(record.findings),
            "blocker": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "info": 0,
            "abstained": 0,
        }
        for f in record.findings:
            sev = str(f.get("severity", "info")).lower()
            if sev in finding_counts:
                finding_counts[sev] += 1
            if f.get("status") == "abstained":
                finding_counts["abstained"] += 1

        return ReviewStatusResponse(
            run_id=record.run_id,
            status=record.status,
            progress_text=record.progress_text,
            repository=str(record.repo_path),
            base=record.request.base,
            head=record.request.head,
            base_commit=(
                record.result.manifest.base_commit
                if record.result is not None and record.result.manifest.base_commit
                else None
            ),
            head_commit=(
                record.result.manifest.head_commit
                if record.result is not None and record.result.manifest.head_commit
                else None
            ),
            policy_decision=record.policy_decision,
            finding_counts=finding_counts,
            test_status=record.test_status,
            full_suite_status=record.full_suite_status,
            patch_validation_status=record.patch_validation_status,
            truncated=record.truncated or len(record.findings) > record.request.max_findings,
            limitations=record.limitations[:20],
            errors=record.errors[:10],
            created_at=record.created_at,
            completed_at=record.completed_at,
        )

    def get_findings(
        self,
        run_id: str,
        *,
        severity: str | None = None,
        category: str | None = None,
        status: str | None = None,
        include_dismissed: bool = False,
    ) -> list[FindingSummary]:
        record = self.get_review(run_id)
        if record is None:
            raise KeyError(f"Review run '{run_id}' not found")

        items: list[FindingSummary] = []
        for f in record.findings:
            f_id = str(f.get("id", ""))
            f_sev = str(f.get("severity", "info")).lower()
            f_cat = str(f.get("category", "")).lower()
            f_stat = str(f.get("status", "")).lower()
            is_dismissed = f_id in record.dismissed_finding_ids

            if not include_dismissed and is_dismissed:
                continue
            if severity and f_sev != severity.lower():
                continue
            if category and f_cat != category.lower():
                continue
            if status and f_stat != status.lower():
                continue

            items.append(
                FindingSummary(
                    id=f_id,
                    severity=f.get("severity", "info"),
                    category=f.get("category", "code_quality"),
                    claim=f.get("claim", "")[:200],
                    file=f.get("file", ""),
                    line=int(f.get("line") or f.get("start_line") or 1),
                    start_line=int(f.get("start_line") or f.get("line") or 1),
                    end_line=int(f.get("end_line") or f.get("line") or 1),
                    confidence=float(f.get("confidence") or 0.8),
                    evidence_strength=str(f.get("evidence_strength", "supported")),
                    status=str(f.get("status", "review_only")),
                    dismissed=is_dismissed,
                    quality_version=str(f.get("quality_version", "11B.1")),
                    quality_decision=str(f.get("quality_decision", "review_only")),
                    quality_score=float(f.get("quality_score") or 0.0),
                    ambiguity_score=float(f.get("ambiguity_score") or 0.0),
                    unsupported_flow=bool(f.get("unsupported_flow", False)),
                    feedback=record.feedback.get(f_id),
                )
            )

        return items[:record.request.max_findings]

    def toggle_dismiss_finding(self, run_id: str, finding_id: str) -> bool:
        record = self.get_review(run_id)
        if record is None:
            raise KeyError(f"Review run '{run_id}' not found")

        with self._lock:
            if finding_id in record.dismissed_finding_ids:
                record.dismissed_finding_ids.remove(finding_id)
                return False
            record.dismissed_finding_ids.add(finding_id)
            return True

    def get_finding_detail(self, run_id: str, finding_id: str) -> FindingDetailResponse:
        record = self.get_review(run_id)
        if record is None:
            raise KeyError(f"Review run '{run_id}' not found")

        finding_dict = next((f for f in record.findings if str(f.get("id")) == finding_id), None)
        if finding_dict is None:
            raise KeyError(f"Finding '{finding_id}' not found in review run '{run_id}'")

        file_path = finding_dict.get("file", "")
        start_line = int(finding_dict.get("start_line") or finding_dict.get("line") or 1)
        end_line = int(finding_dict.get("end_line") or finding_dict.get("line") or 1)

        # Context assembly from packet
        changed_lines: list[int] = []
        containing_symbol: dict[str, Any] | None = None
        relevant_imports: list[dict[str, Any]] = []
        relevant_references: list[dict[str, Any]] = []
        related_tests: list[str] = []
        context_candidates: list[dict[str, Any]] = []
        truncation_status = False
        evidence_sources: list[str] = list(finding_dict.get("tools_consulted", []))

        if record.packet is not None:
            p = record.packet
            truncation_status = p.truncated
            # Changed lines for file
            ranges = p.changed_line_ranges.get(file_path, [])
            for r in ranges:
                if len(r) == 2:
                    changed_lines.extend(range(r[0], r[1] + 1))
            changed_lines = sorted(set(changed_lines))[:100]

            # Matching symbol
            for sym in p.changed_symbols:
                if sym.get("file") == file_path:
                    sym_start = int(sym.get("start_line", 0))
                    sym_end = int(sym.get("end_line", 0))
                    if sym_start <= start_line and end_line <= sym_end:
                        containing_symbol = sym
                        break
            if containing_symbol is None and p.changed_symbols:
                for sym in p.changed_symbols:
                    if sym.get("file") == file_path:
                        containing_symbol = sym
                        break

            # Imports and references
            relevant_imports = [imp for imp in p.relevant_imports if imp.get("file") == file_path][:10]
            relevant_references = [ref for ref in p.relevant_references if ref.get("file") == file_path][:10]
            related_tests = list(p.relevant_tests)[:5]
            context_candidates = [c.model_dump(mode="json") for c in p.context_candidates[:5]]

        context = FindingContext(
            changed_lines=changed_lines,
            containing_symbol=containing_symbol,
            relevant_imports=relevant_imports,
            relevant_references=relevant_references,
            related_tests=related_tests,
            retrieved_context_candidates=context_candidates,
            truncation_status=truncation_status,
            evidence_sources=evidence_sources or ["codeatlas-deterministic-analyzer"],
        )

        return FindingDetailResponse(
            run_id=record.run_id,
            id=finding_id,
            claim=finding_dict.get("claim", "")[:500],
            severity=finding_dict.get("severity", "info"),
            category=finding_dict.get("category", "code_quality"),
            file=file_path,
            line=start_line,
            start_line=start_line,
            end_line=end_line,
            confidence=float(finding_dict.get("confidence") or 0.8),
            evidence_strength=str(finding_dict.get("evidence_strength", "supported")),
            status=str(finding_dict.get("status", "review_only")),
            impact=finding_dict.get("impact", "")[:500],
            limitations=[str(lim)[:200] for lim in finding_dict.get("limitations", [])][:10],
            provenance=finding_dict.get("provenance", {}),
            deterministic_evidence=finding_dict.get("evidence", [])[:10],
            reviewer_evidence=finding_dict.get("reviewer_evidence", [])[:10],
            policy_decision=record.policy_decision,
            test_result=record.test_status,
            patch_status=record.patch_validation_status,
            dismissed=finding_id in record.dismissed_finding_ids,
            quality_version=str(finding_dict.get("quality_version", "11B.1")),
            evidence_sources=[str(item) for item in finding_dict.get("evidence_sources", [])][:20],
            deterministic_support=float(finding_dict.get("deterministic_support") or 0.0),
            reviewer_support=float(finding_dict.get("reviewer_support") or 0.0),
            changed_line_support=float(finding_dict.get("changed_line_support") or 0.0),
            repository_context_support=float(finding_dict.get("repository_context_support") or 0.0),
            test_support=float(finding_dict.get("test_support") or 0.0),
            ambiguity_score=float(finding_dict.get("ambiguity_score") or 0.0),
            truncation_penalty=float(finding_dict.get("truncation_penalty") or 0.0),
            unsupported_flow=bool(finding_dict.get("unsupported_flow", False)),
            abstention_reason=finding_dict.get("abstention_reason"),
            duplicate_group_id=finding_dict.get("duplicate_group_id"),
            suppressed_finding_ids=[str(item) for item in finding_dict.get("suppressed_finding_ids", [])][:50],
            quality_decision=str(finding_dict.get("quality_decision", "review_only")),
            quality_score=float(finding_dict.get("quality_score") or 0.0),
            score_components={
                str(key): float(value)
                for key, value in dict(finding_dict.get("score_components", {})).items()
                if isinstance(value, (int, float))
            },
            quality_limitations=[str(item)[:300] for item in finding_dict.get("quality_limitations", [])][:20],
            feedback=record.feedback.get(finding_id),
            context=context,
        )

    def set_finding_feedback(
        self,
        run_id: str,
        finding_id: str,
        feedback: str | None,
    ) -> FindingFeedbackResponse:
        """Persist explicit repository-scoped feedback; never alter ranking or policy."""
        record = self.get_review(run_id)
        if record is None:
            raise KeyError(f"Review run '{run_id}' not found")
        if not any(str(item.get("id")) == finding_id for item in record.findings):
            raise KeyError(f"Finding '{finding_id}' not found in review run '{run_id}'")
        allowed = {"useful", "not_useful", "false_positive", "accepted", "dismissed", "needs_more_context"}
        if feedback is not None and feedback not in allowed:
            raise ValueError("Unsupported feedback label")
        previous = record.feedback.get(finding_id)
        reversed_feedback = previous is not None and previous != feedback
        if feedback is None:
            record.feedback.pop(finding_id, None)
        else:
            record.feedback[finding_id] = feedback
        for item in record.findings:
            if str(item.get("id")) == finding_id:
                item["feedback"] = feedback
                break
        fb: FeedbackLabel | None = feedback if feedback in ("useful", "not_useful", "false_positive", "accepted", "dismissed", "needs_more_context") else None
        prev_fb: FeedbackLabel | None = previous if previous in ("useful", "not_useful", "false_positive", "accepted", "dismissed", "needs_more_context") else None
        return FindingFeedbackResponse(
            run_id=run_id,
            finding_id=finding_id,
            repository=str(record.repo_path),
            feedback=fb,
            previous_feedback=prev_fb,
            reversed=reversed_feedback,
        )

    def explain_finding(self, finding_id: str, run_id: str | None = None) -> dict[str, Any]:
        """Produce safe deterministic explanation of a finding."""
        record = None
        if run_id:
            record = self.get_review(run_id)
        if record is None:
            # Fall back to searching any active review
            with self._lock:
                for r in self._reviews.values():
                    if any(str(f.get("id")) == finding_id for f in r.findings):
                        record = r
                        break

        if record is None:
            raise KeyError(f"Finding '{finding_id}' not found")

        finding = next((f for f in record.findings if str(f.get("id")) == finding_id), None)
        if finding is None:
            raise KeyError(f"Finding '{finding_id}' not found")

        category = str(finding.get("category", "code_quality"))
        severity = str(finding.get("severity", "info"))
        claim = str(finding.get("claim", ""))
        impact = str(finding.get("impact", ""))

        if category == "HARD_CODED_SECRET":
            explanation = (
                f"A potential secret or credential was identified in `{finding.get('file')}`. "
                "Hardcoding secrets in source files introduces security risks because source "
                "code may be distributed or accessible to unauthorized individuals."
            )
            remediation = "Move credentials to environment variables or an external secrets vault."
        elif category == "SENSITIVE_DATA_EXPOSURE":
            explanation = (
                f"Sensitive data may flow to an observable output sink in `{finding.get('file')}`. "
                "Exposing sensitive personal or security values through loggers or responses "
                "can lead to data disclosure vulnerabilities."
            )
            remediation = "Mask or remove sensitive fields before logging or transmitting data."
        else:
            explanation = (
                f"CodeAtlas flagged an issue in `{finding.get('file')}` at line {finding.get('line')}: {claim}"
            )
            remediation = "Review the flagged code and apply relevant safety checks or assertions."

        return {
            "finding_id": finding_id,
            "category": category,
            "severity": severity,
            "claim": claim[:300],
            "explanation": explanation,
            "impact": impact or "Potential reliability or security degradation.",
            "remediation_advice": remediation,
            "limitations": [str(l)[:200] for l in finding.get("limitations", [])][:5],
        }

    def create_patch_proposal(self, finding_id: str, run_id: str | None = None) -> PatchProposalResponse:
        """Create a draft PatchProposal for a finding without applying it."""
        record = None
        if run_id:
            record = self.get_review(run_id)
        if record is None:
            with self._lock:
                for r in self._reviews.values():
                    if any(str(f.get("id")) == finding_id for f in r.findings):
                        record = r
                        break

        if record is None:
            raise KeyError(f"Finding '{finding_id}' not found")

        finding = next((f for f in record.findings if str(f.get("id")) == finding_id), None)
        if finding is None:
            raise KeyError(f"Finding '{finding_id}' not found")

        file_path = finding.get("file", "src/main.py")
        line = int(finding.get("line") or finding.get("start_line") or 1)
        base_commit = (
            record.result.manifest.head_commit
            if record.result and record.result.manifest.head_commit
            else "a" * 40
        )

        # Construct a safe draft fix proposal (replaces sensitive line with a safe variable / comment)
        diff_text = (
            f"--- a/{file_path}\n"
            f"+++ b/{file_path}\n"
            f"@@ -{line},1 +{line},1 @@\n"
            f"-# CodeAtlas finding: {finding.get('claim', '')[:40]}\n"
            f"+# TODO(operator): resolved CodeAtlas finding {finding_id}\n"
        )

        proposal = create_patch_proposal(
            finding_id=finding_id,
            provider_name="codeatlas-draft-fix",
            provider_version="1.0.0",
            base_commit=base_commit,
            target_files=[file_path],
            unified_diff=diff_text,
            rationale=f"Draft fix proposal for finding {finding_id}",
            expected_behavior="Requires human approval before sandbox application",
            risk_level="low",
        )
        proposal.status = PatchStatus.REQUIRES_HUMAN_APPROVAL

        with self._lock:
            self._proposals[proposal.proposal_id] = (proposal, record.repo_path)
            record.status = "patch_proposed"
            record.patch_validation_status = "proposed"

        return PatchProposalResponse(
            proposal_id=proposal.proposal_id,
            finding_id=finding_id,
            status=proposal.status,
            target_files=proposal.target_files,
            unified_diff=proposal.unified_diff,
            rationale=proposal.rationale,
            risk_level=proposal.risk_level,
            approval_required=True,
        )

    def validate_patch_proposal(
        self,
        proposal_id: str,
        *,
        approval_token: str,
        run_tests: bool = False,
        run_full_suite: bool = False,
    ) -> ValidateProposalResponse:
        """Validate an approved patch proposal in an isolated sandbox."""
        # FixProposals cannot bypass their complete approval scope through the
        # older PatchProposal endpoint or a token in the Phase 7 token format.
        with self._lock:
            fix_record = self._fix_proposals.get(proposal_id)
        if fix_record is not None:
            fix = fix_record.response
            validation = self.validate_fix_proposal(
                fix.run_id, fix.finding_id, proposal_id,
                approval_token=approval_token,
                run_tests=run_tests,
                run_full_suite=run_full_suite,
            )
            return ValidateProposalResponse(
                proposal_id=proposal_id, valid=validation.valid, status=validation.status,
                approval_verified=validation.approval_verified,
                applies_cleanly=validation.applies_cleanly, syntax_valid=validation.syntax_valid,
                tests_status=validation.tests_status, full_suite_status=validation.full_suite_status,
                errors=validation.errors, warnings=validation.warnings,
            )
        item = self.get_proposal(proposal_id)
        if item is None:
            raise KeyError(f"Patch proposal '{proposal_id}' not found")

        proposal, repo_path = item

        # Validate proposal using existing isolated sandbox validator
        val_result = validate_patch_proposal(
            proposal=proposal,
            repository_root=repo_path,
            approval_token=approval_token,
            allow_isolated_apply=True,
            run_tests=run_tests,
            run_full_suite=run_full_suite,
        )

        with self._lock:
            self._validations[proposal_id] = val_result

        status_str = "validated" if val_result.valid else "rejected"
        return ValidateProposalResponse(
            proposal_id=proposal_id,
            valid=val_result.valid,
            status=status_str,
            approval_verified=val_result.approval_verified,
            applies_cleanly=val_result.applies_cleanly,
            syntax_valid=val_result.syntax_valid,
            tests_status=val_result.tests_status,
            full_suite_status=getattr(val_result, "full_suite_status", "not_run"),
            errors=val_result.errors,
            warnings=val_result.warnings,
        )

    # ----------------------------------------------------------------------
    # Phase 11C-B: bounded fix proposals (preview and reject only; the
    # operations reuse RepairOrchestrator and the existing patch pipeline and
    # never apply, approve, or commit anything).
    # ----------------------------------------------------------------------

    def _fix_finding_and_record(self, run_id: str, finding_id: str) -> tuple[ReviewRunRecord, dict[str, Any]]:
        record = self.get_review(run_id)
        if record is None:
            raise KeyError(f"Review run '{run_id}' not found")
        finding = next((f for f in record.findings if str(f.get("id")) == finding_id), None)
        if finding is None:
            raise KeyError(f"Finding '{finding_id}' not found in review run '{run_id}'")
        return record, finding

    def _fix_blockers(self, record: ReviewRunRecord, finding: dict[str, Any]) -> list[str]:
        """Cheap mirror of the orchestrator's fail-closed gates; plan() stays authoritative."""
        blockers: list[str] = []
        manifest = record.result.manifest if record.result is not None else None
        if manifest is None:
            blockers.append("run_not_ready")
        elif manifest.errors:
            blockers.append("run_failed")
        if record.packet is None:
            blockers.append("packet_unavailable")
        provenance = finding.get("provenance") if isinstance(finding.get("provenance"), dict) else {}
        f_status = str(finding.get("status", ""))
        f_quality = str(finding.get("quality_decision", "review_only"))
        f_fixability = str(finding.get("fixability", "unknown"))
        f_strength = str(finding.get("evidence_strength", "none"))
        try:
            f_confidence = float(finding.get("confidence") or 0.0)
        except (TypeError, ValueError):
            f_confidence = 0.0
        if f_status == "abstained" or f_quality == "abstain":
            blockers.append("abstained")
        if f_quality == "suppress_duplicate" or any(
            finding.get("id") in (item.get("suppressed_finding_ids") or []) for item in record.findings
        ):
            blockers.append("suppressed_duplicate")
        if f_quality == "suppress_low_evidence" or f_strength in {"none", "weak"}:
            blockers.append("low_evidence")
        if finding.get("unsupported_flow") or provenance.get("unsupported_flow"):
            blockers.append("unsupported_flow")
        if (finding.get("ambiguity_score") or provenance.get("ambiguity_score")
                or provenance.get("ambiguous_location") or provenance.get("quality_conflict")):
            blockers.append("ambiguous")
        if f_status in {"rejected", "validated", "abstained"} or f_fixability in {"unknown", "not_fixable", "validated"}:
            blockers.append("finding_not_fix_eligible")
        if f_confidence < 0.70 or not finding.get("evidence"):
            blockers.append("low_evidence")
        file = str(finding.get("file", ""))
        packet = record.packet
        if not _safe_finding_path(file) or packet is None or file not in packet.changed_files:
            blockers.append("invalid_location")
        else:
            ranges = packet.changed_line_ranges.get(file, [])
            try:
                start = int(finding.get("start_line") or 0)
                end = int(finding.get("end_line") or 0)
            except (TypeError, ValueError):
                start = end = 0
            if not any(len(r) == 2 and 1 <= r[0] <= start <= end <= r[1] for r in ranges):
                blockers.append("invalid_location")
            adapter = select_language_adapter(file)
            if adapter is None:
                blockers.append("unsupported_language")
            elif (any(fnmatchcase(file, pattern) for pattern in self.repair_limits.prohibited_paths)
                    or adapter.is_protected(file)):
                blockers.append("protected_or_generated_target")
        # The repository must still be at the reviewed head commit.
        if manifest is not None:
            try:
                if resolve_ref(record.repo_path, "HEAD").commit != manifest.head_commit:
                    blockers.append("repository_state_stale")
            except Exception:
                blockers.append("repository_state_stale")
        return list(dict.fromkeys(blockers))

    def get_fix_eligibility(self, run_id: str, finding_id: str) -> FixEligibilityResponse:
        record, finding = self._fix_finding_and_record(run_id, finding_id)
        blockers = self._fix_blockers(record, finding)[:10]
        return FixEligibilityResponse(
            run_id=run_id,
            finding_id=finding_id,
            eligible=not blockers,
            reasons=blockers,
            explanations=[
                FIX_REJECTION_EXPLANATIONS.get(reason, "The finding is not eligible for fix proposals.")
                for reason in blockers
            ],
        )

    def _non_draft_fix_response(self, run_id: str, finding_id: str, reason: str) -> FixProposalResponse:
        return FixProposalResponse(
            proposal_id="",
            finding_id=finding_id,
            run_id=run_id,
            repository="",
            base_commit="",
            head_commit="",
            generation_status=_lifecycle_for_reason(reason),
            rejection_reason=reason,
            rejection_explanation=FIX_REJECTION_EXPLANATIONS.get(reason, "The fix request was rejected."),
        )

    def _store_fix_state(
        self,
        record: ReviewRunRecord,
        finding_id: str,
        state: FixProposalLifecycle,
        proposal_id: str | None,
        response: FixProposalResponse | None,
        reason: str | None = None,
    ) -> None:
        with self._lock:
            record.fix_states[finding_id] = {
                "state": state,
                "proposal_id": proposal_id,
                "response": response,
                "rejection_reason": reason,
                "updated_at": datetime.now(UTC).isoformat(),
            }

    def _draft_fix_response(
        self,
        record: ReviewRunRecord,
        finding_id: str,
        state: RepairRepositoryState,
        result: Any,
    ) -> FixProposalResponse:
        proposal = result.proposal
        context = result.context
        return FixProposalResponse(
            proposal_id=proposal.proposal_id,
            finding_id=finding_id,
            run_id=record.run_id,
            repository=context.repository_identity,
            base_commit=state.base_commit,
            head_commit=state.head_commit,
            target_files=list(proposal.target_files)[:1],
            patch_text=proposal.unified_diff,
            patch_hash=proposal.patch_hash,
            diagnosis=proposal.rationale[:2000],
            explanation=proposal.expected_behavior[:2000],
            expected_behavior_change=proposal.expected_behavior[:2000],
            assumptions=(
                ["Optional repository imports were truncated to the repair context limit."]
                if context.context_truncated else []
            ),
            risk_level=proposal.risk_level,
            confidence=context.confidence,
            evidence_strength=context.evidence_strength,
            evidence_sources=list(context.deterministic_evidence)[:10],
            quality_decision=context.quality_decision,
            policy_decision=dict(proposal.policy_decision or {}),
            generation_status="draft_ready",
            approval_required=True,
            limitations=list(proposal.limitations)[:20],
            created_at=proposal.created_at,
            schema_version=FIX_PROPOSAL_SCHEMA_VERSION,
            validation_status="approval_required",
            validation_history=["draft_ready", "approval_required"],
        )

    def request_fix_proposal(self, run_id: str, finding_id: str, *, regenerate: bool = False) -> FixProposalResponse:
        """Generate one bounded fix proposal for one finding via RepairOrchestrator.

        The user request is the explicit fix-eligibility decision for review-only
        findings; it is never an approval. All rejections return stable reason
        codes with fixed user-safe explanations.
        """
        record, finding = self._fix_finding_and_record(run_id, finding_id)
        with self._lock:
            current = dict(record.fix_states.get(finding_id, {}))
        if current.get("state") == "generating":
            pending = current.get("response")
            if pending is not None:
                return pending
        if regenerate and current.get("state") not in {
            "draft_ready", "rejected", "generation_failed", "rejected_by_policy", "regeneration_requested",
        }:
            raise ServiceStateError("not_regenerable")
        if regenerate and current.get("proposal_id"):
            previous = self._fix_proposals.get(str(current["proposal_id"]))
            if previous is not None and previous.response.validation_status in {
                "approved_for_validation", "validating", "applied_in_isolated_worktree", "tests_running",
            }:
                raise ServiceStateError("not_regenerable")
        if not regenerate and current.get("proposal_id"):
            existing = self._fix_proposals.get(str(current.get("proposal_id")))
            if existing is not None:
                return existing.response

        blockers = self._fix_blockers(record, finding)
        if blockers:
            reason = blockers[0]
            response = self._non_draft_fix_response(run_id, finding_id, reason)
            self._store_fix_state(record, finding_id, response.generation_status, None, response, reason)
            return response

        manifest = record.result.manifest
        packet = record.packet
        if manifest is None or packet is None:
            response = self._non_draft_fix_response(run_id, finding_id, "run_not_ready")
            self._store_fix_state(record, finding_id, response.generation_status, None, response, "run_not_ready")
            return response
        state = RepairRepositoryState(
            run_id=manifest.run_id,
            repository=manifest.repository,
            base_commit=manifest.base_commit,
            head_commit=manifest.head_commit,
        )
        self._store_fix_state(record, finding_id, "generating", None, None)
        try:
            finding_model = Finding.model_validate(finding)
            with temporary_snapshot(record.repo_path, manifest.head_commit) as snapshot:
                result = RepairOrchestrator(self.repair_provider, limits=self.repair_limits).plan(
                    finding_model,
                    packet=packet,
                    manifest=manifest,
                    state=state,
                    snapshot=snapshot,
                    fix_eligible=True,
                )
        except ServiceStateError:
            raise
        except Exception:
            response = self._non_draft_fix_response(run_id, finding_id, "provider_failed")
            self._store_fix_state(record, finding_id, response.generation_status, None, response, "provider_failed")
            return response
        if (result.status == "proposed" and result.proposal is not None and result.context is not None
                and result.proposal.approval_required):
            response = self._draft_fix_response(record, finding_id, state, result)
            if not repair_payload_is_safe(response.model_dump(mode="json")):
                response = self._non_draft_fix_response(run_id, finding_id, "provider_output_redaction_failed")
                self._store_fix_state(
                    record, finding_id, response.generation_status, None, response,
                    "provider_output_redaction_failed",
                )
                return response
            old_id = current.get("proposal_id")
            with self._lock:
                record.result = dataclasses.replace(
                    record.result, manifest=record_repair_result(record.result.manifest, result)
                )
                self._fix_proposals[response.proposal_id] = FixProposalRecord(response, result.proposal)
                # Keep the existing approval-gated validate flow available for this proposal.
                self._proposals[response.proposal_id] = (result.proposal, record.repo_path)
                if regenerate and old_id and old_id != response.proposal_id:
                    previous = self._fix_proposals.get(str(old_id))
                    if previous is not None and previous.response.generation_status == "draft_ready":
                        if previous.patch_proposal is not None:
                            try:
                                previous.patch_proposal.transition_to(PatchStatus.REJECTED, reason="superseded")
                            except ValueError:
                                pass
                        previous.response = previous.response.model_copy(update={
                            "generation_status": "rejected",
                            "rejection_reason": "superseded_by_regeneration",
                            "rejection_explanation": "This proposal was replaced by a regenerated proposal.",
                        })
            self._store_fix_state(record, finding_id, "draft_ready", response.proposal_id, response)
            return response
        reason = result.reason or "provider_failed"
        response = self._non_draft_fix_response(run_id, finding_id, reason)
        self._store_fix_state(record, finding_id, response.generation_status, None, response, reason)
        return response

    def get_fix_proposal(self, run_id: str, finding_id: str, proposal_id: str | None = None) -> FixProposalResponse:
        record, _ = self._fix_finding_and_record(run_id, finding_id)
        with self._lock:
            if proposal_id is None:
                stored = record.fix_states.get(finding_id, {})
                proposal_id = stored.get("proposal_id")
                if not proposal_id:
                    raise ServiceStateError("proposal_not_found")
            fix_record = self._fix_proposals.get(proposal_id)
        if fix_record is None:
            raise ServiceStateError("proposal_not_found")
        if fix_record.response.run_id != run_id or fix_record.response.finding_id != finding_id:
            raise ServiceStateError("scope_mismatch")
        return fix_record.response

    @staticmethod
    def _repository_identity(path: Path) -> str:
        """Return the same bounded identity projection used by repair context."""
        parts = [part for part in path.as_posix().split("/") if part]
        return (parts[-1] if parts else "repository")[:128]

    def _fix_validation_scope(
        self,
        run_id: str,
        finding_id: str,
        proposal_id: str,
    ) -> tuple[ReviewRunRecord, FixProposalRecord, str, str, str]:
        """Resolve and independently verify trusted FixProposal scope metadata."""
        record, _ = self._fix_finding_and_record(run_id, finding_id)
        with self._lock:
            fix_record = self._fix_proposals.get(proposal_id)
        if fix_record is None:
            raise ServiceStateError("proposal_not_found")
        response = fix_record.response
        if response.run_id != run_id or response.finding_id != finding_id:
            raise ServiceStateError("scope_mismatch")
        if fix_record.patch_proposal is None or response.generation_status != "draft_ready":
            raise ServiceStateError("validation_not_available")
        manifest = record.result.manifest if record.result is not None else None
        if manifest is None or not manifest.base_commit or not manifest.head_commit:
            raise ServiceStateError("validation_scope_invalid")
        repository_identity = self._repository_identity(record.repo_path.resolve())
        patch = fix_record.patch_proposal
        provenance = patch.provenance if isinstance(patch.provenance, dict) else {}
        scope_matches = (
            response.repository == repository_identity
            and response.base_commit == manifest.base_commit
            and response.head_commit == manifest.head_commit
            and patch.finding_id == finding_id
            and patch.base_commit == response.head_commit
            # The repair manifest has its own orchestrator run identifier;
            # the externally bound approval scope remains the service run ID
            # carried by the FixProposal response and token.
            and provenance.get("run_id") == manifest.run_id
            and provenance.get("review_base_commit") == response.base_commit
            and provenance.get("review_head_commit") == response.head_commit
            and provenance.get("repository_identity") == response.repository
            and response.target_files == patch.target_files
            and response.patch_text == patch.unified_diff
            and response.patch_hash == patch.patch_hash
            and compute_patch_hash(patch.unified_diff) == patch.patch_hash
        )
        try:
            current_head = resolve_ref(record.repo_path, "HEAD").commit
        except Exception:
            current_head = ""
        if current_head != response.head_commit:
            raise ServiceStateError("repository_state_stale")
        if not scope_matches:
            raise ServiceStateError("validation_scope_invalid")
        return record, fix_record, response.base_commit, response.head_commit, repository_identity

    def _verify_fix_validation_token(
        self,
        *,
        response: FixProposalResponse,
        token: str | None,
        operation: str = "validate",
    ) -> tuple[bool, str | None]:
        """Verify the complete operator scope; provider fields never authorize this."""
        return verify_validation_approval_token(
            token,
            proposal_id=response.proposal_id,
            finding_id=response.finding_id,
            run_id=response.run_id,
            repository_identity=response.repository,
            base_commit=response.base_commit,
            head_commit=response.head_commit,
            patch_hash=response.patch_hash,
            target_files=response.target_files,
            operation=operation,
        )

    def _transition_fix_validation(
        self, record: ReviewRunRecord, fix_record: FixProposalRecord, state: str,
    ) -> None:
        """Explicit validation states, driven by the existing patch lifecycle."""
        transitions = {
            "approval_required": {"approved_for_validation"},
            "approved_for_validation": {"validating"},
            "validating": {"applied_in_isolated_worktree", "validation_failed", "cleanup_failed"},
            "applied_in_isolated_worktree": {"tests_running", "validated", "validation_failed", "cleanup_failed"},
            "tests_running": {"validated", "validation_failed", "cleanup_failed"},
            # Phase 11C-D/E: the validated evidence authorizes exactly one
            # explicit apply to the original workspace, followed by at most
            # one revert back to the captured pre-apply bytes.
            "validated": {"applied"},
            "applied": {"reverted"},
            "reverted": set(),
            "validation_failed": set(),
            "cleanup_failed": set(),
        }
        with self._lock:
            response = fix_record.response
            if state == response.validation_status:
                return
            if state not in transitions.get(response.validation_status, set()):
                raise ServiceStateError("validation_not_available")
            updated = response.model_copy(update={
                "validation_status": state,
                "validation_history": response.validation_history + [state],
            })
            fix_record.response = updated
            record.fix_states[response.finding_id]["response"] = updated
            record.patch_validation_status = state

    def _update_fix_validation_artifacts(
        self,
        record: ReviewRunRecord,
        fix_record: FixProposalRecord,
        result: PatchValidationResult,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Reuse observed-evidence attachment and manifest construction."""
        packet = None
        manifest = None
        source_packet = record.packet.model_copy(deep=True) if record.packet is not None else None
        # Each proposal gets its own observations; previous sandbox identities
        # must never be reused when attaching evidence for another proposal.
        if source_packet is not None:
            source_packet.proposal_id = None
            source_packet.sandbox_id = None
            source_packet.observed_test_evidence = None
        packet, manifest = build_validation_review(
            fix_record.patch_proposal,  # type: ignore[arg-type]
            result,
            repository=str(record.repo_path),
            run_id=record.run_id,
            packet=source_packet,
        )
        if not repair_payload_is_safe({"packet": packet.model_dump(mode="json"), "manifest": manifest.model_dump(mode="json")}):
            raise ServiceStateError("validation_redaction_failed")

        if record.result is not None:
            validated_patch = fix_record.patch_proposal
            if validated_patch is None:
                raise ServiceStateError("validation_not_available")
            current = record.result.manifest
            updates: dict[str, Any] = {}
            for field in (
                "proposal_id", "patch_hash", "approval_scope", "approval_operation", "approval_verified",
                "test_evidence_attached", "test_evidence_summary", "validation_limitations",
                "isolated_validation_attempted", "isolated_validation_status", "sandbox_id",
                "applies_cleanly", "syntax_valid", "tests_status", "build_status",
                "execution_allowed", "resulting_diff_hash", "cleanup_status",
                "validation_warnings", "test_plan", "test_runner", "test_discovery_reason",
                "test_exit_code", "test_duration_ms", "test_failures", "test_timeout",
                "test_output_redaction_audit", "network_allowed", "dependency_install_allowed",
                "resource_limits", "test_execution_attempted", "test_execution_blocked_reason",
                "diagnostic_summary", "failed_test_names", "test_failure_count", "test_pass_count",
                "test_skip_count", "test_output_truncated", "network_policy_requested",
                "network_policy_enforced", "network_isolation_verified", "diagnostic_limitations",
                "full_suite_requested", "full_suite_policy_opted_in", "full_suite_status",
                "full_suite_blocked_reason", "full_suite_command", "full_suite_result",
            ):
                updates[field] = getattr(manifest, field)
            existing_validation = dict(current.patch_validation or {})
            proposals = list(existing_validation.get("proposals") or [])
            validation_data = result.model_dump(mode="json")
            replaced = False
            for index, item in enumerate(proposals):
                if isinstance(item, dict) and item.get("proposal_id") == result.proposal_id:
                    proposals[index] = validation_data
                    replaced = True
                    break
            if not replaced:
                proposals.append(validation_data)
            existing_validation["proposals"] = proposals
            updates["patch_validation"] = existing_validation
            updates["patch_proposals"] = [
                validated_patch.model_dump(mode="json")
                if item.get("proposal_id") == result.proposal_id else item
                for item in current.patch_proposals
            ]
            updates["patch_proposal_statuses"] = [
                item.get("status", "proposed") for item in updates["patch_proposals"]
            ]
            record.result = dataclasses.replace(record.result, manifest=current.model_copy(update=updates))
        packet_data = packet.model_dump(mode="json")
        manifest_data = manifest.model_dump(mode="json")
        # The service FixProposal contract exposes repository identity only;
        # preserve the same projection in nested artifacts rather than leaking
        # the local absolute path used by the sandbox implementation.
        packet_data["repository"] = fix_record.response.repository
        manifest_data["repository"] = fix_record.response.repository
        return packet_data, manifest_data

    def _fix_validation_response(
        self,
        *,
        record: ReviewRunRecord,
        fix_record: FixProposalRecord,
        result: PatchValidationResult,
        validation_status: str,
        packet_data: dict[str, Any] | None,
        manifest_data: dict[str, Any] | None,
    ) -> FixValidationResponse:
        payload = result.model_dump(mode="json")
        token_safe = repair_payload_is_safe(payload)
        if not token_safe:
            # Never serialize a report that may contain a token or secret-bearing
            # execution text. The validator has already completed cleanup.
            payload = {
                "proposal_id": result.proposal_id,
                "base_commit": result.base_commit,
                "patch_hash": result.patch_hash,
                "approval_verified": False,
                "valid": False,
                "errors": [FIX_REJECTION_EXPLANATIONS["validation_redaction_failed"]],
                "cleanup_status": result.cleanup_status,
            }
            validation_status = "validation_failed"
            packet_data = None
            manifest_data = None
        observed = (packet_data or {}).get("observed_test_evidence") if packet_data else None
        return FixValidationResponse(
            proposal_id=fix_record.response.proposal_id,
            finding_id=fix_record.response.finding_id,
            run_id=record.run_id,
            status="validated" if payload.get("valid") else "rejected",
            validation_status=validation_status,  # type: ignore[arg-type]
            approval_verified=bool(payload.get("approval_verified")),
            valid=bool(payload.get("valid")),
            applies_cleanly=bool(payload.get("applies_cleanly")),
            syntax_valid=payload.get("syntax_valid"),
            tests_status=str(payload.get("tests_status", "not_run")),
            full_suite_status=str(payload.get("full_suite_status", "not_run")),
            sandbox_id=payload.get("sandbox_id"),
            cleanup_status=payload.get("cleanup_status"),
            resulting_diff_hash=payload.get("resulting_diff_hash"),
            commands_run=list(payload.get("commands_run") or []),
            tests_run=list(payload.get("tests_run") or []),
            test_plan=payload.get("test_plan"),
            test_result=payload.get("test_result"),
            full_suite_result=payload.get("full_suite_result"),
            validation_result=payload,
            observed_test_evidence=observed,
            errors=list(payload.get("errors") or []),
            warnings=list(payload.get("warnings") or []),
            limitations=list(payload.get("diagnostic_limitations") or [])[:20],
            review_packet=packet_data,
            human_approval_manifest=manifest_data,
            validation_history=list(fix_record.response.validation_history),
        )

    def approve_fix_proposal_for_validation(
        self,
        run_id: str,
        finding_id: str,
        proposal_id: str,
        *,
        approval_token: str,
    ) -> FixValidationApprovalResponse:
        """Verify an operator approval and authorize only sandbox validation."""
        record, fix_record, _base, _head, _repository = self._fix_validation_scope(
            run_id, finding_id, proposal_id
        )
        with fix_record.operation_lock:
            response = fix_record.response
            if response.validation_status != "approval_required":
                raise ServiceStateError("validation_not_available")
            verified, _reason = self._verify_fix_validation_token(response=response, token=approval_token)
            if not verified:
                return FixValidationApprovalResponse(
                    proposal_id=proposal_id,
                    finding_id=finding_id,
                    run_id=run_id,
                    approval_verified=False,
                    validation_status="approval_required",
                    errors=[FIX_REJECTION_EXPLANATIONS["validation_token_invalid"]],
                )
            patch = fix_record.patch_proposal
            if patch is None:
                raise ServiceStateError("validation_not_available")
            try:
                patch.transition_to(PatchStatus.APPROVED, reason="approved_for_validation")
            except ValueError as err:
                raise ServiceStateError("validation_not_available") from err
            self._transition_fix_validation(record, fix_record, "approved_for_validation")
        return FixValidationApprovalResponse(
            proposal_id=proposal_id,
            finding_id=finding_id,
            run_id=run_id,
            approval_verified=True,
            validation_status="approved_for_validation",
        )

    def approve_fix_proposal(
        self,
        run_id: str,
        finding_id: str,
        proposal_id: str,
        *,
        approval_token: str,
    ) -> FixValidationApprovalResponse:
        """Compatibility name for the explicit validate-only approval operation."""
        return self.approve_fix_proposal_for_validation(
            run_id, finding_id, proposal_id, approval_token=approval_token,
        )

    def validate_fix_proposal(
        self,
        run_id: str,
        finding_id: str,
        proposal_id: str,
        *,
        approval_token: str,
        run_tests: bool = False,
        run_full_suite: bool = False,
    ) -> FixValidationResponse:
        """Run an explicitly approved FixProposal through the existing validator."""
        record, fix_record, _base, _head, _repository = self._fix_validation_scope(
            run_id, finding_id, proposal_id
        )
        with fix_record.operation_lock:
            response = fix_record.response
            if response.validation_status != "approved_for_validation":
                raise ServiceStateError("validation_not_approved")
            verified, _reason = self._verify_fix_validation_token(response=response, token=approval_token)
            if not verified:
                return FixValidationResponse(
                    proposal_id=proposal_id,
                    finding_id=finding_id,
                    run_id=run_id,
                    status="rejected",
                    validation_status="approved_for_validation",
                    approval_verified=False,
                    errors=[FIX_REJECTION_EXPLANATIONS["validation_token_invalid"]],
                )
            patch = fix_record.patch_proposal
            if patch is None:
                raise ServiceStateError("validation_not_available")
            self._transition_fix_validation(record, fix_record, "validating")
        scope = {
            "proposal_id": response.proposal_id, "finding_id": response.finding_id,
            "run_id": response.run_id, "repository_identity": response.repository,
            "base_commit": response.base_commit, "head_commit": response.head_commit,
            "patch_hash": response.patch_hash, "target_files": list(response.target_files),
            "operation": "validate",
        }

        def observe_patch_state(state: str) -> None:
            if state == PatchStatus.APPLIED_IN_ISOLATED_WORKTREE:
                self._transition_fix_validation(record, fix_record, "applied_in_isolated_worktree")
            elif state == PatchStatus.TEST_EXECUTION_STARTED:
                self._transition_fix_validation(record, fix_record, "tests_running")

        patch._status_observer = observe_patch_state
        try:
            result = validate_patch_proposal(
                proposal=patch,
                repository_root=record.repo_path,
                approval_token=approval_token,
                allow_isolated_apply=True,
                validation_scope=scope,
                run_id=run_id,
                config=record.result.manifest.configuration if record.result is not None else None,
                run_tests=run_tests,
                run_full_suite=run_full_suite,
            )
        except Exception:
            # The existing sandbox context owns cleanup, including exceptions.
            self._transition_fix_validation(record, fix_record, "validation_failed")
            raise ServiceStateError("validation_not_available") from None
        finally:
            patch._status_observer = None
        if not repair_payload_is_safe(result.model_dump(mode="json")):
            result = PatchValidationResult(
                proposal_id=proposal_id, base_commit=patch.base_commit, patch_hash=patch.patch_hash,
                valid=False, approval_verified=verified, approval_scope=run_id,
                cleanup_status=result.cleanup_status, sandbox_id=result.sandbox_id,
                errors=[FIX_REJECTION_EXPLANATIONS["validation_redaction_failed"]],
            )
        try:
            packet_data, manifest_data = self._update_fix_validation_artifacts(record, fix_record, result)
        except Exception:
            result.valid = False
            result.errors.append("Validation review artifacts could not be safely built.")
            packet_data, manifest_data = None, None
        if result.cleanup_status == "failed":
            validation_status = "cleanup_failed"
        elif result.valid:
            validation_status = "validated"
        else:
            validation_status = "validation_failed"
        self._transition_fix_validation(record, fix_record, validation_status)
        updated = fix_record.response.model_copy(update={
            "validation_status": validation_status,
            "validation_result": result.model_dump(mode="json"),
            "review_packet": packet_data,
            "human_approval_manifest": manifest_data,
        })
        fix_record.response = updated
        with self._lock:
            record.fix_states[finding_id]["response"] = updated
            record.patch_validation_status = validation_status
            self._validations[proposal_id] = result
        return self._fix_validation_response(
            record=record,
            fix_record=fix_record,
            result=result,
            validation_status=validation_status,
            packet_data=packet_data,
            manifest_data=manifest_data,
        )

    validate_fix = validate_fix_proposal

    # ----------------------------------------------------------------------
    # Phase 11C-D/E: explicit apply of one validated FixProposal to the
    # original workspace, safe revert, and bounded apply history. Nothing is
    # ever applied automatically: every path below requires a validated
    # proposal, matching evidence, a clean rechecked workspace, an apply-scoped
    # approval token, and the confirmed exact patch hash.
    # ----------------------------------------------------------------------

    _MAX_APPLY_EVENTS = 50

    def _resolve_fix_record(
        self, run_id: str, finding_id: str, proposal_id: str | None
    ) -> tuple[ReviewRunRecord, FixProposalRecord]:
        record, _ = self._fix_finding_and_record(run_id, finding_id)
        with self._lock:
            if proposal_id is None:
                proposal_id = record.fix_states.get(finding_id, {}).get("proposal_id")
                if not proposal_id:
                    raise ServiceStateError("proposal_not_found")
            fix_record = self._fix_proposals.get(str(proposal_id))
        if fix_record is None:
            raise ServiceStateError("proposal_not_found")
        if fix_record.response.run_id != run_id or fix_record.response.finding_id != finding_id:
            raise ServiceStateError("scope_mismatch")
        return record, fix_record

    def _fix_apply_scope(
        self, run_id: str, finding_id: str, proposal_id: str
    ) -> tuple[ReviewRunRecord, FixProposalRecord, PatchProposal]:
        """Resolve the proposal and independently re-verify its apply scope."""
        record, _ = self._fix_finding_and_record(run_id, finding_id)
        with self._lock:
            fix_record = self._fix_proposals.get(proposal_id)
        if fix_record is None:
            raise ServiceStateError("proposal_not_found")
        response = fix_record.response
        if response.run_id != run_id or response.finding_id != finding_id:
            raise ServiceStateError("scope_mismatch")
        if fix_record.patch_proposal is None:
            raise ServiceStateError("apply_not_available")
        if response.validation_status == "applied":
            raise ServiceStateError("already_applied")
        if response.validation_status == "reverted":
            raise ServiceStateError("apply_not_available")
        if response.validation_status == "validation_failed":
            raise ServiceStateError("apply_validation_failed")
        if response.validation_status != "validated" or response.generation_status != "draft_ready":
            raise ServiceStateError("apply_not_validated")
        return record, fix_record, fix_record.patch_proposal

    def _check_fix_apply_workspace(
        self, record: ReviewRunRecord, fix_record: FixProposalRecord
    ) -> tuple[str, str]:
        """Run the full pre-apply gate; return the current (HEAD, branch).

        Implements the complete Phase 11C-D pre-apply checklist: proposal
        identity, recomputed patch hash, fresh parse, fresh policy decision,
        fresh redaction scan, validation-evidence identity, workspace HEAD,
        and a clean working tree. Any failure raises a stable reason code
        before the apply confirmation path can mutate anything.
        """
        response = fix_record.response
        patch = fix_record.patch_proposal
        if patch is None:  # pragma: no cover - guarded by _fix_apply_scope
            raise ServiceStateError("apply_not_available")
        manifest = record.result.manifest if record.result is not None else None
        if manifest is None or not manifest.base_commit or not manifest.head_commit:
            raise ServiceStateError("apply_scope_invalid")
        repository_identity = self._repository_identity(record.repo_path.resolve())
        provenance = patch.provenance if isinstance(patch.provenance, dict) else {}
        if provenance.get("repair_policy_version") != REPAIR_POLICY_VERSION:
            raise ServiceStateError("apply_policy_version_incompatible")
        scope_matches = (
            response.repository == repository_identity
            and response.base_commit == manifest.base_commit
            and response.head_commit == manifest.head_commit
            and patch.finding_id == response.finding_id
            and patch.base_commit == response.head_commit
            and provenance.get("run_id") == manifest.run_id
            and provenance.get("review_base_commit") == response.base_commit
            and provenance.get("review_head_commit") == response.head_commit
            and provenance.get("repository_identity") == response.repository
            and response.target_files == patch.target_files
            and response.patch_text == patch.unified_diff
            and response.patch_hash == patch.patch_hash
            and compute_patch_hash(patch.unified_diff) == patch.patch_hash
        )
        if not scope_matches:
            raise ServiceStateError("apply_scope_invalid")
        # Validation evidence must exist and identity-match this exact proposal.
        with self._lock:
            validation = self._validations.get(response.proposal_id)
        if validation is None:
            raise ServiceStateError("apply_evidence_missing")
        if not validation.valid:
            raise ServiceStateError("apply_validation_failed")
        if (
            validation.proposal_id != response.proposal_id
            or validation.patch_hash != response.patch_hash
            or validation.base_commit != response.head_commit
            or not validation.resulting_diff_hash
        ):
            raise ServiceStateError("apply_evidence_identity_mismatch")
        if patch.status != PatchStatus.VALIDATED:
            raise ServiceStateError("apply_not_validated")
        # Fresh parse, policy decision, and secret scan on the exact patch text.
        files, parse_errors = parse_unified_diff(patch.unified_diff)
        if parse_errors or not files:
            raise ServiceStateError("patch_parse_failed")
        decision = evaluate_patch_policy(patch, files)
        if not decision.allowed:
            raise ServiceStateError("patch_validation_failed")
        if not audit_patch_redaction(patch.unified_diff).safe:
            raise ServiceStateError("apply_redaction_failed")
        # The workspace must still be exactly where validation left it.
        try:
            current_head = resolve_ref(record.repo_path, "HEAD").commit
        except Exception:
            raise ServiceStateError("repository_state_stale") from None
        if current_head != response.head_commit:
            raise ServiceStateError("repository_state_stale")
        status_res = run_git(["status", "--porcelain"], cwd=record.repo_path, check=False)
        if status_res.stdout.strip():
            raise ServiceStateError("workspace_dirty")
        branch_res = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=record.repo_path, check=False)
        return current_head, branch_res.stdout.strip() or "HEAD"

    def _append_apply_event(
        self,
        fix_record: FixProposalRecord,
        event: str,
        *,
        head: str = "",
        branch: str = "",
        files: list[str] | None = None,
        resulting_diff_hash: str | None = None,
        reason: str | None = None,
    ) -> FixApplyEvent:
        response = fix_record.response
        entry = FixApplyEvent(
            event=event,  # type: ignore[arg-type]
            proposal_id=response.proposal_id,
            finding_id=response.finding_id,
            run_id=response.run_id,
            patch_hash=response.patch_hash,
            resulting_diff_hash=resulting_diff_hash,
            head_commit=head,
            branch_ref=branch,
            files=list(files or response.target_files)[:8],
            reason=reason,
            at=datetime.now(UTC).isoformat(),
        )
        fix_record.apply_events = (fix_record.apply_events + [entry])[-self._MAX_APPLY_EVENTS:]
        return entry

    def apply_fix_proposal(
        self,
        run_id: str,
        finding_id: str,
        proposal_id: str,
        *,
        approval_token: str,
        confirmed_patch_hash: str,
    ) -> FixApplyResponse:
        """Apply one validated FixProposal to the original workspace.

        Requires, in order: a validated proposal with identity-matched
        evidence, the exact current patch hash as confirmation, an
        operation="apply" approval token, and a clean rechecked workspace.
        The applied result must byte-match the validated sandbox diff or
        every touched file is restored immediately. A validate token never
        authorizes this operation.
        """
        record, fix_record, _patch = self._fix_apply_scope(run_id, finding_id, proposal_id)
        with fix_record.operation_lock:
            response = fix_record.response
            if response.validation_status != "validated":
                raise ServiceStateError("apply_not_available")
            if confirmed_patch_hash != response.patch_hash:
                raise ServiceStateError("apply_confirmation_invalid")
            verified, _reason = self._verify_fix_validation_token(
                response=response, token=approval_token, operation="apply"
            )
            if not verified:
                return FixApplyResponse(
                    proposal_id=proposal_id,
                    finding_id=finding_id,
                    run_id=run_id,
                    operation="apply",
                    apply_status="not_applied",
                    approval_verified=False,
                    patch_hash=response.patch_hash,
                    errors=[FIX_REJECTION_EXPLANATIONS["validation_token_invalid"]],
                )
            head, branch = self._check_fix_apply_workspace(record, fix_record)
            with self._lock:
                validation = self._validations.get(proposal_id)
            if validation is None or not validation.resulting_diff_hash:
                raise ServiceStateError("apply_evidence_missing")
            patch = fix_record.patch_proposal
            if patch is None:  # pragma: no cover - guarded above
                raise ServiceStateError("apply_not_available")
            apply_result = apply_patch_to_worktree(
                patch,
                record.repo_path,
                expected_resulting_diff_hash=validation.resulting_diff_hash,
            )
            if not apply_result.success:
                self._append_apply_event(
                    fix_record, "apply_rejected",
                    head=head, branch=branch,
                    files=apply_result.changed_files or list(response.target_files),
                    reason="apply_failed",
                )
                reason = (
                    "resulting_diff_mismatch"
                    if apply_result.failure_kind == "diff_mismatch"
                    else "workspace_dirty"
                    if apply_result.failure_kind == "workspace_dirty"
                    else "apply_failed"
                )
                raise ServiceStateError(reason)
            try:
                patch.transition_to(PatchStatus.APPLIED, reason="applied_to_workspace")
            except ValueError as err:
                restore_worktree_files(record.repo_path, apply_result.original_contents)
                raise ServiceStateError("apply_not_available") from err
            self._transition_fix_validation(record, fix_record, "applied")
            fix_record.apply_state = {
                "status": "applied",
                "patch_hash": response.patch_hash,
                "head_commit": apply_result.head_commit,
                "branch_ref": apply_result.branch_ref,
                "applied_at": datetime.now(UTC).isoformat(),
                "original_contents": apply_result.original_contents,
                "post_apply_file_hashes": apply_result.post_apply_file_hashes,
                "resulting_diff_hash": apply_result.resulting_diff_hash,
                "validated_resulting_diff_hash": validation.resulting_diff_hash,
                "files_changed": list(apply_result.changed_files),
            }
            updated = fix_record.response.model_copy(update={"generation_status": "applied"})
            fix_record.response = updated
            with self._lock:
                record.fix_states[finding_id]["response"] = updated
                record.fix_states[finding_id]["state"] = "applied"
                record.patch_validation_status = "applied"
            self._append_apply_event(
                fix_record, "applied",
                head=apply_result.head_commit, branch=apply_result.branch_ref,
                files=list(apply_result.changed_files),
                resulting_diff_hash=apply_result.resulting_diff_hash,
            )
        return FixApplyResponse(
            proposal_id=proposal_id,
            finding_id=finding_id,
            run_id=run_id,
            operation="apply",
            apply_status="applied",
            approval_verified=True,
            head_commit=apply_result.head_commit,
            branch_ref=apply_result.branch_ref,
            patch_hash=response.patch_hash,
            resulting_diff_hash=apply_result.resulting_diff_hash,
            validated_resulting_diff_hash=validation.resulting_diff_hash,
            files_changed=list(apply_result.changed_files),
            apply_history=list(fix_record.apply_events),
        )

    def revert_fix_apply(
        self,
        run_id: str,
        finding_id: str,
        proposal_id: str,
        *,
        confirmed_patch_hash: str,
    ) -> FixApplyResponse:
        """Revert one applied FixProposal by restoring exact pre-apply bytes.

        The revert only touches the declared target files, only while their
        current bytes still hash-match the post-apply state recorded at apply
        time, and never runs any git mutation (no checkout/reset/stash).
        """
        record, fix_record = self._resolve_fix_record(run_id, finding_id, proposal_id)
        with fix_record.operation_lock:
            response = fix_record.response
            apply_state = fix_record.apply_state
            if not apply_state or apply_state.get("status") != "applied":
                raise ServiceStateError("revert_not_available")
            if confirmed_patch_hash != response.patch_hash:
                raise ServiceStateError("revert_confirmation_invalid")
            target_files = list(apply_state.get("files_changed") or response.target_files)
            try:
                current = capture_worktree_file_state(record.repo_path, target_files)
            except Exception:
                raise ServiceStateError("revert_state_changed") from None
            post_hashes = apply_state.get("post_apply_file_hashes") or {}
            for rel_path, expected_hash in post_hashes.items():
                data = current.get(rel_path)
                if data is None or hashlib.sha256(data).hexdigest() != expected_hash:
                    self._append_apply_event(
                        fix_record, "revert_failed",
                        head=str(apply_state.get("head_commit", "")),
                        branch=str(apply_state.get("branch_ref", "")),
                        files=target_files,
                        reason="revert_state_changed",
                    )
                    raise ServiceStateError("revert_state_changed")
            original_contents = apply_state.get("original_contents") or {}
            if not restore_worktree_files(record.repo_path, original_contents):
                self._append_apply_event(
                    fix_record, "revert_failed",
                    head=str(apply_state.get("head_commit", "")),
                    branch=str(apply_state.get("branch_ref", "")),
                    files=target_files,
                    reason="revert_failed",
                )
                raise ServiceStateError("revert_failed")
            for rel_path, original in original_contents.items():
                target = record.repo_path / rel_path
                if original is None:
                    if target.exists():
                        self._append_apply_event(
                            fix_record, "revert_failed",
                            head=str(apply_state.get("head_commit", "")),
                            branch=str(apply_state.get("branch_ref", "")),
                            files=target_files,
                            reason="revert_failed",
                        )
                        raise ServiceStateError("revert_failed")
                elif target.read_bytes() != original:
                    self._append_apply_event(
                        fix_record, "revert_failed",
                        head=str(apply_state.get("head_commit", "")),
                        branch=str(apply_state.get("branch_ref", "")),
                        files=target_files,
                        reason="revert_failed",
                    )
                    raise ServiceStateError("revert_failed")
            apply_state["status"] = "reverted"
            apply_state["reverted_at"] = datetime.now(UTC).isoformat()
            self._transition_fix_validation(record, fix_record, "reverted")
            with self._lock:
                record.patch_validation_status = "reverted"
            self._append_apply_event(
                fix_record, "reverted",
                head=str(apply_state.get("head_commit", "")),
                branch=str(apply_state.get("branch_ref", "")),
                files=target_files,
                resulting_diff_hash=None,
            )
        return FixApplyResponse(
            proposal_id=proposal_id,
            finding_id=finding_id,
            run_id=run_id,
            operation="revert",
            apply_status="reverted",
            approval_verified=True,
            head_commit=str(apply_state.get("head_commit", "")),
            branch_ref=str(apply_state.get("branch_ref", "")),
            patch_hash=response.patch_hash,
            files_restored=target_files,
            apply_history=list(fix_record.apply_events),
        )

    def get_fix_apply_history(
        self,
        run_id: str,
        finding_id: str,
        proposal_id: str | None = None,
    ) -> FixApplyHistoryResponse:
        """Return the bounded apply history and revert availability."""
        _record, fix_record = self._resolve_fix_record(run_id, finding_id, proposal_id)
        apply_state = fix_record.apply_state or {}
        status = apply_state.get("status", "not_applied")
        return FixApplyHistoryResponse(
            proposal_id=fix_record.response.proposal_id,
            finding_id=finding_id,
            run_id=run_id,
            apply_status=status,  # type: ignore[arg-type]
            revert_available=status == "applied",
            head_commit_at_apply=str(apply_state.get("head_commit", "")),
            branch_ref_at_apply=str(apply_state.get("branch_ref", "")),
            patch_hash=fix_record.response.patch_hash,
            resulting_diff_hash=apply_state.get("resulting_diff_hash"),
            files_changed=list(apply_state.get("files_changed") or []),
            events=list(fix_record.apply_events),
        )

    def reject_fix_proposal(self, run_id: str, finding_id: str, proposal_id: str) -> FixProposalResponse:
        record, _ = self._fix_finding_and_record(run_id, finding_id)
        with self._lock:
            fix_record = self._fix_proposals.get(proposal_id)
        if fix_record is None:
            raise ServiceStateError("proposal_not_found")
        if fix_record.response.run_id != run_id or fix_record.response.finding_id != finding_id:
            raise ServiceStateError("scope_mismatch")
        if (fix_record.response.generation_status != "draft_ready" or fix_record.patch_proposal is None
                or fix_record.response.validation_status != "approval_required"):
            raise ServiceStateError("not_rejectable")
        try:
            fix_record.patch_proposal.transition_to(PatchStatus.REJECTED, reason="rejected_by_user")
        except ValueError:
            pass
        response = fix_record.response.model_copy(update={
            "generation_status": "rejected",
            "rejection_reason": "rejected_by_user",
            "rejection_explanation": "The fix proposal was rejected by the user.",
        })
        fix_record.response = response
        self._store_fix_state(record, finding_id, "rejected", proposal_id, response, "rejected_by_user")
        return response

    def regenerate_fix_proposal(self, run_id: str, finding_id: str) -> FixProposalResponse:
        record, _ = self._fix_finding_and_record(run_id, finding_id)
        with self._lock:
            stored = dict(record.fix_states.get(finding_id, {}))
            previous = self._fix_proposals.get(str(stored.get("proposal_id")))
            if previous is not None and previous.response.validation_status in {
                "approved_for_validation", "validating", "applied_in_isolated_worktree", "tests_running",
            }:
                raise ServiceStateError("not_regenerable")
        if stored.get("state"):
            # Keep the previous proposal id so a different regenerated draft supersedes it.
            self._store_fix_state(record, finding_id, "regeneration_requested", stored.get("proposal_id"), None)
        return self.request_fix_proposal(run_id, finding_id, regenerate=True)
