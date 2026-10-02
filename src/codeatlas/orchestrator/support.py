"""Small orchestration value objects and errors."""

from __future__ import annotations

from dataclasses import dataclass, field

from codeatlas.findings import Finding


class OrchestrationError(RuntimeError):
    """Base exception for review orchestration failures."""


class ProviderError(OrchestrationError):
    """Raised when a provider cannot complete a review request."""


@dataclass(frozen=True)
class ReviewRequest:
    repository: str
    files: tuple[str, ...] = ()
    base_ref: str | None = None
    head_ref: str | None = None
    context: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewResult:
    findings: tuple[Finding, ...] = ()
    provider: str = "unknown"
    abstained: bool = False


__all__ = ["OrchestrationError", "ProviderError", "ReviewRequest", "ReviewResult"]
