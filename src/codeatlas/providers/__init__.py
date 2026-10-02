"""Provider abstractions, transport, prompt construction, and live review backends."""

from __future__ import annotations

from .config import ALLOWED_CONFIG_KEYS, ProviderConfig, load_provider_config
from .errors import (
    ProviderAuthError,
    ProviderBudgetExceededError,
    ProviderConfigError,
    ProviderError,
    ProviderOutputParseError,
    ProviderRateLimitError,
    ProviderResponseSizeError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from .live import LiveReviewer
from .prompt import (
    OUTPUT_SCHEMA_DESCRIPTION,
    SYSTEM_INSTRUCTION,
    build_repository_context,
    build_review_prompt,
)
from .transport import (
    FakeTransport,
    HttpTransport,
    ProviderTransport,
    TransportResponse,
)

# Backwards compatibility for legacy phase 2 stub
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from codeatlas.findings import Finding

if TYPE_CHECKING:  # runtime import would cycle through codeatlas.orchestrator
    from codeatlas.orchestrator import ReviewRequest


class Provider(Protocol):
    """Legacy interface implemented by review providers."""

    name: str

    def review(self, request: ReviewRequest) -> Sequence[Finding]: ...


class FakeProvider:
    """Legacy deterministic provider used until a real provider is integrated."""

    name = "fake"

    def review(self, request: ReviewRequest) -> tuple[Finding, ...]:
        del request
        return ()


__all__ = [
    "ProviderConfig",
    "ALLOWED_CONFIG_KEYS",
    "load_provider_config",
    "ProviderError",
    "ProviderConfigError",
    "ProviderAuthError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderTransportError",
    "ProviderResponseSizeError",
    "ProviderBudgetExceededError",
    "ProviderOutputParseError",
    "ProviderTransport",
    "TransportResponse",
    "HttpTransport",
    "FakeTransport",
    "SYSTEM_INSTRUCTION",
    "OUTPUT_SCHEMA_DESCRIPTION",
    "build_repository_context",
    "build_review_prompt",
    "LiveReviewer",
    "Provider",
    "FakeProvider",
]
