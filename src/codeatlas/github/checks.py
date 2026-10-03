"""Deterministic check-run conclusion mapping and payload rendering (Phase 9D).

The mapping is fixed policy, never influenced by provider output.  Success is
never claimed merely because the provider responded, nor when tests were not
run, and patch correctness is never claimed.
"""

from __future__ import annotations

from typing import Any

CHECK_NAME = "CodeAtlas review"

# Allowed conclusions (never a review verdict on the PR itself).
ALLOWED_CONCLUSIONS = {"success", "neutral", "failure", "action_required", "skipped", "timed_out"}


def map_check_conclusion(
    *,
    policy_decision: str,
    findings: list[dict[str, Any]],
    tests_status: str | None = None,
    full_suite_status: str | None = None,
    run_errors: list[str] | None = None,
) -> str:
    """Deterministic policy mapping from the review outcome to a conclusion."""
    if run_errors:
        return "failure"
    if policy_decision == "blocked":
        return "failure"
    if any(str(f.get("severity")) == "blocker" for f in findings):
        return "action_required"
    if tests_status == "failed" or full_suite_status == "failed":
        return "failure"
    if findings:
        return "neutral"
    if policy_decision == "abstain":
        return "neutral"
    # Clean review: success only when tests actually ran and passed; if tests
    # were not run we cannot claim success.
    if tests_status == "passed" and (full_suite_status in {None, "passed", "skipped"}):
        return "success"
    return "neutral"


def check_external_id(pr_number: int, head_sha: str) -> str:
    """Stable external identity for idempotent check runs."""
    return f"codeatlas-check:pr={pr_number}:head={head_sha}"


def render_check_summary(
    *,
    repo_slug: str,
    pr_number: int,
    head_sha: str,
    run_id: str,
    status_label: str,
    policy_decision: str,
    conclusion: str,
    findings: list[dict[str, Any]],
    tests_status: str | None = None,
    full_suite_status: str | None = None,
    network_isolation_verified: bool | None = None,
    evidence_limitations: list[str] | None = None,
    summary_comment_url: str | None = None,
    max_bytes: int = 50_000,
) -> str:
    """Bounded, redacted check-run summary.  Deterministic; no timestamps."""
    counts: dict[str, int] = {}
    for f in findings:
        sev = str(f.get("severity", "info"))
        counts[sev] = counts.get(sev, 0) + 1
    lines = [
        f"### {CHECK_NAME}",
        "",
        f"**PR:** #{pr_number} · **Head:** `{head_sha[:12]}` · **Run:** `{run_id}`",
        f"**Overall status:** {status_label}",
        f"**Policy decision:** {policy_decision}",
        f"**Finding counts:** " + (
            ", ".join(f"{sev} {count}" for sev, count in sorted(counts.items()))
            if counts else "none"
        ),
        f"**Targeted tests:** {tests_status or 'not run'} · **Full suite:** {full_suite_status or 'not run'}",
        f"**Network isolation independently verified:** {'yes' if network_isolation_verified else 'no'}",
        "",
        "_Advisory review only: this check never approves, rejects, or merges. "
        "Patch correctness is not proven by this run._",
    ]
    if evidence_limitations:
        from .summary import _scrub_private_paths

        lines.extend(["", "**Limitations:**"])
        lines.extend(f"- {_scrub_private_paths(str(l))[:300]}" for l in evidence_limitations[:10])
    if summary_comment_url:
        lines.extend(["", f"Details: {summary_comment_url}"])
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > max_bytes:
        body = body[:max_bytes]
    return body


__all__ = [
    "ALLOWED_CONCLUSIONS",
    "CHECK_NAME",
    "check_external_id",
    "map_check_conclusion",
    "render_check_summary",
]
