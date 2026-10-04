"""Materialization of provider patch suggestions into Phase 6 PatchProposals.

This module composes the existing Phase 6 patch pipeline (unified diff parser,
path normalization, patch policy, proposal construction, redaction audit, and
validator) for opt-in provider-suggested draft patches.  It creates proposals
and evaluates policy only: it never applies patches, never generates approval
tokens, and never runs repository code, tests, or builds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeatlas.review.packet import ReviewPacket

from .models import PatchProposal, PatchStatus
from .parser import parse_unified_diff, unsafe_diff_path_reason
from .proposal import audit_patch_redaction, create_patch_proposal
from .validator import validate_patch_proposal

# Provider suggestions must anchor to changed lines unless policy explicitly
# allows otherwise; provider patches always require human approval.
PROVIDER_PATCH_POLICY_DEFAULTS: dict[str, Any] = {
    "require_changed_line_anchor": True,
    "require_human_approval": True,
    "allow_apply": False,
}

# Keys that must never appear on a suggestion (defense in depth; the live
# reviewer already rejects these before materialization).
_FORBIDDEN_SUGGESTION_KEYS = frozenset({
    "approval_token", "approval", "token", "command", "commands", "shell", "shell_command",
    "execute", "execution", "tool_call", "tool_calls", "status", "fixability", "validated",
    "approved", "applied", "merged", "fixed", "tests_passed", "test_results", "compiler_output",
    "policy_decision", "policy_override", "patch_hash", "credential", "credentials", "api_key",
})


@dataclass
class SuggestionRejection:
    """One rejected patch suggestion with a safe, redacted reason."""

    suggestion_id: str
    finding_id: str
    reason: str


@dataclass
class SuggestionMaterialization:
    """Outcome of materializing provider patch suggestions."""

    proposals: list[PatchProposal] = field(default_factory=list)
    rejections: list[SuggestionRejection] = field(default_factory=list)
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    validation_summaries: list[dict[str, Any]] = field(default_factory=list)
    redaction_failures: int = 0
    # Always true when suggestions are materialized: the system never applies
    # provider patches automatically.
    automatic_application_blocked: bool = True

    @property
    def accepted_count(self) -> int:
        return len(self.proposals)

    @property
    def rejected_count(self) -> int:
        return len(self.rejections)


def _finding_anchor_error(finding: dict[str, Any], packet: ReviewPacket) -> str | None:
    """Return a rejection reason when the source finding lacks a valid anchor."""
    file = str(finding.get("file", "")).replace("\\", "/")
    if file not in packet.changed_files:
        return f"source finding {finding.get('id')} is not on a changed file"
    ranges = packet.changed_line_ranges.get(file, [])
    start = int(finding.get("start_line", 0))
    end = int(finding.get("end_line", 0))
    if not any(not (end < r[0] or start > r[1]) for r in ranges):
        return f"source finding {finding.get('id')} is not anchored to changed lines"
    return None


def _verify_hunks_match_snapshot(snapshot_path: Path, parsed_files: list[Any]) -> list[str]:
    """Read-only content check: hunk old-side lines must match the head snapshot."""
    errors: list[str] = []
    for pf in parsed_files:
        if pf.operation != "modify":
            continue
        full = snapshot_path / pf.path
        try:
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            errors.append(f"patch target not readable in repository snapshot: {pf.path}")
            continue
        for hunk in pf.hunks:
            old_side = [line[1:].rstrip("\r") for line in hunk.lines if line[:1] in {" ", "-"}]
            start = hunk.old_start - 1
            expected = [line.rstrip("\r") for line in lines[start:start + hunk.old_lines]]
            if old_side != expected:
                errors.append(
                    f"hunk does not apply to repository snapshot at {pf.path}:{hunk.old_start} "
                    "(stale base commit or patch conflict)"
                )
    return errors


def _hunk_anchor_error(parsed_files: list[Any], packet: ReviewPacket, require_anchor: bool) -> str | None:
    """Reject patches whose hunks target unchanged or deleted lines.

    Provider patches apply to the head snapshot, so hunk ``old_start`` ranges
    are head-coordinates and compare directly against the packet's changed
    line ranges.
    """
    if not require_anchor:
        return None
    for pf in parsed_files:
        ranges = packet.changed_line_ranges.get(pf.path, [])
        for hunk in pf.hunks:
            old_start = hunk.old_start
            old_end = hunk.old_start + max(hunk.old_lines - 1, 0)
            if not any(not (old_end < r[0] or old_start > r[1]) for r in ranges):
                return (
                    f"patch hunk targets unchanged or deleted lines at {pf.path}:{hunk.old_start} "
                    "(changed-line anchor required by policy)"
                )
    return None


def materialize_patch_suggestions(
    suggestions: list[dict[str, Any]],
    *,
    packet: ReviewPacket,
    findings: list[dict[str, Any]],
    snapshot_path: Path | str,
    base_commit: str,
    provider_name: str,
    provider_version: str,
    model_name: str,
    run_id: str,
    config: dict[str, Any] | None = None,
) -> SuggestionMaterialization:
    """Turn sanitized provider suggestions into validated draft PatchProposals.

    Every suggestion passes through the Phase 6 machinery: unified diff
    parsing, path safety, protected-path policy, budgets, redaction audit,
    deterministic proposal construction, and structural validation with
    ``allow_isolated_apply=False``.  No proposal is ever applied here.
    """
    result = SuggestionMaterialization()
    root = Path(snapshot_path)
    cfg = dict(config or {})
    raw_patch_cfg = cfg.get("patch", {})
    patch_cfg = dict(raw_patch_cfg) if isinstance(raw_patch_cfg, dict) else {}
    patch_cfg.update(PROVIDER_PATCH_POLICY_DEFAULTS)

    allowed_files = set(packet.changed_files) | {c.file for c in packet.context_candidates}
    findings_by_id: dict[str, dict[str, Any]] = {}
    for f in findings:
        if not f.get("id"):
            continue
        findings_by_id[str(f["id"])] = f
        # A reviewer finding merged with a deterministic finding keeps its
        # anchor through the merged record's provenance.
        merged_reviewer_id = f.get("provenance", {}).get("reviewer_finding_id")
        if merged_reviewer_id:
            findings_by_id[str(merged_reviewer_id)] = f
    seen_hashes: set[tuple[str, str]] = set()

    for suggestion in suggestions:
        sid = str(suggestion.get("suggestion_id", "unknown"))
        fid = str(suggestion.get("finding_id", ""))

        def reject(reason: str) -> None:
            result.rejections.append(SuggestionRejection(suggestion_id=sid, finding_id=fid, reason=reason))

        # 0. Defense in depth: forbidden fields never materialize.
        forbidden = [k for k in suggestion if k.strip().lower() in _FORBIDDEN_SUGGESTION_KEYS]
        if forbidden:
            reject(f"forbidden field(s) on suggestion: {sorted(forbidden)}")
            continue

        # 1. Source finding must exist and be anchored to changed lines.
        finding = findings_by_id.get(fid)
        if finding is None:
            reject(f"no source finding with id {fid}")
            continue
        anchor_error = _finding_anchor_error(finding, packet)
        if anchor_error:
            reject(anchor_error)
            continue

        # 2. Target files must live inside the review packet's allowed files.
        targets = [str(t).replace("\\", "/").strip("/") for t in suggestion.get("target_files", [])]
        outside = [t for t in targets if t not in allowed_files]
        if outside:
            reject(f"patch target outside review packet: {outside[0]}")
            continue

        # 3. Structural diff parsing with configured budgets.
        diff_text = str(suggestion.get("unified_diff", ""))
        parsed_files, parse_errors = parse_unified_diff(
            diff_text,
            max_patch_bytes=int(patch_cfg.get("max_patch_bytes", 500_000)),
            max_files=int(patch_cfg.get("max_files", 5)),
            max_changed_lines=int(patch_cfg.get("max_changed_lines", 150)),
        )
        if parse_errors:
            reject(f"malformed unified diff: {parse_errors[0]}")
            continue

        # 3a. Both diff header sides must stay inside the repository.
        unsafe_path = unsafe_diff_path_reason(diff_text)
        if unsafe_path:
            reject(unsafe_path)
            continue

        # 3b. File operations: new/delete/rename are prohibited for provider
        #     patches by default policy, with an explicit early reason.
        operation_error = None
        for pf in parsed_files:
            allowed_op = patch_cfg.get(f"allow_{pf.operation}_files", False)
            if pf.operation in {"add", "delete", "rename"} and not allowed_op:
                operation_error = f"file operation prohibited by policy: {pf.operation} ({pf.path})"
                break
        if operation_error:
            reject(operation_error)
            continue

        # 4. Parsed paths must match the declared targets and stay allowed.
        parsed_paths = [pf.path for pf in parsed_files]
        if sorted(parsed_paths) != sorted(set(targets)):
            reject("target_files do not match the unified diff file set")
            continue
        outside_parsed = [p for p in parsed_paths if p not in allowed_files]
        if outside_parsed:
            reject(f"patch target outside review packet: {outside_parsed[0]}")
            continue

        # 5. Hunks must anchor to changed lines (default policy).
        anchor_error = _hunk_anchor_error(parsed_files, packet, bool(patch_cfg["require_changed_line_anchor"]))
        if anchor_error:
            reject(anchor_error)
            continue

        # 6. Read-only content match against the head snapshot.
        content_errors = _verify_hunks_match_snapshot(root, parsed_files)
        if content_errors:
            reject(content_errors[0])
            continue

        # 7. Redaction audit across diff, rationale, and expected behavior.
        redaction_text = "\n".join([
            diff_text,
            str(suggestion.get("rationale", "")),
            str(suggestion.get("expected_behavior", "")),
        ])
        redaction = audit_patch_redaction(redaction_text)
        if not redaction.safe:
            result.redaction_failures += 1
            reject("patch introduces likely secret material; proposal not created")
            continue

        # 8. Duplicate suppression per (finding, patch hash).
        proposal = create_patch_proposal(
            finding_id=fid,
            provider_name=provider_name,
            provider_version=provider_version,
            base_commit=base_commit,
            target_files=targets,
            unified_diff=diff_text,
            rationale=str(suggestion.get("rationale", "")),
            expected_behavior=str(suggestion.get("expected_behavior", "")),
            risk_level=str(suggestion.get("risk_level", "medium")),
            requested_action="validate",
            provenance={
                "origin": "provider",
                "provider": provider_name,
                "model": model_name,
                "run_id": run_id,
                "suggestion_id": sid,
                "patch_suggestions_opt_in": True,
            },
        )
        dedup_key = (fid, proposal.patch_hash)
        if dedup_key in seen_hashes:
            reject(f"duplicate suggestion for finding {fid} with identical patch hash")
            continue
        seen_hashes.add(dedup_key)

        # Draft transparency: carry suggestion limitations plus the standing
        # provider-draft caveat into the proposal record.
        proposal.limitations.extend(
            [x for x in suggestion.get("limitations", []) if isinstance(x, str)]
        )
        proposal.limitations.append("Draft provider suggestion; requires validation and human approval")

        # 9. Phase 6 policy + structural validation; never isolated-apply here.
        validation = validate_patch_proposal(
            proposal,
            root,
            config={"patch": patch_cfg},
            allow_isolated_apply=False,
        )
        if not validation.valid:
            reject(f"proposal validation failed: {'; '.join(validation.errors) or 'unknown'}")
            continue
        if proposal.status == PatchStatus.APPROVED:
            # Provider patches always require human approval, regardless of config.
            proposal.status = PatchStatus.REQUIRES_HUMAN_APPROVAL
            proposal.limitations.append("Provider patch downgraded to requires_human_approval")

        result.proposals.append(proposal)
        result.policy_decisions.append(dict(proposal.policy_decision or {}))
        result.validation_summaries.append({
            "proposal_id": proposal.proposal_id,
            "valid": validation.valid,
            "applies_cleanly": validation.applies_cleanly,
            "errors": list(validation.errors),
            "execution_allowed": validation.execution_allowed,
        })

    return result


__all__ = [
    "PROVIDER_PATCH_POLICY_DEFAULTS",
    "SuggestionMaterialization",
    "SuggestionRejection",
    "materialize_patch_suggestions",
]
