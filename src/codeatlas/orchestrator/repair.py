"""One-finding, offline proposal planning composed from the existing patch pipeline.

The caller supplies a trusted run manifest, current run/revision state, and an
existing head Snapshot. This layer reads only the bounded target file. It never
creates a sandbox, generates approval tokens, or enters isolated application.
"""

from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError

from codeatlas.adapters import select_language_adapter
from codeatlas.evidence import EvidenceLogger
from codeatlas.findings.models import Finding
from codeatlas.git.snapshot import Snapshot
from codeatlas.patching.models import PatchFile, PatchProposal, PatchStatus, PatchValidationResult
from codeatlas.patching.parser import parse_unified_diff, unsafe_diff_path_reason
from codeatlas.patching.proposal import normalize_diff
from codeatlas.patching.suggestions import materialize_patch_suggestions, preview_patch
from codeatlas.review.packet import ContextItem, PacketSizeStats, ReviewPacket
from codeatlas.review.provider import RepairProvider, ReviewerProvider, ReviewerResult
from codeatlas.review.quality import QUALITY_VERSION, assess_finding, has_deterministic_support
from codeatlas.review.packet import redact_text
from .manifest import RunManifest
from .repair_models import (
    REPAIR_POLICY_VERSION, RepairContext, RepairLimits, RepairRepositoryState, RepairResult, RepairSymbol,
    repair_payload_is_safe,
)

_LIMITATIONS = [
    "Static proposal validation only; no patch has been applied or approved.",
    "Tests, formatters, type checks, and builds have not run; passing static checks does not prove a fix correct.",
    "Human approval and the existing isolated validator are required before isolated application or test execution.",
]


class RepairRejected(ValueError):
    """A bounded repair request failed closed before a proposal was returned."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _RepairSuggestion(BaseModel):
    """Strict projection of the existing provider patch-suggestion contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    suggestion_id: str = Field(min_length=1, max_length=128)
    finding_id: str = Field(min_length=1, max_length=128)
    unified_diff: str = Field(min_length=1, max_length=64_000)
    rationale: str = Field(min_length=1, max_length=2_000)
    expected_behavior: str = Field(min_length=1, max_length=2_000)
    target_files: list[str] = Field(min_length=1, max_length=1)
    risk_level: str = Field(default="medium", pattern=r"^(low|medium|high)$")
    limitations: list[Annotated[str, Field(max_length=512)]] = Field(default_factory=list, max_length=10)
    provider_provenance: dict[str, Annotated[str, Field(max_length=512)]] = Field(default_factory=dict, max_length=8)


def _safe_path(path: str) -> bool:
    return (
        bool(path) and len(path) <= 512 and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
        and not any(ord(c) < 32 for c in path)
        and unsafe_diff_path_reason(f"--- a/{path}\n+++ b/{path}\n") is None
    )


def _read_source(snapshot: Snapshot, path: str, budget: int) -> str:
    if snapshot.path.is_symlink():
        raise ValueError("symlink_snapshot")
    root = snapshot.path.resolve()
    target = root
    for part in path.split("/"):
        target = target / part
        if target.is_symlink():
            raise ValueError("symlink_target")
    if not target.is_file():
        raise ValueError("invalid_location")
    with target.open("rb") as stream:
        data = stream.read(budget + 1)
    if len(data) > budget:
        raise ValueError("source_budget_exceeded")
    if b"\x00" in data:
        raise ValueError("binary_target")
    return data.decode("utf-8")


def _scope_is_bounded(patch: PatchFile, context: RepairContext) -> bool:
    """Every hunk stays in supplied code, with an actual edit at the finding."""
    anchored = False
    start, end = context.code_line_range
    finding_start, finding_end = context.changed_line_range
    for hunk in patch.hunks:
        if not (start <= hunk.old_start <= end and hunk.old_start + max(hunk.old_lines - 1, 0) <= end):
            return False
        line_no = hunk.old_start + (1 if hunk.old_lines == 0 else 0)
        block_start: int | None = None
        for line in hunk.lines:
            if line.startswith(" "):
                block_start = None
            elif line.startswith(("+", "-")):
                if block_start is None:
                    block_start = line_no
                anchor_line = block_start if line.startswith("+") else line_no
                if finding_start <= anchor_line <= finding_end + (1 if line.startswith("+") else 0):
                    anchored = True
            if line.startswith((" ", "-")):
                line_no += 1
    return anchored


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate provider output key")
        result[key] = value
    return result


