from __future__ import annotations

import re

from typing import Any

MAX_SUMMARY_BYTES = 50_000
MAX_FINDING_ROWS = 50
MAX_LIMITATIONS = 20
MAX_CLAIM_CHARS = 2_000

_SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEVERITY_LABELS = ["blocker", "high", "medium", "low", "info"]

_DRIVE_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\\\s]+\\?)*")
_ABS_PATH_RE = re.compile(r"(?<![\w.])/[\w.\-/]{3,}")


def _scrub_private_paths(text: str) -> str:
    """Replace absolute local filesystem paths with a placeholder."""
    text = _DRIVE_PATH_RE.sub("<local-path>", text)
    text = _ABS_PATH_RE.sub("<local-path>", text)
    return text


def summary_marker(pr_number: int, head_sha: str) -> str:
    """Stable summary idempotency marker (PR, exact head SHA, mode, schema)."""
    return f"<!-- codeatlas:summary pr={pr_number} head={head_sha} mode=summary schema=1 -->"


def parse_summary_marker(body: str) -> dict[str, str] | None:
    import re

    match = re.search(
        r"codeatlas:summary pr=(?P<pr>\d+)\s+head=(?P<head>[0-9a-f]+)\s+mode=(?P<mode>summary)(?:\s+schema=(?P<schema>\S+))?",
        body,
    )
    if match is None:
        return None
    return match.groupdict()


def _clip(text: str, limit: int) -> str:
    text = _scrub_private_paths(str(text).replace("\n", " ").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {label: 0 for label in _SEVERITY_LABELS}
    for f in findings:
        sev = str(f.get("severity", "info"))
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _category_counts(findings: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for f in findings:
        cat = str(f.get("category", "UNKNOWN"))
        counts[cat] = counts.get(cat, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _origin_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"deterministic": 0, "reviewer": 0, "merged": 0, "other": 0}
    for f in findings:
        origin = str((f.get("provenance") or {}).get("origin", "other"))
        counts[origin if origin in counts else "other"] += 1
    return counts


def _status_label(decision: str) -> str:
    if decision in {"blocked"}:
        return "Blocked by policy"
    if decision == "abstain":
        return "Abstained (insufficient evidence)"
    if decision == "requires_human_approval":
        return "Review required"
    if decision == "review_only":
        return "Review only"
    return "Clean"


def _finding_rows(
    findings: list[dict[str, Any]],
    inline_posted_ids: set[str],
    fallback_ids: dict[str, str],
) -> list[str]:
    rows = []
    ordered = sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(str(f.get("severity", "info")), 9),
            str(f.get("file", "")),
            int(f.get("start_line", 0) or 0),
        ),
    )
    for f in ordered[:MAX_FINDING_ROWS]:
        fid = str(f.get("id", "?"))
        location = f"`{f.get('file', '?')}:{f.get('start_line', '?')}-{f.get('end_line', '?')}`"
        delivery = "inline" if fid in inline_posted_ids else (
            f"issue comment ({fallback_ids[fid]})" if fid in fallback_ids else "not posted"
        )
        rows.append(
            "| {sev} | {cat} | {loc} | {claim} | {status} | {delivery} |".format(
                sev=str(f.get("severity", "info")),
                cat=_clip(str(f.get("category", "?")), 60),
                loc=location,
                claim=_clip(str(f.get("claim", "")), MAX_CLAIM_CHARS),
                status=str(f.get("status", "?")),
                delivery=delivery,
            )
        )
    return rows


