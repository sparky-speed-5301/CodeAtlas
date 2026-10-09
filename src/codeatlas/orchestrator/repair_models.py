"""Bounded, data-only repair contracts. No provider transcripts or tokens."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.analyzers.secrets import _matches, _matched_value, _PLACEHOLDER
from codeatlas.findings.models import EvidenceStrength, QualityDecision
from codeatlas.patching.models import PatchProposal, PatchValidationResult
from codeatlas.review.packet import SECRET_RAW_MARKERS, audit_packet_redaction
from codeatlas.verification.redaction import redact_test_output

REPAIR_POLICY_VERSION: Literal["11C-A.1"] = "11C-A.1"
RepairLanguage = Literal["python", "javascript", "typescript", "go", "java", "rust"]
MAX_REPAIR_CONTEXT_BYTES = 24_000
BoundedText = Annotated[str, Field(min_length=1, max_length=512)]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Commit = Annotated[str, Field(min_length=1, max_length=128)]


def repair_payload_is_safe(payload: object) -> bool:
    """Audit string leaves before JSON escaping, reusing existing secret scanners."""
    if isinstance(payload, dict):
        return all(repair_payload_is_safe(key) and repair_payload_is_safe(value) for key, value in payload.items())
    if isinstance(payload, (tuple, list)):
        return all(repair_payload_is_safe(value) for value in payload)
    if not isinstance(payload, str):
        return True
    _, audit = redact_test_output(payload, target_name="repair_payload")
    if audit.raw_value_matches or not audit.safe or not audit_packet_redaction({"text": payload}).redacted:
        return False
    if any(marker.lower() in payload.lower() for marker in (*SECRET_RAW_MARKERS, "sk-", "CAT-APP-")):
        return False
    return not any(
        _PLACEHOLDER.fullmatch(_matched_value(rule, match).strip()) is None
        for line in payload.splitlines() for rule, match, _ in _matches(line)
    )


class RepairLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_target_files: Literal[1] = 1
    max_patch_lines: int = Field(default=60, ge=1, le=150)
    max_patch_bytes: int = Field(default=32_000, ge=1, le=64_000)
    max_source_bytes: int = Field(default=64_000, ge=1, le=128_000)
    max_context_lines: int = Field(default=160, ge=1, le=160)
    max_context_bytes: int = Field(default=16_000, ge=1, le=16_000)
    max_context_files: int = Field(default=4, ge=1, le=8)
    max_lines_per_file: int = Field(default=160, ge=1, le=200)
    max_symbols: int = Field(default=20, ge=1, le=64)
    max_callers_callees: int = Field(default=8, ge=1, le=16)
    max_tests: int = Field(default=8, ge=1, le=32)
    max_diagnostic_bytes: int = Field(default=4_000, ge=256, le=16_000)
    prohibited_paths: tuple[BoundedText, ...] = Field(default=(
        ".git/**", "**/node_modules/**", "**/vendor/**", "**/dist/**", "**/build/**",
        "**/*.generated.*", "**/*.min.js", "**/*.d.ts", "**/*_pb2.py",
        "tests/**", ".github/**", "**/package.json", "**/*lock*", "**/pyproject.toml",
    ), max_length=32)


class RepairRepositoryState(BaseModel):
    """Trusted current run/revision supplied by the caller, independent of provider data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: Identifier
    repository: Annotated[str, Field(min_length=1, max_length=4096)]
    base_commit: Commit
    head_commit: Commit


