"""Reviewer provider interface and result models."""

from __future__ import annotations

from typing import Any, Protocol, TYPE_CHECKING
from pydantic import BaseModel, ConfigDict, Field

from codeatlas.review.packet import ReviewPacket

if TYPE_CHECKING:
    from codeatlas.orchestrator.repair_models import RepairContext


class ReviewerResult(BaseModel):
    """The structured result returned by a ReviewerProvider."""

    model_config = ConfigDict(extra="ignore")

    provider_name: str
    provider_version: str = "1.0.0"
    findings: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    limitations: list[str] = Field(default_factory=list)
    abstentions: list[str] = Field(default_factory=list)
    usage_metadata: dict[str, Any] = Field(default_factory=dict)
    validation_errors: list[str] = Field(default_factory=list)
    validation_status: str = Field(
        default="unvalidated",
        description="Provider-side output status: unvalidated, accepted, sanitized, rejected, abstained, failed, or not_requested",
    )
    patch_suggestions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Optional draft patch suggestions (Phase 7B). Structured data only; never executable instructions, approval tokens, or status claims.",
    )


class ReviewerProvider(Protocol):
    """Interface required for reviewer implementations."""

    name: str
    version: str

    def review(self, packet: ReviewPacket) -> ReviewerResult: ...


class RepairProvider(Protocol):
    """Optional offline repair operation over a bounded RepairContext."""

    name: str
    version: str
    network_access: bool

    def propose(self, context: RepairContext) -> object: ...


__all__ = ["ReviewerResult", "ReviewerProvider", "RepairProvider"]
