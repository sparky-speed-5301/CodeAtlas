"""Structured finding contracts used by providers and renderers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Finding(BaseModel):
    """A finding shaped like ``schemas/finding.schema.json``."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^CA-[A-Z0-9][A-Z0-9._-]*$")
    file: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    severity: Literal["info", "low", "medium", "high", "blocker"]
    category: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    evidence_strength: Literal["none", "weak", "supported", "strong", "reproduced"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    tools_consulted: list[str] = Field(default_factory=list)
    tests_consulted: list[str] = Field(default_factory=list)
    fixability: Literal["unknown", "not_fixable", "review_required", "suggested", "validated"]
    status: Literal["detected", "review_only", "suggested", "validated", "rejected", "abstained"]
    limitations: list[str] = Field(default_factory=list)
    abstention_reason: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("end_line")
    @classmethod
    def end_line_is_after_start(cls, value: int, info: Any) -> int:
        start = info.data.get("start_line")
        if start is not None and value < start:
            raise ValueError("end_line must be greater than or equal to start_line")
        return value


__all__ = ["Finding"]
