"""State management, lifecycle tracking, and review orchestration for CodeAtlas service."""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeatlas.git.repository import validate_repository
from codeatlas.orchestrator.review import ReviewResult, run_review
from codeatlas.patching.models import PatchProposal, PatchStatus, PatchValidationResult
from codeatlas.patching.proposal import create_patch_proposal
from codeatlas.patching.validator import validate_patch_proposal
from codeatlas.review.packet import ReviewPacket
from codeatlas.service.models import (
    FeedbackLabel,
    FindingContext,
    FindingDetailResponse,
    FindingFeedbackResponse,
    FindingSummary,
    LifecycleState,
    PatchProposalResponse,
    ReviewCreateRequest,
    ReviewStatusResponse,
    ValidateProposalResponse,
)


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


class ReviewStateManager:
    """Thread-safe state manager for reviews, findings, proposals, and validations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reviews: dict[str, ReviewRunRecord] = {}
        self._proposals: dict[str, tuple[PatchProposal, Path]] = {}
        self._validations: dict[str, PatchValidationResult] = {}

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
