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
]