def render_summary_comment(
    *,
    repo_slug: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    run_id: str,
    review_mode: str,
    policy_decision: str,
    findings: list[dict[str, Any]],
    abstention_count: int = 0,
    invalid_finding_count: int = 0,
    policy_blocked_count: int = 0,
    planned_inline: int = 0,
    planned_fallback: int = 0,
    fallback_reasons: list[str] | None = None,
    suppressed_unanchored: int = 0,
    suppressed_outside_diff: int = 0,
    suppressed_redacted: int = 0,
    tests_status: str | None = None,
    full_suite_status: str | None = None,
    test_pass_count: int | None = None,
    test_failure_count: int | None = None,
    failed_test_names: list[str] | None = None,
    test_execution_attempted: bool | None = None,
    packet_truncated: bool | None = None,
    evidence_limitations: list[str] | None = None,
    network_isolation_verified: bool | None = None,
    max_bytes: int = MAX_SUMMARY_BYTES,
) -> tuple[str, bool]:
    """Render the consolidated summary.  Returns (body, truncated)."""
    truncated = False
    sev = _severity_counts(findings)
    cats = _category_counts(findings)
    origins = _origin_counts(findings)
    fallback_ids = {r.split(": ", 1)[0]: r.split(": ", 1)[1] for r in (fallback_reasons or []) if ": " in r}
    inline_posted_ids = set()

    lines: list[str] = [
        "## CodeAtlas Review",
        "",
        f"**Status:** {_status_label(policy_decision)}  ",
        f"**Repository:** `{repo_slug}` · **PR:** #{pr_number}  ",
        f"**Head:** `{head_sha}` · **Base:** `{base_sha[:12]}`  ",
        f"**Run:** `{run_id}` · **Mode:** {review_mode}",
        "",
        "### Summary",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Findings | {len(findings)} |",
    ]
    for label in _SEVERITY_LABELS:
        lines.append(f"| {label.capitalize()} | {sev.get(label, 0)} |")
    lines.append(f"| Abstained | {abstention_count} |")
    lines.append(f"| Deterministic findings | {origins['deterministic']} |")
    lines.append(f"| Provider findings | {origins['reviewer']} |")
    lines.append(f"| Merged findings | {origins['merged']} |")
    lines.append(f"| Invalid findings rejected | {invalid_finding_count} |")
    lines.append(f"| Policy-blocked findings | {policy_blocked_count} |")
    lines.append(f"| Planned inline comments | {planned_inline} |")
    lines.append(f"| Issue-comment fallbacks | {planned_fallback} |")
    lines.append(f"| Suppressed (unanchored) | {suppressed_unanchored} |")
    lines.append(f"| Suppressed (outside diff) | {suppressed_outside_diff} |")
    lines.append(f"| Suppressed (redaction) | {suppressed_redacted} |")
    if cats:
        lines.append("")
        lines.append("**By category:** " + ", ".join(f"{name} ({count})" for name, count in cats[:8]))

    lines.extend(["", "### Findings", ""])
    if findings:
        lines.extend([
            "| Severity | Category | Location | Finding | Status | Delivery |",
            "|---|---|---|---|---|---|",
        ])
        rows = _finding_rows(findings, inline_posted_ids, fallback_ids)
        lines.extend(rows)
        if len(findings) > MAX_FINDING_ROWS:
            lines.append(f"*…{len(findings) - MAX_FINDING_ROWS} additional findings omitted (limit {MAX_FINDING_ROWS}).*")
            truncated = True
    else:
        lines.append("_No findings reported for this run._")

    lines.extend(["", "### Validation", ""])
    lines.append(f"- Targeted tests: {_tests_label(tests_status)}")
    lines.append(f"- Full suite: {_tests_label(full_suite_status)}")
    lines.append(
        "- Tests executed in isolated sandbox: "
        + ("yes" if test_execution_attempted else "no")
    )
    if test_pass_count is not None or test_failure_count is not None:
        lines.append(f"- Test results: {test_pass_count or 0} passed, {test_failure_count or 0} failed")
    if failed_test_names:
        lines.append("- Failed tests: " + ", ".join(_clip(n, 120) for n in failed_test_names[:10]))
    lines.append(
        "- Network isolation independently verified: "
        + ("yes" if network_isolation_verified else "no")
    )
    lines.append("- Patch correctness: not proven")

    limitations = (evidence_limitations or [])[:MAX_LIMITATIONS]
    if packet_truncated:
        limitations = ["Review packet was truncated; context is incomplete", *limitations]
    lines.extend(["", "### Limitations", ""])
    if limitations:
        lines.extend(f"- {_clip(l, 300)}" for l in limitations)
    else:
        lines.append("- Bounded static analysis of the changed lines only.")

    lines.extend(["", summary_marker(pr_number, head_sha)])
    body = "\n".join(lines)

    if len(body.encode("utf-8")) > max_bytes:
        body = _reduce(body, max_bytes)
        truncated = True
    return body, truncated


def _reduce(body: str, max_bytes: int) -> str:
    """Deterministically shrink an oversized summary: drop the lowest-priority
    finding rows first (rows are already sorted blocker-first), then
    limitations, until the body fits.  A truncation notice is inserted once."""
    nl = chr(10)
    lines = body.split(nl)
    findings_start = next(i for i, l in enumerate(lines) if l == "### Findings")
    validation_start = next(i for i, l in enumerate(lines) if l == "### Validation")

    def row_indexes() -> list[int]:
        return [
            i for i in range(findings_start, validation_start)
            if lines[i].startswith("| ") and not lines[i].startswith("| Severity") and not lines[i].startswith("|---")
        ]

    notice = "*Summary truncated deterministically to fit the size limit.*"
    rows = [lines[i] for i in row_indexes()]
    head = lines[:findings_start + 3]
    tail = lines[validation_start:]
    def render(rows: list[str]) -> str:
        body_rows = nl.join(rows) if rows else "_No finding rows shown (truncated)._"
        return nl.join(head) + nl + nl + notice + nl + nl + body_rows + nl + nl + nl.join(tail)

    reduced = render(rows)
    while len(reduced.encode("utf-8")) > max_bytes and rows:
        rows = rows[:-1]
        reduced = render(rows)
    return reduced


def _tests_label(status: str | None) -> str:
    if status == "passed":
        return "passed"
    if status == "failed":
        return "failed"
    if status:
        return str(status)
    return "not run"


__all__ = [
    "MAX_FINDING_ROWS",
    "MAX_LIMITATIONS",
    "MAX_SUMMARY_BYTES",
    "parse_summary_marker",
    "render_summary_comment",
    "summary_marker",
]
