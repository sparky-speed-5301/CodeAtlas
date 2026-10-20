"""Safe feedback exports projected from existing service-owned scoped records."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.findings.models import FeedbackOutcome, FeedbackReason


class BetaFeedbackItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target: Literal["finding", "fix_proposal"]
    finding_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    proposal_id: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    outcome: FeedbackOutcome
    reason: FeedbackReason | None = None


class BetaFeedbackExport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    report_version: Literal["beta-feedback-v1"] = "beta-feedback-v1"
    repository_identity_hash: str = Field(pattern=r"^repo-[0-9a-f]{64}$")
    run_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    provider_mode: Literal["offline", "live", "fallback", "unavailable", "blocked", "not_run"]
    items: tuple[BetaFeedbackItem, ...] = Field(default=(), max_length=200)
    truncated: bool = False
    redaction_status: Literal["safe_metadata_only"] = "safe_metadata_only"
    approval_granted: Literal[False] = False
