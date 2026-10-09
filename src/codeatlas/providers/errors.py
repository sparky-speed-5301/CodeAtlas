"""Typed exceptions for reviewer provider configuration, transport, and execution."""

from __future__ import annotations


class ProviderError(Exception):
    """Base exception for all provider operations."""


class ProviderConfigError(ProviderError):
    """Raised when provider configuration or credentials are invalid or missing."""


class ProviderAuthError(ProviderError):
    """Raised when provider authentication fails (e.g. 401 Unauthorized, 403 Forbidden)."""


class ProviderRateLimitError(ProviderError):
    """Raised when provider rate limits or quotas are exceeded (e.g. 429 Too Many Requests)."""


class ProviderTimeoutError(ProviderError):
    """Raised when a provider network request times out."""


class ProviderTransportError(ProviderError):
    """Raised when an unrecoverable network or HTTP error occurs."""


class ProviderResponseSizeError(ProviderError):
    """Raised when a provider response exceeds the configured maximum byte size."""


class ProviderBudgetExceededError(ProviderError):
    """Raised when the session or run request budget is exhausted."""


class ProviderOutputParseError(ProviderError):
    """Raised when provider output cannot be parsed into valid JSON or findings."""


REPAIR_FAILURE_CODES = frozenset({
    "provider_unavailable",
    "timeout",
    "rate_limit",
    "malformed_response",
    "schema_failure",
    "output_too_large",
    "secret_detected",
    "unsupported_model",
    "budget_exceeded",
    "network_disabled",
    "retry_exhausted",
})


class RepairProviderFailure(ProviderError):
    """A stable, user-safe failure from the structured repair provider.

    The exception deliberately stores only a reason code.  Provider exception
    text can contain prompts, response fragments, URLs, or credentials and is
    therefore never part of the repair result or evidence stream.
    """

    CODES = REPAIR_FAILURE_CODES

    def __init__(self, code: str) -> None:
        self.code = code if code in self.CODES else "provider_unavailable"
        self.reason_code = self.code
        super().__init__(self.code)


__all__ = [
    "ProviderError",
    "ProviderConfigError",
    "ProviderAuthError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderTransportError",
    "ProviderResponseSizeError",
    "ProviderBudgetExceededError",
    "ProviderOutputParseError",
    "RepairProviderFailure",
    "REPAIR_FAILURE_CODES",
]