def _repository_identity(repository: str) -> str:
    """Keep only the repository identity, never an absolute local path."""
    parts = [part for part in repository.replace("\\", "/").split("/") if part]
    return (parts[-1] if parts else "repository")[:128]


class RepairOrchestrator:
    """Bounded offline composition of ReviewerProvider and patch materialization.

    Providers are trusted in-process implementations of the existing protocol;
    they must explicitly declare ``network_access=False``. Network-capable and
    undeclared providers fail closed before invocation. Their output has no
    authority over scope, policy, approval, quality, or execution.
    """

    def __init__(self, provider: ReviewerProvider | RepairProvider, *, limits: RepairLimits | None = None) -> None:
        self.provider = provider
        self.limits = limits or RepairLimits()

    def plan(
        self,
        finding: Finding,
        *,
        packet: ReviewPacket,
        manifest: RunManifest,
        state: RepairRepositoryState,
        snapshot: Snapshot,
        fix_eligible: bool = False,
        evidence: EvidenceLogger | None = None,
    ) -> RepairResult:
        """Receive exactly one finding; return a draft or a bounded rejection.

        ``fix_eligible`` is explicit caller eligibility for a review-only finding,
        not approval. ``state`` must be captured from the active run/repository by
        the caller, independently of provider output. Uncommitted worktree data
        is never read into a repair request.
        """
        def emit(event: str, **details: object) -> None:
            if evidence is not None:
                evidence.emit(event, **details)

        def reject(reason: str, *, review_only: bool = False) -> RepairResult:
            emit("repair_rejected", reason=reason, status="review_only" if review_only else "rejected")
            return RepairResult(status="review_only" if review_only else "rejected", reason=reason,
                                limitations=list(_LIMITATIONS))

        if not isinstance(finding, Finding):
            return reject("exactly_one_finding_required")
        # Revalidate caller models: model_copy/assignment can otherwise bypass
        # Pydantic's contract. Error output never includes input values.
        try:
            finding = Finding.model_validate(finding.model_dump())
            state = RepairRepositoryState.model_validate(state.model_dump())
            limits = RepairLimits.model_validate(self.limits.model_dump())
            manifest = RunManifest.model_validate(manifest.model_dump())
            packet = ReviewPacket.model_validate(packet.model_dump())
            if not isinstance(snapshot, Snapshot) or not isinstance(snapshot.path, Path):
                return reject("invalid_input")
        except (ValidationError, AttributeError):
            return reject("invalid_input")
        if manifest.run_id != state.run_id or finding.provenance.get("run_id", state.run_id) != state.run_id:
            return reject("stale_run")
        if (manifest.review_packet_id != packet.packet_id or manifest.errors
                or manifest.repository != state.repository or packet.repository != state.repository
                or manifest.base_commit != state.base_commit or packet.base_commit != state.base_commit
                or manifest.head_commit != state.head_commit or packet.head_commit != state.head_commit
                or snapshot.commit != state.head_commit):
            return reject("incompatible_repository_state")
        try:
            snapshot_root = snapshot.path.resolve(strict=True)
            repository_root = Path(state.repository).expanduser().resolve()
            if (snapshot_root == repository_root or snapshot_root.is_relative_to(repository_root)
                    or repository_root.is_relative_to(snapshot_root)):
                return reject("incompatible_repository_state")
        except (OSError, RuntimeError):
            return reject("incompatible_repository_state")
        records = [item for item in manifest.findings if item.get("id") == finding.id]
        if len(records) != 1:
            return reject("finding_not_in_run")
        try:
            recorded = Finding.model_validate(records[0])
        except ValidationError:
            return reject("invalid_run_finding")
        if finding.model_dump() != recorded.model_dump():
            return reject("stale_finding")
        if (finding.quality_decision == "suppress_duplicate"
                or any(finding.id in item.get("suppressed_finding_ids", []) for item in manifest.findings)):
            return reject("suppressed_duplicate")
        if finding.status == "abstained" or finding.quality_decision == "abstain":
            return reject("abstained")
        if finding.quality_decision == "suppress_low_evidence" or finding.evidence_strength in {"none", "weak"}:
            return reject("low_evidence")
        if finding.unsupported_flow or finding.provenance.get("unsupported_flow"):
            return reject("unsupported_flow")
        if (finding.ambiguity_score or finding.provenance.get("ambiguity_score")
                or finding.provenance.get("ambiguous_location") or finding.provenance.get("quality_conflict")):
            return reject("ambiguous")
        if (finding.quality_decision == "review_only" or finding.status == "review_only") and fix_eligible is not True:
            return reject("review_only_without_fix_eligibility")
        if finding.status in {"rejected", "validated"} or finding.fixability in {"unknown", "not_fixable", "validated"}:
            return reject("finding_not_fix_eligible")
        if (finding.quality_version != QUALITY_VERSION or finding.truncation_penalty
                or finding.confidence < 0.70 or not has_deterministic_support(finding) or not finding.evidence):
            return reject("low_evidence")
        file = finding.file
        if not _safe_path(file) or file not in packet.changed_files:
            return reject("invalid_location")
        ranges = packet.changed_line_ranges.get(file, [])
        if not any(len(r) == 2 and 1 <= r[0] <= finding.start_line <= finding.end_line <= r[1] for r in ranges):
            return reject("invalid_location")
        assessed = assess_finding(finding, packet=packet)
        if assessed.quality_decision not in {"report", "report_with_uncertainty", "review_only"}:
            return reject("quality_gate_failed")
        if assessed.ambiguity_score or assessed.unsupported_flow or assessed.truncation_penalty:
            return reject("quality_gate_failed")
        if assessed.quality_decision == "review_only" and fix_eligible is not True:
            return reject("review_only_without_fix_eligibility")
        adapter = select_language_adapter(file)
        if adapter is None:
            return reject("unsupported_language", review_only=True)
        if adapter.classify_fixability(finding) != "fix_eligible":
            return reject("language_review_only", review_only=True)
        if any(fnmatchcase(file, pattern) for pattern in limits.prohibited_paths) or adapter.is_protected(file):
            return reject("protected_or_generated_target")
        if (getattr(self.provider, "network_access", None) is not False
                or str(getattr(self.provider, "name", "")).lower() in {"live", "http", "httptransport"}
                or not (callable(getattr(self.provider, "review", None))
                        or callable(getattr(self.provider, "propose", None))
                        or callable(getattr(self.provider, "repair", None)))):
            return reject("offline_provider_required")
        provider_metadata = {key: getattr(self.provider, key, default) for key, default in (
            ("name", "repair-provider"), ("version", REPAIR_POLICY_VERSION), ("model_name", "offline"),
        )}
        if (any(not isinstance(value, str) or not value or len(value) > 128 for value in provider_metadata.values())
                or not repair_payload_is_safe(provider_metadata)):
            return reject("invalid_provider_metadata")
        if not packet.redaction_status.redacted or packet.redaction_status.failed_checks or packet.redaction_status.raw_value_matches:
            return reject("packet_redaction_failed")
        try:
            source = _read_source(snapshot, file, limits.max_source_bytes)
        except (OSError, UnicodeError, ValueError):
            return reject("unreadable_or_unbounded_target")
        if adapter.is_protected(file, source):
            return reject("protected_or_generated_target")
        lines = source.splitlines()
        if finding.end_line > len(lines):
            return reject("invalid_location")
        # Required packet snippets must match their actual head coordinates.
        matched = False
        for item in packet.context_candidates:
            if item.file != file or item.source_type not in {"changed_code", "symbol"}:
                continue
            if len(item.line_range) != 2:
                return reject("invalid_location")
            start, end = item.line_range
            if start <= finding.start_line <= finding.end_line <= end:
                expected, _ = redact_text("\n".join(lines[start - 1:end]))
                if (start < 1 or end > len(lines) or item.truncation_status != "full" or not item.content
                        or item.content.splitlines() != expected.splitlines()):
                    return reject("stale_or_truncated_context")
                matched = True
        if not matched:
            return reject("missing_required_context")
        parsed_source = adapter.parse_modified_file(file, source)
        if parsed_source.errors:
            return reject("source_parse_failed")
        symbols = [s for s in parsed_source.symbols if s.start_line <= finding.start_line <= finding.end_line <= s.end_line]
        symbol = min(symbols, key=lambda s: s.end_line - s.start_line) if symbols else None
        start = symbol.start_line if symbol else max(1, finding.start_line - 10)
        end = symbol.end_line if symbol else min(len(lines), finding.end_line + 10)
        code, _ = redact_text("\n".join(lines[start - 1:end]))
        if end - start + 1 > limits.max_context_lines or len(code.encode("utf-8")) > limits.max_context_bytes:
            return reject("required_context_exceeds_bounds")
        available_tests = packet.relevant_tests[:64]
        related_tests = adapter.discover_targeted_tests(file, available_tests, finding.tests_consulted)
        # Project named imports for this file, never arbitrary packet config,
        # provider provenance, transcripts, observed logs, or unrelated files.
        imports = tuple(str(i.get("module", "")) for i in packet.relevant_imports[:20]
                        if i.get("file") == file and isinstance(i.get("module"), str) and i["module"])
        optional_context_truncated = len(imports) > 10
        try:
            context = RepairContext(
                finding_id=finding.id, run_id=state.run_id,
                repository_identity=_repository_identity(state.repository),
                base_commit=state.base_commit, head_commit=state.head_commit, language=adapter.language,
                changed_file=file, changed_line_range=(finding.start_line, finding.end_line),
                containing_symbol=RepairSymbol(name=symbol.name, kind=symbol.kind, start_line=start, end_line=end) if symbol else None,
                code_line_range=(start, end), code_context=code, category=finding.category,
                claim=finding.claim, impact=finding.impact, deterministic_evidence=tuple(finding.evidence),
                quality_decision=finding.quality_decision, confidence=finding.confidence,
                evidence_strength=finding.evidence_strength, repository_context=imports[:10],
                related_tests=related_tests, limits=limits, target_file_limits=limits.max_target_files,
                maximum_patch_line_limits=limits.max_patch_lines, prohibited_paths=limits.prohibited_paths,
                context_truncated=optional_context_truncated,
            )
        except ValidationError:
            return reject("unsafe_or_unbounded_context")
        emit("repair_context_built", run_id=state.run_id, finding_id=finding.id, language=adapter.language, status="ok")
        provider_packet = _provider_packet(context)
        try:
            proposer = getattr(self.provider, "propose", None) or getattr(self.provider, "repair", None)
            if callable(proposer):
                output = proposer(context.model_copy(deep=True))
                if isinstance(output, ReviewerResult):
                    result = output
                else:
                    if isinstance(output, str):
                        if len(output.encode("utf-8")) > limits.max_patch_bytes + 8_000:
                            return reject("provider_output_exceeds_bounds")
                        output = json.loads(output, object_pairs_hook=_unique_json_object)
                    if hasattr(output, "model_dump"):
                        output = output.model_dump(mode="json")
                    if not isinstance(output, dict):
                        return reject("invalid_provider_result")
                    if (len(json.dumps(output).encode("utf-8")) > limits.max_patch_bytes + 8_000
                            or not repair_payload_is_safe(output)):
                        return reject("unsafe_or_unbounded_provider_output")
                    if "patch_suggestions" in output and set(output) - set(ReviewerResult.model_fields):
                        return reject("invalid_provider_output")
                    suggestions = output.get("patch_suggestions", [output])
                    if not isinstance(suggestions, list) or len(suggestions) != 1:
                        return reject("exactly_one_proposal_required")
                    result = ReviewerResult(
                        **({"provider_name": provider_metadata["name"], **output} if "patch_suggestions" in output else {
                            "provider_name": provider_metadata["name"], "patch_suggestions": suggestions,
                        }),
                    )
            else:
                output = self.provider.review(provider_packet)  # type: ignore[union-attr]
                result = output if isinstance(output, ReviewerResult) else None
            if result is None:
                return reject("invalid_provider_result")
            result = ReviewerResult.model_validate(result.model_dump())
            if (result.validation_status not in {"unvalidated", "accepted", "sanitized"}
                    or result.abstentions or result.validation_errors or result.findings):
                return reject("provider_abstained_or_failed")
            if len(result.patch_suggestions) != 1:
                return reject("exactly_one_proposal_required")
            # All output is scanned, including fields that will be discarded.
            serialized = result.model_dump_json()
            if len(serialized.encode("utf-8")) > limits.max_patch_bytes + 8_000:
                return reject("provider_output_exceeds_bounds")
            if not repair_payload_is_safe(result.model_dump(mode="json")):
                return reject("provider_output_redaction_failed")
            suggestion = _RepairSuggestion.model_validate(result.patch_suggestions[0])
        except (ValidationError, ValueError, TypeError):
            return reject("invalid_provider_output")
        except Exception:
            return reject("provider_failed")
        if (suggestion.finding_id != finding.id or suggestion.target_files != [file]
                or not suggestion.rationale.strip() or not suggestion.expected_behavior.strip()):
            return reject("provider_scope_or_explanation_invalid")
        syntax_valid: bool | None = None
        syntax_limits = list(parsed_source.limitations)
        if optional_context_truncated:
            syntax_limits.append("Optional repository imports were truncated to the repair context limit.")

        normalized_diff = normalize_diff(suggestion.unified_diff)
        parsed_patch, patch_errors = parse_unified_diff(
            normalized_diff,
            max_patch_bytes=limits.max_patch_bytes,
            max_files=limits.max_target_files,
            max_changed_lines=limits.max_patch_lines,
        )
        if patch_errors or len(parsed_patch) != 1:
            return reject("patch_parse_failed")
        if (parsed_patch[0].path != file or unsafe_diff_path_reason(normalized_diff)
                or not _scope_is_bounded(parsed_patch[0], context)):
            return reject("patch_scope_invalid")
        adapter_errors = adapter.validate_patch_constraints(parsed_patch[0], source)
        if adapter_errors:
            return reject("language_constraint_failed")
        try:
            preview = preview_patch(source, parsed_patch[0])
        except ValueError:
            return reject("patch_preview_failed")
        if len(preview.encode("utf-8")) > limits.max_source_bytes:
            return reject("patch_preview_exceeds_bounds")
        if preview.splitlines() == source.splitlines():
            return reject("empty_repair")
        parsed_preview = adapter.parse_modified_file(file, preview)
        syntax_valid = parsed_preview.syntax_valid
        syntax_limits.extend(parsed_preview.limitations)
        if parsed_preview.errors or syntax_valid is False:
            return reject("syntax_validation_failed")

        materialized = materialize_patch_suggestions(
            [suggestion.model_dump(mode="json") | {"unified_diff": normalized_diff}], packet=_provider_packet(context),
            findings=[finding.model_dump(mode="json")], snapshot_path=snapshot.path,
            base_commit=state.head_commit,
            provider_name=provider_metadata["name"], provider_version=provider_metadata["version"],
            model_name=provider_metadata["model_name"], run_id=state.run_id,
            config={"patch": {"max_files": 1, "max_changed_lines": limits.max_patch_lines,
                              "max_patch_bytes": limits.max_patch_bytes, "enabled": True}},
        )
        if not materialized.proposals:
            # Rejections may quote adversarial paths/text; return a fixed code.
            return reject("patch_validation_failed")
        # Detect a target snapshot change during provider invocation. No writes.
        try:
            if _read_source(snapshot, file, limits.max_source_bytes) != source:
                return reject("repository_state_changed_during_repair")
        except (OSError, UnicodeError, ValueError):
            return reject("repository_state_changed_during_repair")
        proposal: PatchProposal = materialized.proposals[0]
        proposal.status = PatchStatus.REQUIRES_HUMAN_APPROVAL
        proposal.approval_required = True
        proposal.provenance.update({"origin": "repair_orchestrator", "repository_identity": context.repository_identity,
                                    "review_base_commit": state.base_commit, "review_head_commit": state.head_commit,
                                    "repair_policy_version": REPAIR_POLICY_VERSION, "quality_decision": context.quality_decision,
                                    "language": context.language, "context_truncated": context.context_truncated})
        proposal.limitations = list(dict.fromkeys(proposal.limitations + _LIMITATIONS + syntax_limits))[:20]
        summary = materialized.validation_summaries[0] if materialized.validation_summaries else {}
        if (summary.get("valid") is not True or summary.get("execution_allowed") is not False
                or not proposal.policy_decision or proposal.policy_decision.get("allowed") is not True
                or proposal.policy_decision.get("decision") != PatchStatus.REQUIRES_HUMAN_APPROVAL):
            return reject("patch_validation_failed")
        validation = PatchValidationResult(
            valid=summary.get("valid") is True, applies_cleanly=False,
            syntax_valid=syntax_valid, proposal_id=proposal.proposal_id, base_commit=state.head_commit,
            patch_hash=proposal.patch_hash, policy_decision=proposal.policy_decision,
            redaction_audit=proposal.redaction_audit.model_dump(mode="json"),
            changed_files=[file], errors=list(summary.get("errors", [])), warnings=list(_LIMITATIONS + syntax_limits),
            execution_allowed=False, approval_verified=False, approval_scope=state.run_id,
            cleanup_status="not_applicable",
        )
        tools = adapter.report_tool_availability(snapshot.path)
        planned = adapter.allowlisted_check_commands(file, tools) + adapter.check_commands(related_tests, tools)
        emit("repair_proposal_created", run_id=state.run_id, finding_id=finding.id,
             proposal_id=proposal.proposal_id, status=proposal.status, approval_required=True,
             automatic_application_blocked=True, context_truncated=context.context_truncated)
        return RepairResult(status="proposed", context=context, proposal=proposal, validation=validation,
                            limitations=list(dict.fromkeys(_LIMITATIONS + syntax_limits)),
                            tool_availability=tools, checks_planned=planned)

    def propose(self, finding: Finding, **kwargs: object) -> PatchProposal:
        """Return only the existing-compatible proposal on success.

        ``plan`` is available when callers need the bounded rejection/result
        envelope. A failed plan raises a bounded repair rejection and
        includes only a stable reason code, never provider text or source data.
        """
        result = self.plan(finding, **kwargs)  # type: ignore[arg-type]
        if result.proposal is None:
            raise RepairRejected(result.reason or "repair proposal was rejected")
        return result.proposal

    create_proposal = propose


