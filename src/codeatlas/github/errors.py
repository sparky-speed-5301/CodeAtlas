"""Typed errors for the GitHub integration.  All errors fail closed and never
carry tokens or secret material."""

from __future__ import annotations


class GitHubError(Exception):
    """Base exception for GitHub integration failures."""


class GitHubAuthError(GitHubError):
    """Raised when GitHub authentication is missing or rejected."""


class GitHubNetworkError(GitHubError):
    """Raised when a GitHub request fails at the network/transport level."""


class GitHubRateLimitError(GitHubError):
    """Raised when GitHub rate limits are exhausted."""


class GitHubResponseError(GitHubError):
    """Raised when a GitHub response is malformed or unexpected."""


class SHAMismatchError(GitHubError):
    """Raised when local or remote SHAs do not match the PR head/base, or the
    head SHA changes during the run."""


__all__ = [
    "GitHubError",
    "GitHubAuthError",
    "GitHubNetworkError",
    "GitHubRateLimitError",
    "GitHubResponseError",
    "SHAMismatchError",
]