class RepairSymbol(BaseModel):
    """Bounded projection of an existing repository Symbol."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Identifier
    kind: Identifier
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class RepairContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    finding_id: Annotated[str, Field(pattern=r"^CA-[A-Z0-9][A-Z0-9._-]*$", max_length=128)]
    run_id: Identifier
    repository_identity: Identifier
    base_commit: Commit
    head_commit: Commit
    language: RepairLanguage
    changed_file: BoundedText
    changed_line_range: tuple[int, int]
    containing_symbol: RepairSymbol | None
    code_line_range: tuple[int, int]
    code_context: Annotated[str, Field(min_length=1, max_length=16_000)]
    category: Identifier
    claim: BoundedText
    impact: BoundedText
    deterministic_evidence: tuple[BoundedText, ...] = Field(min_length=1, max_length=10)
    quality_decision: QualityDecision
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_strength: EvidenceStrength
    repository_context: tuple[BoundedText, ...] = Field(default=(), max_length=10)
    related_tests: tuple[BoundedText, ...] = Field(default=(), max_length=8)
    relevant_symbol_definitions: tuple[BoundedText, ...] = Field(default=(), max_length=20)
    bounded_callers: tuple[BoundedText, ...] = Field(default=(), max_length=8)
    bounded_callees: tuple[BoundedText, ...] = Field(default=(), max_length=8)
    diagnostic_evidence: tuple[BoundedText, ...] = Field(default=(), max_length=10)
    provenance_labels: tuple[BoundedText, ...] = Field(default=(), max_length=20)
    truncated_sections: tuple[BoundedText, ...] = Field(default=(), max_length=20)
    limits: RepairLimits = Field(default_factory=RepairLimits)
    target_file_limits: Literal[1] = 1
    maximum_patch_line_limits: int = Field(default=60, ge=1, le=150)
    prohibited_paths: tuple[BoundedText, ...] = Field(
        default_factory=lambda: RepairLimits().prohibited_paths, max_length=32,
    )
    policy_version: Literal["11C-A.1"] = REPAIR_POLICY_VERSION
    context_truncated: bool = False
    context_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] = ""

    @model_validator(mode="after")
    def check_bounds_and_redaction(self) -> RepairContext:
        for start, end in (self.changed_line_range, self.code_line_range):
            if start < 1 or end < start:
                raise ValueError("Invalid repair context line range")
        start, end = self.code_line_range
        if (len(self.code_context.splitlines()) != end - start + 1
                or end - start + 1 > self.limits.max_context_lines
                or len(self.code_context.encode("utf-8")) > self.limits.max_context_bytes):
            raise ValueError("Repair code exceeds line or byte bounds")
        if len(self.relevant_symbol_definitions) > self.limits.max_symbols:
            raise ValueError("Repair symbol context exceeds bounds")
        if len(self.bounded_callers) + len(self.bounded_callees) > self.limits.max_callers_callees:
            raise ValueError("Repair caller/callee context exceeds bounds")
        if len(self.related_tests) > self.limits.max_tests:
            raise ValueError("Repair test context exceeds bounds")
        if sum(len(item.encode("utf-8")) for item in self.diagnostic_evidence) > self.limits.max_diagnostic_bytes:
            raise ValueError("Repair diagnostics exceed byte bounds")
        if not (start <= self.changed_line_range[0] <= self.changed_line_range[1] <= end):
            raise ValueError("Repair code must contain the complete finding location")
        if (self.target_file_limits != self.limits.max_target_files
                or self.maximum_patch_line_limits != self.limits.max_patch_lines
                or self.prohibited_paths != self.limits.prohibited_paths):
            raise ValueError("Repair limit projections must match the bounded limits")
        if self.containing_symbol and not (
            1 <= self.containing_symbol.start_line <= self.changed_line_range[0]
            <= self.changed_line_range[1] <= self.containing_symbol.end_line
        ):
            raise ValueError("Containing symbol must contain the finding location")
        serialized = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        if len(serialized.encode("utf-8")) > MAX_REPAIR_CONTEXT_BYTES:
            raise ValueError("Repair context exceeds byte budget")
        if not repair_payload_is_safe(self.model_dump(mode="json")):
            raise ValueError("Repair context contains unsafe secret material")
        if self.context_hash and self.context_hash != compute_context_hash(self):
            raise ValueError("Repair context hash does not match bounded content")
        return self


def compute_context_hash(context: RepairContext) -> str:
    """Hash only the canonical bounded context, excluding the hash field itself."""
    payload = context.model_dump(mode="json", exclude={"context_hash"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["proposed", "rejected", "review_only"]
    reason: str = Field(default="", max_length=512)
    context: RepairContext | None = None
    proposal: PatchProposal | None = None
    validation: PatchValidationResult | None = None
    approval_required: Literal[True] = True
    limitations: list[BoundedText] = Field(default_factory=list, max_length=20)
    tool_availability: dict[str, bool] = Field(default_factory=dict, max_length=12)
    checks_planned: tuple[tuple[BoundedText, ...], ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def proposal_requires_approval(self) -> RepairResult:
        if self.status == "proposed":
            if (self.context is None or self.proposal is None or self.validation is None
                    or not self.proposal.approval_required or self.proposal.status != "requires_human_approval"
                    or not self.validation.valid or self.validation.execution_allowed
                    or self.validation.approval_verified is True
                    or self.validation.patch_applied_in_isolated_sandbox
                    or self.validation.commands_run):
                raise ValueError("Repair success requires an unapproved, statically validated draft")
        elif self.proposal is not None or self.validation is not None:
            raise ValueError("Rejected repairs cannot contain a proposal or validation")
        return self