def record_repair_result(manifest: RunManifest, result: RepairResult) -> RunManifest:
    """Return a manifest copy using the existing proposal/validation fields.

    No artifact is written. The caller owns persistence and any later explicit
    approval/isolated-validation operation. Existing proposals are preserved.
    """
    manifest = RunManifest.model_validate(manifest.model_dump())
    result = RepairResult.model_validate(result.model_dump())
    recorded = manifest.model_copy(deep=True)
    recorded.automatic_application_attempted = False
    recorded.automatic_application_blocked = True
    if result.proposal is None:
        if result.reason and result.reason not in recorded.patch_rejection_reasons:
            recorded.patch_rejection_reasons.append(result.reason)
        recorded.patch_suggestions_rejected = (recorded.patch_suggestions_rejected or 0) + 1
        return recorded
    proposal = result.proposal
    if result.validation is None:
        raise RepairRejected("invalid_repair_result")
    if (proposal.provenance.get("run_id") != manifest.run_id or proposal.base_commit != manifest.head_commit
            or proposal.provenance.get("review_base_commit") != manifest.base_commit
            or proposal.provenance.get("repository_identity") != _repository_identity(manifest.repository)):
        raise RepairRejected("incompatible_repository_state")
    if proposal.proposal_id in recorded.patch_proposal_ids:
        return recorded
    recorded.patch_proposals.append(proposal.model_dump(mode="json"))
    recorded.patch_proposal_ids.append(proposal.proposal_id)
    recorded.patch_proposal_statuses.append(proposal.status)
    recorded.patch_policy_decisions.append(dict(proposal.policy_decision or {}))
    recorded.patch_suggestions_received = (recorded.patch_suggestions_received or 0) + 1
    recorded.patch_suggestions_accepted = (recorded.patch_suggestions_accepted or 0) + 1
    recorded.approval_verified = False
    recorded.approval_scope = manifest.run_id
    recorded.proposal_id = proposal.proposal_id
    recorded.patch_hash = proposal.patch_hash
    recorded.isolated_validation_attempted = False
    recorded.isolated_validation_status = "not_requested"
    recorded.sandbox_id = None
    recorded.network_isolation_verified = False
    recorded.applies_cleanly = result.validation.applies_cleanly
    recorded.syntax_valid = result.validation.syntax_valid
    recorded.tests_status = result.validation.tests_status
    recorded.build_status = result.validation.build_status
    recorded.execution_allowed = False
    recorded.validation_limitations = list(result.limitations)
    summaries = recorded.patch_validation or {}
    summaries.setdefault("proposals", []).append(result.validation.model_dump(mode="json"))
    recorded.patch_validation = summaries
    return recorded


