"""Read-only GitHub pull-request integration (Phase 9A)."""

from .adapter import (
    GITHUB_CONFIG_DEFAULTS,
    MAX_COMMENTS_PER_RUN,
    finding_marker,
    parse_marker,
    render_finding_comment,
    run_github_pr_review,
)
from .checks import CHECK_NAME, check_external_id, map_check_conclusion, render_check_summary
from .summary import parse_summary_marker, render_summary_comment, summary_marker
from .errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNetworkError,
    GitHubRateLimitError,
    GitHubResponseError,
    SHAMismatchError,
)
from .models import CheckStatus, GitHubComment, GitHubReviewReport, PRFile, PRMetadata
from .transport import (
    FakeGitHubTransport,
    GitHubTransport,
    GhCliTransport,
    parse_comments,
    parse_pr_files,
    parse_pr_metadata,
)

__all__ = [
    "CHECK_NAME",
    "GITHUB_CONFIG_DEFAULTS",
    "MAX_COMMENTS_PER_RUN",
    "check_external_id",
    "map_check_conclusion",
    "parse_summary_marker",
    "render_check_summary",
    "render_summary_comment",
    "summary_marker",
    "CheckStatus",
    "FakeGitHubTransport",
    "GitHubAuthError",
    "GitHubComment",
    "GitHubError",
    "GitHubNetworkError",
    "GitHubRateLimitError",
    "GitHubResponseError",
    "GitHubReviewReport",
    "GitHubTransport",
    "GhCliTransport",
    "PRFile",
    "PRMetadata",
    "SHAMismatchError",
    "finding_marker",
    "parse_comments",
    "parse_marker",
    "parse_pr_files",
    "parse_pr_metadata",
    "render_finding_comment",
    "run_github_pr_review",
]
