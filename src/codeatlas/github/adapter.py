"""Read-only GitHub pull-request review adapter (Phase 9A).

Fetches PR metadata, files, and diff through a typed transport, validates the
local repository against the PR base/head SHAs, reuses the existing CodeAtlas
review pipeline, and publishes redacted, diff-anchored comments with
idempotency markers.  Writes happen only in explicit ``--post`` mode; the
default is draft/dry-run with zero write calls.

This adapter never: applies patches, creates branches, merges, approves,
modifies files, changes labels/settings, submits review verdicts, or exposes
secrets or raw provider output.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from codeatlas.evidence import EvidenceLogger
from codeatlas.git.executable import run_git
from codeatlas.git.refs import resolve_ref
from codeatlas.git.repository import validate_repository
from codeatlas.orchestrator.review import run_review, load_config
from codeatlas.review.packet import SECRET_PATTERNS

from .checks import CHECK_NAME, check_external_id, map_check_conclusion, render_check_summary
from .errors import GitHubError, SHAMismatchError
from .summary import parse_summary_marker, render_summary_comment, status_label, summary_marker
from .transport import GhCliTransport, GitHubTransport
from .models import GitHubReviewReport

_config = load_config
_status_label = status_label

# Non-secret defaults; the repository's `.codeatlas.yml` `github:` section may
# only tighten these.  Tokens are never part of this configuration.
GITHUB_CONFIG_DEFAULTS: dict[str, Any] = {
    "allow_post": True,
    "allow_summary_comment": True,
    "allow_check_run": True,
    "max_comments": 20,
    "max_summary_bytes": 50_000,
    "require_sha_recheck": True,
}

MAX_COMMENTS_PER_RUN = 20

_MARKER_RE = re.compile(
    r"codeatlas:pr=(?P<pr>\d+)\s+head=(?P<head>[0-9a-f]+)\s+finding=(?P<finding>\S+)\s+run=(?P<run>\S+)"
    r"(?:\s+mode=(?P<mode>\S+))?"
)


def finding_marker(pr_number: int, head_sha: str, finding_id: str, run_id: str, mode: str = "issue") -> str:
    """Idempotency marker binding PR, head SHA, finding ID, run ID, and comment mode."""
    return f"<!-- codeatlas:pr={pr_number} head={head_sha} finding={finding_id} run={run_id} mode={mode} -->"


def parse_marker(body: str) -> dict[str, str] | None:
    match = _MARKER_RE.search(body)
    if match is None:
        return None
    data = match.groupdict()
    data.setdefault("mode", "issue")
    return data


def _emit(evidence: EvidenceLogger | None, event: str, **details: Any) -> None:
    if evidence is not None:
        evidence.emit(event, **details)


def render_finding_comment(
    pr_number: int,
    head_sha: str,
    run_id: str,
    finding: dict[str, Any],
    mode: str = "issue",
) -> str:
    """Render a bounded, redacted markdown comment for one anchored finding."""
    file = str(finding.get("file", "?"))
    start = finding.get("start_line", "?")
    end = finding.get("end_line", start)
    severity = str(finding.get("severity", "?"))
    category = str(finding.get("category", "?"))
    claim = str(finding.get("claim", ""))
    impact = str(finding.get("impact", ""))
    confidence = float(finding.get("confidence", 0.0))
    strength = str(finding.get("evidence_strength", "none"))
    quality_decision = str(finding.get("quality_decision", "review_only"))
    quality_score = float(finding.get("quality_score", 0.0) or 0.0)
    evidence_items = [str(e) for e in (finding.get("evidence") or [])][:3]
    limitations = [str(x) for x in (finding.get("limitations") or [])][:3]

    lines = [
        f"### CodeAtlas finding ({severity} · {category})",
        "",
        f"**File:** `{file}:{start}-{end}`",
        f"**Claim:** {claim}",
        f"**Impact:** {impact}",
        f"**Confidence:** {confidence:.2f} · **Evidence strength:** {strength}",
        f"**Quality:** {quality_decision} (score {quality_score:.2f})",
    ]
    if evidence_items:
        lines.append("")
        lines.append("**Evidence:**")
        lines.extend(f"- {item}" for item in evidence_items)
    if limitations:
        lines.append("")
        lines.append("*Limitations:* " + "; ".join(limitations))
    quality_limitations = [str(item)[:300] for item in (finding.get("quality_limitations") or [])[:5]]
    if quality_limitations:
        lines.append("*Quality limitations:* " + "; ".join(quality_limitations))
    if finding.get("abstention_reason"):
        lines.append("*Abstention:* " + str(finding.get("abstention_reason"))[:1000])
    lines.append("")
    lines.append(
        "*Automated read-only review. Findings are advisory only and require human review; "
        "CodeAtlas never approves, merges, or applies anything.*"
    )
    lines.append(finding_marker(pr_number, head_sha, str(finding.get("id", "?")), run_id, mode=mode))
    return "\n".join(lines)


def build_diff_line_map(diff_text: str) -> dict[str, dict[str, set[int]]]:
    """Map each diff file to its added, context, and deleted new/old line numbers.

    Derived deterministically from the local unified diff between the verified
    base and head SHAs.  Added lines are the only inline-comment-eligible
    positions; context lines are unchanged; deleted lines exist only on the
    old side and can never anchor an inline comment.
    """
    from codeatlas.patching.parser import parse_unified_diff

    maps: dict[str, dict[str, set[int]]] = {}
    parsed_files, _errors = parse_unified_diff(diff_text)
    for pf in parsed_files:
        file_map = maps.setdefault(pf.path, {"added": set(), "context": set(), "deleted": set()})
        for hunk in pf.hunks:
            new_line = hunk.new_start
            old_line = hunk.old_start
            for line in hunk.lines:
                prefix = line[:1]
                if prefix == " ":
                    file_map["context"].add(new_line)
                    new_line += 1
                    old_line += 1
                elif prefix == "+":
                    file_map["added"].add(new_line)
                    new_line += 1
                elif prefix == "-":
                    file_map["deleted"].add(old_line)
                    old_line += 1
    return maps


def classify_inline_target(
    finding: dict[str, Any],
    line_map: dict[str, dict[str, set[int]]],
) -> tuple[str, dict[str, Any]]:
    """Decide the inline-comment disposition of one finding.  Never guesses.

    Returns ``(decision, details)`` where decision is one of:
      - ``inline``   details carry path/line/start_line for the GitHub API;
      - ``fallback`` details carry a visible reason (post as issue comment);
      - ``suppress`` details carry a visible reason (do not post at all).
    """
    file = str(finding.get("file", "")).replace("\\", "/")
    try:
        start = int(finding.get("start_line", 0))
        end = int(finding.get("end_line", start))
    except (TypeError, ValueError):
        return "suppress", {"reason": "invalid_line_coordinates"}
    if end < start:
        return "suppress", {"reason": "invalid_line_coordinates"}
    file_map = line_map.get(file)
    if file_map is None:
        return "suppress", {"reason": "file_not_in_diff"}
    lines = range(start, end + 1)
    if all(line in file_map["added"] for line in lines):
        details: dict[str, Any] = {"path": file, "line": end}
        if end > start:
            details["start_line"] = start
        return "inline", details
    if all(line in file_map["context"] for line in lines):
        return "fallback", {"reason": "unchanged_context_line"}
    if all(line in file_map["deleted"] for line in lines):
        return "suppress", {"reason": "deleted_line"}
    if any(line in file_map["added"] for line in lines):
        return "fallback", {"reason": "ambiguous_line_mapping"}
    return "suppress", {"reason": "line_outside_diff"}


def _is_anchored(finding: dict[str, Any], changed_ranges: dict[str, list[list[int]]]) -> bool:
    file = str(finding.get("file", "")).replace("\\", "/")
    ranges = changed_ranges.get(file, [])
    try:
        start = int(finding.get("start_line", 0))
        end = int(finding.get("end_line", 0))
    except (TypeError, ValueError):
        return False
    return any(not (end < r[0] or start > r[1]) for r in ranges)


def _contains_raw_secret(text: str) -> bool:
    return any(pat.search(text) for pat in SECRET_PATTERNS)


def run_github_pr_review(
    repo_slug: str,
    pr_number: int,
    *,
    local_repo: str | Path,
    transport: GitHubTransport | None = None,
    post: bool = False,
    inline: bool = False,
    summary_comment: bool = False,
    check_run: bool = False,
    review_provider: str | None = None,
    evidence_output: str | Path | None = None,
    json_output: str | Path | None = None,
    max_comments: int = MAX_COMMENTS_PER_RUN,
) -> GitHubReviewReport:
    """Review one GitHub pull request read-only and optionally post comments.

    Fail-closed contract: any validation failure (SHA mismatch, malformed
    response, local review failure, redaction) raises before any write; the
    default (``post=False``) performs zero write calls.
    """
    transport = transport or GhCliTransport()
    run_id = f"ghrun-{uuid.uuid4().hex}"
    errors: list[str] = []
    posted_ids: list[Any] = []
    skipped_duplicate = 0
    inline_posted = 0
    issue_posted = 0

    # Non-secret configuration gates from the local repository's .codeatlas.yml.
    github_cfg = dict(GITHUB_CONFIG_DEFAULTS)
    try:
        section = _config(validate_repository(local_repo).root / ".codeatlas.yml").get("github", {})
        if isinstance(section, dict):
            for key in GITHUB_CONFIG_DEFAULTS:
                if key in section:
                    github_cfg[key] = section[key]
    except Exception:
        pass
    if post and not github_cfg["allow_post"]:
        raise GitHubError("GitHub posting is disabled by configuration (github.allow_post: false)")
    if summary_comment and not github_cfg["allow_summary_comment"]:
        raise GitHubError("Summary comments are disabled by configuration (github.allow_summary_comment: false)")
    if check_run and not github_cfg["allow_check_run"]:
        raise GitHubError("Check runs are disabled by configuration (github.allow_check_run: false)")
    max_comments = min(max_comments, int(github_cfg["max_comments"]))
    suppressed_unanchored = 0
    suppressed_redacted = 0

    evidence_ctx = EvidenceLogger(evidence_output) if evidence_output else None
    evidence = evidence_ctx.__enter__() if evidence_ctx is not None else None
    try:
        # 1. Fetch PR metadata and diff (read-only).
        metadata = transport.get_pr_metadata(repo_slug, pr_number)
        _emit(evidence, "github_pr_fetched", repository=repo_slug, pr=pr_number, sha=metadata.head_sha, status="ok")
        files = transport.get_pr_files(repo_slug, pr_number)
        pr_diff = transport.get_pr_diff(repo_slug, pr_number)
        del pr_diff  # The local git diff between the same SHAs is authoritative.

        # 2. Validate the local repository against the PR SHAs.
        repository = validate_repository(local_repo)
        try:
            resolved_head = resolve_ref(repository.root, metadata.head_sha)
            resolved_base = resolve_ref(repository.root, metadata.base_sha)
        except Exception as err:
            raise SHAMismatchError(
                f"Local repository does not contain the PR commits (head {metadata.head_sha[:12]}): {err}"
            ) from err
        if resolved_head.commit != metadata.head_sha or resolved_base.commit != metadata.base_sha:
            raise SHAMismatchError(
                "Local repository SHAs do not match the PR base/head; failing closed"
            )
        _emit(
            evidence,
            "github_local_repo_validated",
            repository=repo_slug,
            pr=pr_number,
            sha=metadata.head_sha,
            status="ok",
        )

        # 3. Reuse the existing review pipeline (diff -> analyzers -> index ->
        #    packet -> reviewer -> policy -> sanitized report).
        review_result = run_review(
            repository.root,
            base=metadata.base_sha,
            head=metadata.head_sha,
            index_repository=True,
            assemble_review_packet=True,
            review_provider=review_provider,
            evidence_output=evidence_output,
        )
        manifest = review_result.manifest
        errors.extend(manifest.errors)

        # 4. Cross-check the locally computed changed files against GitHub's.
        local_files = {c["path"] for c in manifest.changes}
        github_files = {f.filename for f in files}
        if local_files != github_files:
            raise SHAMismatchError(
                "Local diff files do not match the PR files from GitHub; failing closed "
                f"(local-only: {sorted(local_files - github_files)[:3]}, "
                f"github-only: {sorted(github_files - local_files)[:3]})"
            )

        # 5. Fail closed if the PR head SHA changed during the run (before any write).
        refetched = transport.get_pr_metadata(repo_slug, pr_number)
        if refetched.head_sha != metadata.head_sha or refetched.base_sha != metadata.base_sha:
            _emit(
                evidence,
                "github_sha_mismatch_failed",
                repository=repo_slug,
                pr=pr_number,
                sha=refetched.head_sha,
                status="sha_changed",
            )
            raise SHAMismatchError(
                "PR base/head SHA changed during the review run; failing closed before any comment was posted"
            )
        _emit(
            evidence,
            "github_review_completed",
            repository=repo_slug,
            pr=pr_number,
            sha=metadata.head_sha,
            status="ok",
        )

        # 6. Anchor findings to valid changed files/lines.
        changed_ranges: dict[str, list[list[int]]] = {}
        for change in manifest.changes:
            changed_ranges[change["path"]] = [
                [r["start"], r["start"] + max(r["count"] - 1, 0)] for r in change.get("new_ranges", [])
            ]
        findings = list(manifest.findings)
        anchored = [f for f in findings if _is_anchored(f, changed_ranges)]
        suppressed_unanchored = len(findings) - len(anchored)

        # 6b. In inline mode, classify each anchored finding against the local
        # diff's added/context/deleted lines.  Only added lines are inline
        # eligible; everything else falls back or suppresses with a reason.
        fallback_reasons: list[str] = []
        suppressed_outside_diff = 0
        inline_targets: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        issue_targets: list[tuple[dict[str, Any], str]] = []
        if inline:
            local_diff = run_git(
                ["diff", metadata.base_sha, metadata.head_sha], cwd=repository.root, check=False
            ).stdout
            line_map = build_diff_line_map(local_diff)
            for finding in anchored[:max_comments]:
                decision, details = classify_inline_target(finding, line_map)
                fid = str(finding.get("id", "?"))
                if decision == "inline":
                    body = render_finding_comment(
                        pr_number, metadata.head_sha, run_id, finding, mode="inline"
                    )
                    if _contains_raw_secret(body):
                        suppressed_redacted += 1
                        _emit(evidence, "github_comment_suppressed_redacted",
                              repository=repo_slug, pr=pr_number, finding_id=fid,
                              reason="raw secret pattern in comment body", status="redacted")
                        continue
                    inline_targets.append((finding, body, details))
                elif decision == "fallback":
                    reason = str(details["reason"])
                    fallback_reasons.append(f"{fid}: {reason}")
                    body = render_finding_comment(
                        pr_number, metadata.head_sha, run_id, finding, mode="issue"
                    )
                    if _contains_raw_secret(body):
                        suppressed_redacted += 1
                        _emit(evidence, "github_comment_suppressed_redacted",
                              repository=repo_slug, pr=pr_number, finding_id=fid,
                              reason=reason + "; raw secret pattern in comment body", status="redacted")
                        continue
                    body = body.replace(
                        "*Automated read-only review.",
                        f"*Inline posting skipped: {reason}. Automated read-only review.",
                    )
                    issue_targets.append((finding, body))
                    _emit(evidence, "github_inline_fallback",
                          repository=repo_slug, pr=pr_number, finding_id=fid,
                          reason=reason, status="fallback")
                else:
                    reason = str(details["reason"])
                    if reason in {"unchanged_context_line", "ambiguous_line_mapping"}:
                        fallback_reasons.append(f"{fid}: {reason}")
                    else:
                        suppressed_outside_diff += 1
                    _emit(evidence, "github_inline_suppressed",
                          repository=repo_slug, pr=pr_number, finding_id=fid,
                          reason=reason, status="suppressed")
        else:
            for finding in anchored[:max_comments]:
                body = render_finding_comment(
                    pr_number, metadata.head_sha, run_id, finding, mode="issue"
                )
                if _contains_raw_secret(body):
                    suppressed_redacted += 1
                    _emit(
                        evidence,
                        "github_comment_suppressed_redacted",
                        repository=repo_slug,
                        pr=pr_number,
                        finding_id=str(finding.get("id", "?")),
                        reason="raw secret pattern in comment body",
                        status="redacted",
                    )
                    continue
                issue_targets.append((finding, body))

        # 7. Idempotency: skip findings already commented at this head SHA in
        # either comment mode (issue or inline).
        existing = list(transport.list_comments(repo_slug, pr_number))
        existing += list(transport.list_inline_comments(repo_slug, pr_number))
        existing_keys = set()
        for comment in existing:
            marker = parse_marker(comment.body)
            if marker is not None and int(marker["pr"]) == pr_number:
                existing_keys.add((marker["finding"], marker["head"]))
        fresh_inline: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        fresh_issue: list[tuple[dict[str, Any], str]] = []

        def _dedupe(finding: dict[str, Any], body: str) -> bool:
            nonlocal skipped_duplicate
            key = (str(finding.get("id", "?")), metadata.head_sha)
            if key in existing_keys:
                skipped_duplicate += 1
                _emit(
                    evidence,
                    "github_comment_skipped_duplicate",
                    repository=repo_slug,
                    pr=pr_number,
                    finding_id=str(finding.get("id", "?")),
                    status="duplicate",
                )
                return False
            return True

        for finding, body, details in inline_targets:
            if _dedupe(finding, body):
                fresh_inline.append((finding, body, details))
        for finding, body in issue_targets:
            if _dedupe(finding, body):
                fresh_issue.append((finding, body))

        planned_total = len(fresh_inline) + len(fresh_issue)
        _emit(
            evidence,
            "github_comments_planned",
            repository=repo_slug,
            pr=pr_number,
            count=planned_total,
            status="planned",
        )

        # 8. Build all report bodies, redact, and validate sizes BEFORE writes.
        policy_decision_str = (
            manifest.policy_decisions[-1].get("decision", "unknown") if manifest.policy_decisions else "unknown"
        )
        review_mode_str = ("post" if post else "dry_run") + ("+inline" if inline else "")

        summary_body: str | None = None
        summary_truncated = False
        summary_redaction_safe = True
        summary_failure_reason = ""
        if summary_comment:
            _emit(evidence, "summary_render_started", repository=repo_slug, pr=pr_number, status="started")
            candidate, summary_truncated = render_summary_comment(
                repo_slug=repo_slug,
                pr_number=pr_number,
                head_sha=metadata.head_sha,
                base_sha=metadata.base_sha,
                run_id=run_id,
                review_mode=review_mode_str,
                policy_decision=policy_decision_str,
                findings=list(findings),
                abstention_count=sum(1 for f in findings if f.get("status") == "abstained"),
                invalid_finding_count=len(manifest.provider_validation_errors or []),
                policy_blocked_count=sum(1 for e in manifest.errors if "blocked by policy" in e.lower()),
                planned_inline=len(fresh_inline),
                planned_fallback=len(fresh_issue),
                fallback_reasons=fallback_reasons,
                suppressed_unanchored=suppressed_unanchored,
                suppressed_outside_diff=suppressed_outside_diff,
                suppressed_redacted=suppressed_redacted,
                tests_status=manifest.tests_status,
                full_suite_status=manifest.full_suite_status,
                test_pass_count=manifest.test_pass_count,
                test_failure_count=manifest.test_failure_count,
                failed_test_names=list(manifest.failed_test_names or []),
                test_execution_attempted=manifest.test_execution_attempted,
                packet_truncated=manifest.packet_truncated,
                evidence_limitations=list(manifest.packet_limitations or []) + list(manifest.diagnostic_limitations or []),
                network_isolation_verified=manifest.network_isolation_verified,
                max_bytes=github_cfg["max_summary_bytes"],
            )
            _emit(evidence, "summary_render_completed", repository=repo_slug, pr=pr_number,
                  status="ok", bytes=len(candidate.encode("utf-8")))
            if summary_truncated:
                _emit(evidence, "summary_comment_truncated", repository=repo_slug, pr=pr_number, status="truncated")
            _emit(evidence, "summary_redaction_completed", repository=repo_slug, pr=pr_number,
                  status="ok" if not _contains_raw_secret(candidate) else "unsafe")
            if _contains_raw_secret(candidate):
                summary_redaction_safe = False
                summary_failure_reason = "raw secret pattern in summary body; no write performed"
                _emit(evidence, "summary_comment_suppressed_redacted", repository=repo_slug,
                      pr=pr_number, reason=summary_failure_reason, status="redacted")
            else:
                summary_body = candidate

        check_payload: dict[str, Any] | None = None
        check_redaction_safe = True
        check_failure_reason = ""
        check_conclusion = ""
        if check_run:
            _emit(evidence, "check_run_render_started", repository=repo_slug, pr=pr_number, status="started")
            check_conclusion = map_check_conclusion(
                policy_decision=policy_decision_str,
                findings=list(findings),
                tests_status=manifest.tests_status,
                full_suite_status=manifest.full_suite_status,
                run_errors=list(manifest.errors),
            )
            check_summary = render_check_summary(
                repo_slug=repo_slug,
                pr_number=pr_number,
                head_sha=metadata.head_sha,
                run_id=run_id,
                status_label=_status_label(policy_decision_str),
                policy_decision=policy_decision_str,
                conclusion=check_conclusion,
                findings=list(findings),
                tests_status=manifest.tests_status,
                full_suite_status=manifest.full_suite_status,
                network_isolation_verified=manifest.network_isolation_verified,
                evidence_limitations=list(manifest.packet_limitations or []) + list(manifest.diagnostic_limitations or []),
            )
            _emit(evidence, "check_run_render_completed", repository=repo_slug, pr=pr_number,
                  conclusion=check_conclusion, status="ok")
            _emit(evidence, "check_run_redaction_completed", repository=repo_slug, pr=pr_number,
                  status="ok" if not _contains_raw_secret(check_summary) else "unsafe")
            if _contains_raw_secret(check_summary):
                check_redaction_safe = False
                check_failure_reason = "raw secret pattern in check-run summary; no write performed"
                _emit(evidence, "check_run_suppressed_redacted", repository=repo_slug,
                      pr=pr_number, reason=check_failure_reason, status="redacted")
            else:
                check_payload = {
                    "title": f"{CHECK_NAME} (#{pr_number})",
                    "summary": check_summary,
                    "conclusion": check_conclusion,
                    "external_id": check_external_id(pr_number, metadata.head_sha),
                }

        postable_comments = (
            (len(fresh_inline) + len(fresh_issue))
            if (inline or (not summary_comment and not check_run))
            else 0
        )
        planned_writes = postable_comments + (1 if summary_body is not None else 0) + (1 if check_payload is not None else 0)
        _emit(
            evidence,
            "github_comments_planned",
            repository=repo_slug,
            pr=pr_number,
            count=planned_writes,
            status="planned",
        )

        # 9. Ordered write groups (summary -> comments -> check run), each
        # gated by an immediate head-SHA re-check.  A SHA change with zero
        # writes so far fails closed; after partial writes it stops the
        # remaining groups and is reported.
        partial_write = False
        wrote_any = False

        def _sha_gate() -> None:
            pre_write = transport.get_pr_metadata(repo_slug, pr_number)
            if pre_write.head_sha != metadata.head_sha or pre_write.base_sha != metadata.base_sha:
                _emit(
                    evidence,
                    "github_sha_mismatch_failed",
                    repository=repo_slug,
                    pr=pr_number,
                    sha=pre_write.head_sha,
                    status="sha_changed_before_write",
                )
                raise SHAMismatchError(
                    "PR head SHA changed before the comment write batch; failing closed with no writes"
                )

        def _gate(group: str) -> bool:
            nonlocal partial_write
            try:
                _sha_gate()
                return True
            except SHAMismatchError:
                if wrote_any:
                    partial_write = True
                    errors.append(f"PR head SHA changed before the {group} write group; remaining writes skipped")
                    return False
                raise

        summary_write_attempted = False
        summary_write_succeeded = False
        summary_posted = False
        summary_updated = False
        summary_skipped_duplicate = False
        summary_comment_id: Any = None
        summary_marker_text = summary_marker(pr_number, metadata.head_sha)

        if summary_body is not None:
            if post:
                if _gate("summary"):
                    summary_write_attempted = True
                    try:
                        _emit(evidence, "summary_comment_lookup_started", repository=repo_slug, pr=pr_number, status="started")
                        matches = []
                        for c in transport.list_comments(repo_slug, pr_number):
                            m = parse_summary_marker(c.body)
                            if m is not None and int(m["pr"]) == pr_number:
                                matches.append(c)
                        current_head = [
                            c for c in matches
                            if (m := parse_summary_marker(c.body)) is not None and m.get("head") == metadata.head_sha
                        ]
                        if len(current_head) > 1:
                            _emit(evidence, "summary_comment_duplicate_found",
                                  repository=repo_slug, pr=pr_number, count=len(current_head), status="duplicates")
                            summary_skipped_duplicate = True
                        _emit(evidence, "summary_comment_write_started", repository=repo_slug, pr=pr_number, status="started")
                        if current_head:
                            target = min(
                                current_head,
                                key=lambda c: int(c.comment_id) if str(c.comment_id).isdigit() else 0,
                            )
                            updated = transport.update_comment(repo_slug, pr_number, target.comment_id, summary_body)
                            summary_updated = True
                            summary_comment_id = updated.comment_id
                            _emit(evidence, "summary_comment_updated", repository=repo_slug,
                                  pr=pr_number, comment_id=str(updated.comment_id), status="updated")
                        else:
                            created = transport.post_comment(repo_slug, pr_number, summary_body)
                            summary_posted = True
                            summary_comment_id = created.comment_id
                            _emit(evidence, "summary_comment_created", repository=repo_slug,
                                  pr=pr_number, comment_id=str(created.comment_id), status="created")
                        summary_write_succeeded = True
                        wrote_any = True
                    except GitHubError as err:
                        summary_failure_reason = f"{type(err).__name__}: {err}"
                        errors.append(f"Summary comment write failed: {summary_failure_reason}")
                        _emit(evidence, "summary_comment_write_failed", repository=repo_slug,
                              pr=pr_number, reason=summary_failure_reason, status="failed")

        # Per-finding comments are posted only when explicitly requested:
        # inline mode, or classic issue-comment mode without a summary or
        # check run (which already consolidate the findings).
        post_comments = post and (inline or (not summary_comment and not check_run))
        if post_comments and (fresh_inline or fresh_issue):
            if _gate("inline/fallback comment"):
                try:
                    # Comments only, never verdicts.
                    for finding, body, details in fresh_inline:
                        comment = transport.post_inline_comment(
                            repo_slug,
                            pr_number,
                            body,
                            commit_id=metadata.head_sha,
                            path=str(details["path"]),
                            line=int(details["line"]),
                            start_line=details.get("start_line"),
                        )
                        posted_ids.append(comment.comment_id)
                        inline_posted += 1
                        _emit(
                            evidence,
                            "github_comment_posted",
                            repository=repo_slug,
                            pr=pr_number,
                            finding_id=str(finding.get("id", "?")),
                            comment_id=str(comment.comment_id),
                            status="posted_inline",
                        )
                    for finding, body in fresh_issue:
                        comment = transport.post_comment(repo_slug, pr_number, body)
                        posted_ids.append(comment.comment_id)
                        issue_posted += 1
                        _emit(
                            evidence,
                            "github_comment_posted",
                            repository=repo_slug,
                            pr=pr_number,
                            finding_id=str(finding.get("id", "?")),
                            comment_id=str(comment.comment_id),
                            status="posted_issue",
                        )
                    wrote_any = True
                except GitHubError as err:
                    errors.append(f"Comment write failed: {type(err).__name__}: {err}")

        check_write_attempted = False
        check_created = False
        check_updated = False
        check_skipped_duplicate = False
        check_run_id: Any = None
        if check_payload is not None:
            if post:
                if _gate("check run"):
                    check_write_attempted = True
                    try:
                        _emit(evidence, "check_run_lookup_started", repository=repo_slug, pr=pr_number,
                              sha=metadata.head_sha, status="started")
                        existing_checks = [
                            c for c in transport.list_check_runs(repo_slug, metadata.head_sha)
                            if str(c.get("external_id", "")) == check_payload["external_id"]
                            or c.get("name") == CHECK_NAME
                        ]
                        if existing_checks:
                            if len(existing_checks) > 1:
                                check_skipped_duplicate = True
                            target = min(existing_checks, key=lambda c: int(c.get("id", 0)))
                            result = transport.update_check_run(
                                repo_slug, target["id"],
                                title=check_payload["title"],
                                summary=check_payload["summary"],
                                conclusion=check_payload["conclusion"],
                            )
                            check_updated = True
                            check_run_id = result.get("id", target["id"])
                            _emit(evidence, "check_run_updated", repository=repo_slug,
                                  pr=pr_number, conclusion=check_payload["conclusion"], status="updated")
                        else:
                            result = transport.create_check_run(
                                repo_slug, metadata.head_sha,
                                name=CHECK_NAME,
                                title=check_payload["title"],
                                summary=check_payload["summary"],
                                conclusion=check_payload["conclusion"],
                                external_id=check_payload["external_id"],
                            )
                            check_created = True
                            check_run_id = result.get("id")
                            _emit(evidence, "check_run_created", repository=repo_slug,
                                  pr=pr_number, conclusion=check_payload["conclusion"], status="created")
                    except GitHubError as err:
                        check_failure_reason = f"{type(err).__name__}: {err}"
                        errors.append(f"Check-run write failed: {check_failure_reason}")
                        _emit(evidence, "check_run_write_failed", repository=repo_slug,
                              pr=pr_number, reason=check_failure_reason, status="failed")

        _emit(
            evidence,
            "github_run_completed",
            repository=repo_slug,
            pr=pr_number,
            status="ok",
        )

        report = GitHubReviewReport(
            repository=repo_slug,
            pr_number=pr_number,
            head_sha=metadata.head_sha,
            base_sha=metadata.base_sha,
            mode=review_mode_str + ("+summary" if summary_comment else "") + ("+check" if check_run else ""),
            run_id=run_id,
            review_summary=review_result.summary,
            review_policy=policy_decision_str,
            findings_total=len(findings),
            findings_anchored=len(anchored),
            comments_planned=planned_writes,
            comments_posted=len(posted_ids) + (1 if summary_write_succeeded else 0),
            inline_comments_posted=inline_posted,
            issue_comments_posted=issue_posted,
            comments_skipped_duplicate=skipped_duplicate,
            comments_suppressed_unanchored=suppressed_unanchored,
            comments_suppressed_redacted=suppressed_redacted,
            comments_suppressed_outside_diff=suppressed_outside_diff,
            comments_fallback_issue=len(fallback_reasons),
            fallback_reasons=fallback_reasons,
            write_calls=getattr(transport, "write_calls", len(posted_ids)),
            errors=errors,
            posted_comment_ids=posted_ids,
            partial_write=partial_write,
            summary_comment_requested=summary_comment,
            summary_comment_write_attempted=summary_write_attempted,
            summary_comment_write_succeeded=summary_write_succeeded,
            summary_comment_posted=summary_posted,
            summary_comment_updated=summary_updated,
            summary_comment_skipped_duplicate=summary_skipped_duplicate,
            summary_comment_id=summary_comment_id,
            summary_comment_marker=summary_marker_text,
            summary_comment_truncated=summary_truncated,
            summary_comment_bytes=len(summary_body.encode("utf-8")) if summary_body is not None else 0,
            summary_comment_redaction_safe=summary_redaction_safe,
            summary_comment_failure_reason=summary_failure_reason,
            summary_comment_head_sha=metadata.head_sha,
            check_run_requested=check_run,
            check_run_write_attempted=check_write_attempted,
            check_run_created=check_created,
            check_run_updated=check_updated,
            check_run_skipped_duplicate=check_skipped_duplicate,
            check_run_id=check_run_id,
            check_run_head_sha=metadata.head_sha,
            check_run_status="completed" if (check_created or check_updated) else "",
            check_run_conclusion=check_conclusion,
            check_run_redaction_safe=check_redaction_safe,
            check_run_failure_reason=check_failure_reason,
        )
        if json_output:
            out = Path(json_output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return report
    finally:
        if evidence_ctx is not None:
            evidence_ctx.__exit__(None, None, None)


__all__ = [
    "run_github_pr_review",
    "render_finding_comment",
    "finding_marker",
    "parse_marker",
    "MAX_COMMENTS_PER_RUN",
]
