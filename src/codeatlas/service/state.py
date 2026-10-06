"""State management, lifecycle tracking, and review orchestration for CodeAtlas service."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from codeatlas.adapters import select_language_adapter
from codeatlas.findings.models import Finding
from codeatlas.git.refs import resolve_ref
from codeatlas.git.repository import validate_repository
from codeatlas.git.snapshot import temporary_snapshot
from codeatlas.orchestrator.manifest import RunManifest
from codeatlas.orchestrator.repair import RepairOrchestrator, record_repair_result
from codeatlas.orchestrator.repair_models import RepairLimits, RepairRepositoryState, repair_payload_is_safe
from codeatlas.orchestrator.review import ReviewResult, run_review
from codeatlas.patching.models import PatchProposal, PatchStatus, PatchValidationResult
from codeatlas.patching.proposal import create_patch_proposal
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
    FixEligibilityResponse,
    FixProposalLifecycle,
    FixProposalResponse,
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
        self.created_at = datetime.now(timezone.utc).isoformat()
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
                record.completed_at = datetime.now(timezone.utc).isoformat()
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
                record.completed_at = datetime.now(timezone.utc).isoformat()
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
            record.completed_at = datetime.now(timezone.utc).isoformat()

        except Exception as err:
            record.status = "failed"
            record.progress_text = f"Review failed: {err}"
            record.errors.append(str(err))
            record.completed_at = datetime.now(timezone.utc).isoformat()
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
            record.completed_at = datetime.now(timezone.utc).isoformat()
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
                "updated_at": datetime.now(timezone.utc).isoformat(),
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

    def reject_fix_proposal(self, run_id: str, finding_id: str, proposal_id: str) -> FixProposalResponse:
        record, _ = self._fix_finding_and_record(run_id, finding_id)
        with self._lock:
            fix_record = self._fix_proposals.get(proposal_id)
        if fix_record is None:
            raise ServiceStateError("proposal_not_found")
        if fix_record.response.run_id != run_id or fix_record.response.finding_id != finding_id:
            raise ServiceStateError("scope_mismatch")
        if fix_record.response.generation_status != "draft_ready" or fix_record.patch_proposal is None:
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
        if stored.get("state"):
            # Keep the previous proposal id so a different regenerated draft supersedes it.
            self._store_fix_state(record, finding_id, "regeneration_requested", stored.get("proposal_id"), None)
        return self.request_fix_proposal(run_id, finding_id, regenerate=True)
