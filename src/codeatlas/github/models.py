"""Typed models for the read-only GitHub pull-request integration (Phase 9A)."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class PRMetadata(BaseModel):
    """Metadata for a GitHub pull request."""

    model_config = ConfigDict(extra="ignore")

    number: int
    title: str = ""
    state: str = ""
    author: str = ""
    base_ref: str = ""
    head_ref: str = ""
    base_sha: str
    head_sha: str
    changed_files_count: int = 0
    html_url: str = ""


class PRFile(BaseModel):
    """One file changed by a GitHub pull request."""

    model_config = ConfigDict(extra="ignore")

    filename: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0


class GitHubComment(BaseModel):
    """An existing or newly created PR issue comment."""

    model_config = ConfigDict(extra="ignore")

    comment_id: int | str = ""
    author: str = ""
    body: str
    created_at: str = ""


class CheckStatus(BaseModel):
    """Aggregated check/run status for a pull request (read-only view)."""

    model_config = ConfigDict(extra="ignore")

    state: str = "unknown"
    total: int = 0
    successful: int = 0
    failed: int = 0
    pending: int = 0


class GitHubReviewReport(BaseModel):
    """Safe, serializable report for one GitHub PR review run."""

    model_config = ConfigDict(extra="ignore")

    repository: str
    pr_number: int
    head_sha: str
    base_sha: str = ""
    mode: str = Field(description="dry_run or post")
    run_id: str = ""
    review_summary: str = ""
    review_policy: str = ""
    findings_total: int = 0
    findings_anchored: int = 0
    comments_planned: int = 0
    comments_posted: int = 0
    inline_comments_posted: int = 0
    issue_comments_posted: int = 0
    comments_skipped_duplicate: int = 0
    comments_suppressed_unanchored: int = 0
    comments_suppressed_redacted: int = 0
    comments_suppressed_outside_diff: int = 0
    comments_fallback_issue: int = 0
    fallback_reasons: list[str] = Field(default_factory=list)
    write_calls: int = 0
    errors: list[str] = Field(default_factory=list)
    posted_comment_ids: list[Any] = Field(default_factory=list)
    partial_write: bool = False
    # Phase 9C summary fields.
    summary_comment_requested: bool = False
    summary_comment_write_attempted: bool = False
    summary_comment_write_succeeded: bool = False
    summary_comment_posted: bool = False
    summary_comment_updated: bool = False
    summary_comment_skipped_duplicate: bool = False
    summary_comment_id: Any = None
    summary_comment_marker: str = ""
    summary_comment_truncated: bool = False
    summary_comment_bytes: int = 0
    summary_comment_redaction_safe: bool = True
    summary_comment_failure_reason: str = ""
    summary_comment_head_sha: str = ""
    # Phase 9D check-run fields.
    check_run_requested: bool = False
    check_run_write_attempted: bool = False
    check_run_created: bool = False
    check_run_updated: bool = False
    check_run_skipped_duplicate: bool = False
    check_run_id: Any = None
    check_run_head_sha: str = ""
    check_run_status: str = ""
    check_run_conclusion: str = ""
    check_run_redaction_safe: bool = True
    check_run_failure_reason: str = ""


__all__ = [
    "PRMetadata",
    "PRFile",
    "GitHubComment",
    "CheckStatus",
    "GitHubReviewReport",
]
