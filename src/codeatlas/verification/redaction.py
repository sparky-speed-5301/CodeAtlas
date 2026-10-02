"""Safe output capture, secret pattern redaction, and redaction audit."""

from __future__ import annotations

import re
from typing import Any, Sequence

from .models import OutputRedactionAudit

_SECRET_PATTERNS = [
    re.compile(r"(?i)(AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"(?i)ghp_[A-Za-z0-9_]{36}"),
    re.compile(r"(?i)github_pat_[A-Za-z0-9_]{22}_[A-Za-z0-9_]{59}"),
    re.compile(r"(?i)(?:postgres|postgresql|mysql|mongodb|redis)://[^:\s]+:[^@\s]+@[^\s]+"),
    re.compile(r"(?i)(?:api[_-]?key|password|token|secret)\s*[:=]\s*['\"][A-Za-z0-9_\-+=/.~]{12,}['\"]"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    re.compile(r"CAT-APP-[A-Za-z0-9_\-]{16,}"),
]


def redact_test_output(
    text: str,
    *,
    extra_tokens: Sequence[str] = (),
    target_name: str = "output",
) -> tuple[str, OutputRedactionAudit]:
    """Redact secret patterns, credentials, and explicit tokens from text.

    Returns the sanitized string and an OutputRedactionAudit.
    """
    if not text:
        return "", OutputRedactionAudit(
            safe=True,
            raw_value_matches=0,
            rules_applied=["regex_secret_scrubbing"],
            redacted_items_count=0,
            targets_audited=[target_name],
        )

    sanitized = text
    matches_count = 0
    rules_applied = ["regex_secret_scrubbing"]

    # Redact explicit tokens first
    for token in extra_tokens:
        if token and len(token) >= 6 and token in sanitized:
            count = sanitized.count(token)
            matches_count += count
            sanitized = sanitized.replace(token, "[REDACTED]")
            if "approval_token_scrubbing" not in rules_applied:
                rules_applied.append("approval_token_scrubbing")

    # Redact regex patterns
    for pat in _SECRET_PATTERNS:
        matches = pat.findall(sanitized)
        if matches:
            matches_count += len(matches)
            sanitized = pat.sub("[REDACTED]", sanitized)

    audit = OutputRedactionAudit(
        safe=True,
        raw_value_matches=matches_count,
        rules_applied=rules_applied,
        redacted_items_count=matches_count,
        targets_audited=[target_name],
    )
    return sanitized, audit


def audit_and_redact_results(
    stdout: str,
    stderr: str,
    failures: list[str],
    *,
    extra_tokens: Sequence[str] = (),
    max_output_bytes: int = 100_000,
) -> tuple[str, str, list[str], OutputRedactionAudit]:
    """Truncate, redact, and audit all output streams safely."""
    # First enforce output size limits
    stdout_raw = stdout[:max_output_bytes]
    stderr_raw = stderr[:max_output_bytes]

    redacted_stdout, audit1 = redact_test_output(
        stdout_raw, extra_tokens=extra_tokens, target_name="stdout"
    )
    redacted_stderr, audit2 = redact_test_output(
        stderr_raw, extra_tokens=extra_tokens, target_name="stderr"
    )

    redacted_failures: list[str] = []
    total_matches = audit1.raw_value_matches + audit2.raw_value_matches
    rules: set[str] = set(audit1.rules_applied + audit2.rules_applied)
    targets = ["stdout", "stderr"]

    for idx, f in enumerate(failures):
        f_redacted, f_audit = redact_test_output(
            f, extra_tokens=extra_tokens, target_name=f"failure_{idx}"
        )
        redacted_failures.append(f_redacted)
        total_matches += f_audit.raw_value_matches
        rules.update(f_audit.rules_applied)
        targets.append(f"failure_{idx}")

    combined_audit = OutputRedactionAudit(
        safe=True,
        raw_value_matches=total_matches,
        rules_applied=sorted(rules),
        redacted_items_count=total_matches,
        targets_audited=targets,
    )
    return redacted_stdout, redacted_stderr, redacted_failures, combined_audit


__all__ = [
    "redact_test_output",
    "audit_and_redact_results",
]
