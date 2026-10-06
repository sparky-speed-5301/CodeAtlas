"""Structured finding contracts used by providers and renderers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Severity = Literal["info", "low", "medium", "high", "blocker"]
EvidenceStrength = Literal["none", "weak", "supported", "strong", "reproduced"]
QualityDecision = Literal[
    "report", "report_with_uncertainty", "review_only", "abstain",
    "suppress_duplicate", "suppress_low_evidence",
]


class Finding(BaseModel):
    """A finding shaped like ``schemas/finding.schema.json``."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^CA-[A-Z0-9][A-Z0-9._-]*$")
    file: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    severity: Severity
    category: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    evidence_strength: EvidenceStrength
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    tools_consulted: list[str] = Field(default_factory=list)
    tests_consulted: list[str] = Field(default_factory=list)
    fixability: Literal["unknown", "not_fixable", "review_required", "suggested", "validated"]
    status: Literal["detected", "review_only", "suggested", "validated", "rejected", "abstained"]
    limitations: list[str] = Field(default_factory=list)
    abstention_reason: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    # Phase 11B backward-compatible quality metadata.  Defaults keep older
    # analyzer/provider payloads valid while the backend deterministically
    # recomputes these fields before they become user-facing.
    quality_version: str = "11B.1"
    evidence_sources: list[str] = Field(default_factory=list)
    deterministic_support: float = Field(default=0.0, ge=0, le=1)
    reviewer_support: float = Field(default=0.0, ge=0, le=1)
    changed_line_support: float = Field(default=0.0, ge=0, le=1)
    repository_context_support: float = Field(default=0.0, ge=0, le=1)
    test_support: float = Field(default=0.0, ge=0, le=1)
    ambiguity_score: float = Field(default=0.0, ge=0, le=1)
    truncation_penalty: float = Field(default=0.0, ge=0, le=1)
    unsupported_flow: bool = False
    duplicate_group_id: str | None = None
    suppressed_finding_ids: list[str] = Field(default_factory=list)
    quality_decision: QualityDecision = "review_only"
    quality_score: float = Field(default=0.0, ge=0, le=1)
    score_components: dict[str, float] = Field(default_factory=dict)
    quality_limitations: list[str] = Field(default_factory=list)
    feedback: Literal[
        "useful",
        "not_useful",
        "false_positive",
        "accepted",
        "dismissed",
        "needs_more_context",
    ] | None = None

    @field_validator("end_line")
    @classmethod
    def end_line_is_after_start(cls, value: int, info: Any) -> int:
        start = info.data.get("start_line")
        if start is not None and value < start:
            raise ValueError("end_line must be greater than or equal to start_line")
        return value


__all__ = ["EvidenceStrength", "Finding", "QualityDecision", "Severity"]
