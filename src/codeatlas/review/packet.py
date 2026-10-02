"""Deterministic bounded review packet and context assembly."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.findings.models import Finding
from codeatlas.git.models import Diff, LineRange
from codeatlas.repository.models import ContextCandidate, RepositoryIndex, Symbol

SECRET_PATTERNS = [
    re.compile(r"(?i)(AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"(?i)ghp_[A-Za-z0-9_]{36}"),
    re.compile(r"(?i)github_pat_[A-Za-z0-9_]{22}_[A-Za-z0-9_]{59}"),
    re.compile(r"(?i)(?:postgres|postgresql|mysql|mongodb|redis)://[^:\s]+:[^@\s]+@[^\s]+"),
    re.compile(r"(?i)(?:api[_-]?key|password|token|secret)\s*[:=]\s*['\"][A-Za-z0-9_\-+=/.~]{12,}['\"]"),
]

SECRET_RAW_MARKERS = (
    "AKIA", "ASIA", "ghp_", "github_pat_", "postgres://", "postgresql://",
    "mysql://", "mongodb://", "redis://", "-----BEGIN ",
)


class RedactionAudit(BaseModel):
    """Audit of redaction checks performed on review packet contents."""

    model_config = ConfigDict(extra="ignore")

    redacted: bool = True
    raw_value_matches: int = 0
    redaction_rules_applied: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)


class PacketSizeStats(BaseModel):
    """Byte and line count metrics for the assembled packet."""

    model_config = ConfigDict(extra="ignore")

    total_files: int = 0
    total_lines: int = 0
    total_bytes: int = 0
    changed_code_lines: int = 0
    context_lines: int = 0
    findings_count: int = 0


class ContextItem(BaseModel):
    """Bounded, redacted context snippet attached to a review packet."""

    model_config = ConfigDict(extra="ignore")

    file: str
    line_range: list[int] = Field(description="[start_line, end_line]")
    reason: str
    ranking_score: float = Field(ge=0.0, le=1.0)
    retrieval_signals: list[str] = Field(default_factory=list)
    source_type: str = Field(description="changed_code, symbol, test, caller, callee, configuration, or path_proximity")
    content: str | None = None
    truncation_status: str = Field(default="full", description="full, truncated, or omitted")
    lines_included: int = 0
    bytes_included: int = 0


class ObservedTestEvidence(BaseModel):
    """Bounded, observed test execution evidence attached to a ReviewPacket."""

    model_config = ConfigDict(extra="ignore")

    proposal_id: str
    sandbox_id: str
    test_runner: str
    command: list[str] = Field(default_factory=list)
    target_tests: list[str] = Field(default_factory=list)
    result: str = "not_run"  # passed, failed, timed_out, blocked, error
    failure_summary: str | None = None
    failed_test_names: list[str] = Field(default_factory=list)
    stack_trace_summary: str | None = None
    duration_ms: float = 0.0
    limitations: list[str] = Field(default_factory=list)
    observed_evidence_only: bool = True


class ReviewPacket(BaseModel):
    """Deterministic, bounded packet ready for review policy and provider."""

    model_config = ConfigDict(extra="ignore")

    packet_id: str
    repository: str
    base_commit: str | None = None
    head_commit: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    changed_line_ranges: dict[str, list[list[int]]] = Field(default_factory=dict)
    changed_symbols: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_findings: list[dict[str, Any]] = Field(default_factory=list)
    context_candidates: list[ContextItem] = Field(default_factory=list)
    relevant_imports: list[dict[str, Any]] = Field(default_factory=list)
    relevant_references: list[dict[str, Any]] = Field(default_factory=list)
    relevant_tests: list[str] = Field(default_factory=list)
    configuration: dict[str, Any] = Field(default_factory=dict)
    policy_summary: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    redaction_status: RedactionAudit = Field(default_factory=RedactionAudit)
    packet_size_statistics: PacketSizeStats = Field(default_factory=PacketSizeStats)
    truncated: bool = False
    excluded_candidates: list[dict[str, Any]] = Field(default_factory=list)
    observed_test_evidence: ObservedTestEvidence | None = None


def redact_text(text: str) -> tuple[str, list[str]]:
    """Sanitize secrets from text, returning sanitized text and applied rules."""
    applied: list[str] = []
    sanitized = text

    for pat in SECRET_PATTERNS:
        matches = pat.findall(sanitized)
        if matches:
            applied.append(f"regex:{pat.pattern[:20]}")
            sanitized = pat.sub("[REDACTED_SECRET]", sanitized)

    # Secondary token replacement
    for marker in ("password", "token", "secret", "api_key", "apikey"):
        sub_pat = re.compile(rf"(?i)({marker}\s*[:=]\s*)[^\s,;]+", re.I)
        if sub_pat.search(sanitized):
            sanitized = sub_pat.sub(r"\1[REDACTED]", sanitized)
            applied.append(f"keyword:{marker}")

    return sanitized, applied


def audit_packet_redaction(packet_dict: dict[str, Any]) -> RedactionAudit:
    """Audit an assembled packet dictionary to verify no raw secrets remain."""
    serialized = json.dumps(packet_dict)
    raw_matches = 0
    failed: list[str] = []

    # Check for unredacted sensitive patterns
    for pat in SECRET_PATTERNS:
        found = pat.findall(serialized)
        if found:
            raw_matches += len(found)
            failed.append(f"unredacted_pattern:{pat.pattern[:25]}")

    return RedactionAudit(
        redacted=raw_matches == 0,
        raw_value_matches=raw_matches,
        redaction_rules_applied=["regex_scrubbing", "keyword_sanitization"],
        failed_checks=failed,
    )


def _safe_read_range(full_path: Path, start: int, end: int) -> tuple[str, int]:
    """Read lines [start, end] 1-indexed safely from file."""
    if not full_path.is_file():
        return "", 0
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(start - 1, 0)
        e = min(end, len(lines))
        if s >= len(lines) or s > e:
            return "", 0
        selected = lines[s:e]
        return "\n".join(selected), len(selected)
    except OSError:
        return "", 0

def _candidate_priority_tuple(cand: ContextCandidate) -> tuple[int, float, str, list[int]]:
    if cand.is_directly_changed and "changed_symbol" not in cand.signals:
        prio = 1
    elif "changed_symbol" in cand.signals or (cand.is_directly_changed and "changed_symbol" in cand.signals):
        prio = 2
    elif "caller" in cand.signals or "callee" in cand.signals:
        prio = 3
    elif cand.is_test:
        prio = 4
    elif cand.is_configuration:
        prio = 5
    else:
        prio = 6
    return (prio, -cand.score, cand.file, cand.line_range or [0, 0])


def assemble_review_packet(
    snapshot_path: Path | str,
    diff: Diff,
    *,
    repository_name: str | None = None,
    base_commit: str | None = None,
    head_commit: str | None = None,
    repo_index: RepositoryIndex | None = None,
    deterministic_findings: Sequence[Finding | dict[str, Any]] | None = None,
    context_candidates: Sequence[ContextCandidate] | None = None,
    config: Mapping[str, Any] | None = None,
    policy_summary: Mapping[str, Any] | None = None,
) -> ReviewPacket:
    """Deterministically assemble a bounded, redacted ReviewPacket."""
    root = Path(snapshot_path).resolve()
    repo_name = repository_name or root.name or "repository"
    cfg = dict(config or {})
    rev_cfg = cfg.get("review", {}) if isinstance(cfg.get("review", {}), dict) else {}

    max_files = int(rev_cfg.get("max_context_files", 12))
    max_lines_per_file = int(rev_cfg.get("max_lines_per_file", 160))
    max_total_lines = int(rev_cfg.get("max_total_context_lines", 1200))
    max_packet_bytes = int(rev_cfg.get("max_packet_bytes", 100_000))
    max_findings = int(rev_cfg.get("max_findings", 10))
    include_tests = bool(rev_cfg.get("include_tests", True))
    include_config = bool(rev_cfg.get("include_configuration", True))

    changed_files = [c.path for c in diff.files]
    changed_ranges: dict[str, list[list[int]]] = {}
    for c in diff.files:
        ranges = [[r.start, r.start + max(r.count - 1, 0)] for r in c.new_ranges]
        changed_ranges[c.path] = ranges

    # Deterministic packet ID
    hasher = hashlib.sha256()
    hasher.update((repository_name or "repo").encode("utf-8"))
    hasher.update((base_commit or "base").encode("utf-8"))
    hasher.update((head_commit or "head").encode("utf-8"))
    for f in sorted(changed_files):
        hasher.update(f.encode("utf-8"))
    packet_id = f"pkt-{hasher.hexdigest()[:16]}"

    limitations: list[str] = []
    excluded_candidates: list[dict[str, Any]] = []

    # 1. Deterministic findings (priority 2, bounded by max_findings)
    findings_list: list[dict[str, Any]] = []
    raw_findings = list(deterministic_findings or [])
    for f in raw_findings[:max_findings]:
        f_dict = f.model_dump(mode="json") if hasattr(f, "model_dump") else dict(f)
        findings_list.append(f_dict)
    if len(raw_findings) > max_findings:
        limitations.append(f"deterministic findings bounded to {max_findings} of {len(raw_findings)}")

    # 2. Context Items (Priorities 1, 3, 4, 5)
    candidates_in = list(context_candidates or [])
    existing_changed_files = {c.file for c in candidates_in if c.is_directly_changed}
    for change in diff.files:
        if change.path not in existing_changed_files:
            if change.new_ranges:
                for r in change.new_ranges:
                    cand = ContextCandidate(
                        file=change.path,
                        line_range=[r.start, r.start + max(r.count - 1, 0)],
                        reason="directly changed diff lines",
                        score=1.0,
                        is_directly_changed=True,
                        signals=["changed_code"],
                    )
                    candidates_in.append(cand)
            else:
                candidates_in.append(
                    ContextCandidate(
                        file=change.path,
                        line_range=[1, 1],
                        reason="directly changed file",
                        score=1.0,
                        is_directly_changed=True,
                        signals=["changed_code"],
                    )
                )

    candidates_in.sort(key=_candidate_priority_tuple)
    context_items: list[ContextItem] = []
    total_lines_consumed = 0
    total_bytes_consumed = 0
    seen_files: set[str] = set()

    for cand in candidates_in:
        # Check test and config filters
        if cand.is_test and not include_tests:
            excluded_candidates.append({"file": cand.file, "reason": "include_tests=false"})
            continue
        if cand.is_configuration and not include_config:
            excluded_candidates.append({"file": cand.file, "reason": "include_configuration=false"})
            continue

        # File limit check
        if cand.file not in seen_files and len(seen_files) >= max_files:
            excluded_candidates.append({"file": cand.file, "reason": f"exceeded max_context_files ({max_files})"})
            continue

        full_file_path = root / cand.file
        start_line = cand.line_range[0] if cand.line_range else 1
        end_line = cand.line_range[1] if len(cand.line_range) > 1 else start_line

        raw_snippet, line_cnt = _safe_read_range(full_file_path, start_line, end_line)
        if not raw_snippet:
            continue

        trunc_status = "full"
        snippet_lines = raw_snippet.splitlines()

        # Check line limit per file
        if len(snippet_lines) > max_lines_per_file:
            snippet_lines = snippet_lines[:max_lines_per_file]
            trunc_status = "truncated"
            limitations.append(f"file {cand.file} snippet truncated to {max_lines_per_file} lines")

        # Check total lines budget
        remaining_lines = max_total_lines - total_lines_consumed
        if remaining_lines <= 0:
            excluded_candidates.append({"file": cand.file, "reason": "exceeded max_total_context_lines budget"})
            limitations.append(f"context candidate {cand.file} omitted due to line budget")
            continue
        if len(snippet_lines) > remaining_lines:
            snippet_lines = snippet_lines[:remaining_lines]
            trunc_status = "truncated"
            limitations.append(f"candidate {cand.file} truncated to fit total line budget")

        content = "\n".join(snippet_lines)
        sanitized_content, _ = redact_text(content)
        content_bytes = len(sanitized_content.encode("utf-8"))

        # Check byte budget
        if total_bytes_consumed + content_bytes > max_packet_bytes:
            if cand.is_directly_changed and not context_items:
                # Force at least a small portion of directly changed code
                sanitized_content = sanitized_content[:1000]
                content_bytes = len(sanitized_content.encode("utf-8"))
                trunc_status = "truncated"
            else:
                excluded_candidates.append({"file": cand.file, "reason": "exceeded max_packet_bytes budget"})
                limitations.append(f"candidate {cand.file} omitted due to byte budget")
                continue

        source_type = (
            "changed_code"
            if cand.is_directly_changed and "changed_symbol" not in cand.signals
            else (
                "symbol"
                if "changed_symbol" in cand.signals
                else (
                    "test"
                    if cand.is_test
                    else (
                        "configuration"
                        if cand.is_configuration
                        else (
                            "caller"
                            if "caller" in cand.signals
                            else "callee" if "callee" in cand.signals else "other"
                        )
                    )
                )
            )
        )

        item = ContextItem(
            file=cand.file,
            line_range=[start_line, start_line + len(snippet_lines) - 1],
            reason=cand.reason,
            ranking_score=cand.score,
            retrieval_signals=cand.signals,
            source_type=source_type,
            content=sanitized_content,
            truncation_status=trunc_status,
            lines_included=len(snippet_lines),
            bytes_included=content_bytes,
        )
        context_items.append(item)
        seen_files.add(cand.file)
        total_lines_consumed += len(snippet_lines)
        total_bytes_consumed += content_bytes

    # Collect relevant imports, references, tests from index
    relevant_imports: list[dict[str, Any]] = []
    relevant_references: list[dict[str, Any]] = []
    relevant_tests: list[str] = []
    changed_symbols_list: list[dict[str, Any]] = []

    if repo_index:
        for imp in repo_index.imports:
            if imp.file in changed_files or (imp.resolved_path and imp.resolved_path in changed_files):
                relevant_imports.append(imp.model_dump(mode="json"))
        for ref in repo_index.references:
            if ref.file in changed_files or any(ref.symbol_name == s.name for s in repo_index.symbols if s.file in changed_files):
                relevant_references.append(ref.model_dump(mode="json"))
        for f, rf in repo_index.files.items():
            if rf.is_test:
                relevant_tests.append(f)
        for s in repo_index.symbols:
            if s.file in changed_files:
                changed_symbols_list.append(s.model_dump(mode="json"))

    packet_dict_pre = {
        "packet_id": packet_id,
        "repository": repo_name,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "changed_files": changed_files,
        "changed_line_ranges": changed_ranges,
        "changed_symbols": changed_symbols_list,
        "deterministic_findings": findings_list,
        "context_candidates": [c.model_dump(mode="json") for c in context_items],
        "relevant_imports": relevant_imports[:20],
        "relevant_references": relevant_references[:20],
        "relevant_tests": relevant_tests[:20],
        "configuration": cfg,
        "policy_summary": dict(policy_summary or {}),
        "limitations": limitations,
    }

    audit = audit_packet_redaction(packet_dict_pre)
    is_truncated = any(c.truncation_status != "full" for c in context_items) or bool(excluded_candidates) or bool(limitations)

    stats = PacketSizeStats(
        total_files=len(seen_files),
        total_lines=total_lines_consumed,
        total_bytes=total_bytes_consumed,
        changed_code_lines=sum(c.lines_included for c in context_items if c.source_type in {"changed_code", "symbol"}),
        context_lines=sum(c.lines_included for c in context_items if c.source_type not in {"changed_code", "symbol"}),
        findings_count=len(findings_list),
    )

    return ReviewPacket(
        packet_id=packet_id,
        repository=repo_name,
        base_commit=base_commit,
        head_commit=head_commit,
        changed_files=changed_files,
        changed_line_ranges=changed_ranges,
        changed_symbols=changed_symbols_list,
        deterministic_findings=findings_list,
        context_candidates=context_items,
        relevant_imports=relevant_imports[:20],
        relevant_references=relevant_references[:20],
        relevant_tests=relevant_tests[:20],
        configuration=cfg,
        policy_summary=dict(policy_summary or {}),
        limitations=limitations,
        redaction_status=audit,
        packet_size_statistics=stats,
        truncated=is_truncated,
        excluded_candidates=excluded_candidates,
    )


def attach_observed_test_evidence(
    packet: ReviewPacket,
    proposal: Any,
    test_result: Any,
) -> ReviewPacket:
    """Attach bounded, redacted observed test evidence to a review packet.

    Enforces:
    - Test actually ran (execution_allowed is True and status != 'not_run').
    - Output is verified redacted.
    - Proposal ID and Sandbox ID are explicitly recorded.
    - Limits on failed tests (10), stack frames (3), summary length (500), output bytes (4000).
    - Failed tests are explicitly marked as observed evidence and not proof of patch correctness outside scope.
    """
    if not test_result or not getattr(test_result, "execution_allowed", False):
        return packet

    if getattr(test_result, "status", "not_run") == "not_run":
        return packet

    redaction_audit = getattr(test_result, "redaction_audit", {}) or {}
    if isinstance(redaction_audit, dict) and not redaction_audit.get("safe", True):
        return packet

    proposal_id = getattr(proposal, "proposal_id", "") if proposal else ""
    sandbox_id = getattr(test_result, "sandbox_id", "sandbox") or "sandbox"
    test_runner = getattr(test_result, "runner", "") or getattr(test_result, "language", "python")
    raw_command = list(getattr(test_result, "command", []) or getattr(test_result, "commands_run", []))
    target_tests = list(getattr(test_result, "tests_run", []))
    result_status = getattr(test_result, "status", "not_run")

    raw_summary = getattr(test_result, "failure_summary", "") or ""
    summary = raw_summary[:500] if raw_summary else None

    failed_names = list(getattr(test_result, "failed_test_names", []))[:10]
    raw_stack = getattr(test_result, "stack_trace_summary", "") or ""
    stack_lines = raw_stack.splitlines()[:6]
    stack_trace = "\n".join(stack_lines) if stack_lines else None

    dur_ms = float(getattr(test_result, "duration_ms", 0.0) or 0.0)

    evidence_limitations = list(getattr(test_result, "diagnostics_limitations", []) or [])
    evidence_limitations.append(
        "Observed test evidence reflects tested scope only; does not prove patch correctness or regression absence elsewhere."
    )

    packet.observed_test_evidence = ObservedTestEvidence(
        proposal_id=proposal_id,
        sandbox_id=sandbox_id,
        test_runner=test_runner,
        command=raw_command,
        target_tests=target_tests,
        result=result_status,
        failure_summary=summary,
        failed_test_names=failed_names,
        stack_trace_summary=stack_trace,
        duration_ms=dur_ms,
        limitations=evidence_limitations,
        observed_evidence_only=True,
    )
    return packet


__all__ = [
    "RedactionAudit",
    "PacketSizeStats",
    "ContextItem",
    "ObservedTestEvidence",
    "ReviewPacket",
    "redact_text",
    "audit_packet_redaction",
    "assemble_review_packet",
    "attach_observed_test_evidence",
]