def _provider_packet(context: RepairContext) -> ReviewPacket:
    """Build a fresh, one-finding projection for the existing provider interface."""
    finding = {
        "id": context.finding_id, "file": context.changed_file,
        "start_line": context.changed_line_range[0], "end_line": context.changed_line_range[1],
        "category": context.category, "claim": context.claim, "impact": context.impact,
        "evidence": list(context.deterministic_evidence), "evidence_strength": context.evidence_strength,
        "confidence": context.confidence, "quality_decision": context.quality_decision,
    }
    packet = ReviewPacket(
        packet_id="repair-" + hashlib.sha256(context.model_dump_json().encode()).hexdigest()[:24],
        repository=context.repository_identity, base_commit=context.base_commit, head_commit=context.head_commit,
        changed_files=[context.changed_file], changed_line_ranges={context.changed_file: [list(context.changed_line_range)]},
        deterministic_findings=[finding],
        changed_symbols=[dict(context.containing_symbol.model_dump(), file=context.changed_file)] if context.containing_symbol else [],
        context_candidates=[ContextItem(file=context.changed_file, line_range=list(context.code_line_range),
                                       reason="repair target", ranking_score=1.0, source_type="symbol",
                                       content=context.code_context, lines_included=len(context.code_context.splitlines()),
                                       bytes_included=len(context.code_context.encode()))],
        relevant_imports=[{"file": context.changed_file, "module": i} for i in context.repository_context],
        relevant_tests=list(context.related_tests),
        policy_summary={"operation": "propose_one_repair", "approval_required": True,
                        "repair_context": context.model_dump(mode="json")},
        configuration={"repair": {"policy_version": context.policy_version, "max_files": 1,
                                   "max_patch_lines": context.limits.max_patch_lines,
                                    "prohibited_paths": list(context.limits.prohibited_paths)}},
        limitations=list(_LIMITATIONS),
    )
    packet.packet_size_statistics = PacketSizeStats(total_files=1, total_lines=len(context.code_context.splitlines()),
                                                    total_bytes=len(packet.model_dump_json().encode()), findings_count=1)
    return packet


__all__ = ["RepairOrchestrator", "RepairRejected", "RepairContext", "RepairLimits", "RepairRepositoryState", "RepairResult",
           "record_repair_result"]
