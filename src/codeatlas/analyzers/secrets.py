"""Conservative, diff-scoped hardcoded-secret detection."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from typing import Pattern

from codeatlas.core.language import Language, detect_language
from codeatlas.findings import Finding
from codeatlas.git.models import ChangeStatus, Diff, FileChange

from .models import AnalysisContext

_SUPPORTED = {Language.PYTHON, Language.JAVASCRIPT, Language.TYPESCRIPT}
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_AWS = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_GITHUB = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_DB_URL = re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss)://[^\s\"']+:[^\s@\"']+@", re.I)
_ASSIGNMENT = re.compile(
    r"\b(?P<name>api[_-]?key|apikey)\b\s*[:=]\s*(?P<quote>[\"'`])(?P<value>[^\"'`\n]+)(?P=quote)", re.I
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"\b(?P<name>password|passwd|token|secret|credential|credentials)\b\s*[:=]\s*(?P<quote>[\"'`])(?P<value>[^\"'`\n]+)(?P=quote)", re.I
)
_PLACEHOLDER = re.compile(
    r"^(?:$|changeme|change[-_ ]?me|your[_-]?(?:api[_-]?key|token|secret|password)|"
    r"(?:xxx+|dummy|example|sample|test|todo|replace(?:[-_ ]?me)?|none|null|n/?a)|<[^>]+>|\$\{[^}]+\})$",
    re.I,
)
_ENV_REF = re.compile(r"(?:os\.getenv|os\.environ|process\.env|import\.meta\.env|System\.getenv|\benv\b|\$\{[^}]+\})", re.I)


class HardcodedSecretAnalyzer:
    """Find likely credentials only on added lines of supported source files."""

    name = "hardcoded-secrets"
    supported_languages = {language.value for language in _SUPPORTED}

    def __init__(
        self,
        *,
        allow_patterns: Iterable[str | Pattern[str]] = (),
        ignore_placeholders: bool = True,
        min_confidence: float = 0.0,
    ) -> None:
        self.allow_patterns = tuple(allow_patterns)
        self.ignore_placeholders = ignore_placeholders
        self.min_confidence = min_confidence

    def analyze(self, context: AnalysisContext | object, diff: Diff | None = None) -> Sequence[Finding]:
        """Analyze a context; ``analyze(snapshot, diff)`` is supported for convenience."""
        if isinstance(context, AnalysisContext):
            ctx = context
        else:
            if diff is None:
                raise TypeError("analyze requires AnalysisContext or snapshot and Diff")
            ctx = AnalysisContext(
                context,
                diff,
                self.allow_patterns,
                self.ignore_placeholders,
                self.min_confidence,
            )
        root = ctx.snapshot_path
        findings: list[Finding] = []
        for change in ctx.diff.files:
            # A rename has a new path and new-side ranges just like a modified
            # file.  Deleted files have no readable head snapshot and must not
            # be scanned.
            if change.status not in {ChangeStatus.ADDED, ChangeStatus.MODIFIED, ChangeStatus.RENAMED, ChangeStatus.COPIED}:
                continue
            if detect_language(change.path) not in _SUPPORTED:
                continue
            file_path = root / change.path
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            allowed = tuple(ctx.allow_patterns) + self.allow_patterns
            for line_no in _changed_lines(change):
                if line_no > len(lines):
                    continue
                line = lines[line_no - 1]
                for rule, match, confidence in _matches(line):
                    value = _matched_value(rule, match)
                    if _ignored(line, value, allowed, ctx.ignore_placeholders):
                        continue
                    if confidence < ctx.min_confidence:
                        continue
                    findings.append(_finding(change.path, line_no, rule, line, confidence))
        return tuple(findings)


def _changed_lines(change: FileChange) -> set[int]:
    return {line for item in change.new_ranges for line in range(item.start, item.end + 1)}


def _matches(line: str) -> list[tuple[str, re.Match[str], float]]:
    matches: list[tuple[str, re.Match[str], float]] = []
    for rule, pattern, confidence in (
        ("private-key", _PRIVATE_KEY, 0.99),
        ("aws-access-key", _AWS, 0.98),
        ("github-token", _GITHUB, 0.98),
        ("database-url", _DB_URL, 0.96),
        ("api-key-assignment", _ASSIGNMENT, 0.91),
        ("sensitive-assignment", _SENSITIVE_ASSIGNMENT, 0.88),
    ):
        match = pattern.search(line)
        if match:
            matches.append((rule, match, confidence))
    # A recognizable token is more specific than the broad assignment rule.
    if any(rule in {"private-key", "aws-access-key", "github-token", "database-url"} for rule, _, _ in matches):
        matches = [item for item in matches if item[0] not in {"api-key-assignment", "sensitive-assignment"}]
    return matches


def _matched_value(rule: str, match: re.Match[str]) -> str:
    if rule in {"api-key-assignment", "sensitive-assignment"}:
        return match.group("value")
    return match.group(0)


def _ignored(line: str, value: str, patterns: tuple[str | Pattern[str], ...], placeholders: bool) -> bool:
    if any(re.search(pattern, line) for pattern in patterns):
        return True
    if _ENV_REF.search(line):
        return True
    return placeholders and _PLACEHOLDER.fullmatch(value.strip()) is not None


def _finding(path: str, line_no: int, rule: str, line: str, confidence: float) -> Finding:
    normalized = re.sub(r"\s+", " ", line.strip()).lower()
    # Rule-specific matches are replaced before hashing; the original line never enters a Finding.
    for pattern in (_PRIVATE_KEY, _AWS, _GITHUB, _DB_URL, _ASSIGNMENT, _SENSITIVE_ASSIGNMENT):
        normalized = pattern.sub("<redacted>", normalized)
    digest = hashlib.sha256(f"{path.replace(chr(92), '/')}/{rule}/{line_no}/{normalized}".encode()).hexdigest()[:16].upper()
    redacted = _redacted_evidence(rule, line)
    return Finding(
        id=f"CA-SECRET-{digest}",
        file=path,
        start_line=line_no,
        end_line=line_no,
        severity="blocker" if rule == "private-key" else "high",
        category="HARD_CODED_SECRET",
        claim=f"The changed line contains a likely {rule.replace('-', ' ')}.",
        impact="Credentials committed in source can be exposed and reused.",
        evidence_strength="strong" if confidence >= 0.95 else "supported",
        confidence=confidence,
        evidence=[f"rule={rule}; redacted_match={redacted}; line_kind=added"],
        tools_consulted=["deterministic-secret-analyzer"],
        fixability="review_required",
        status="detected",
        limitations=["Pattern-based detection may require human review."],
        provenance={"analyzer": "hardcoded-secrets", "rule": rule},
    )


def _redacted_evidence(rule: str, line: str) -> str:
    """Return a bounded, non-sensitive representation of a matching line."""
    patterns = (_PRIVATE_KEY, _AWS, _GITHUB, _DB_URL, _ASSIGNMENT, _SENSITIVE_ASSIGNMENT)
    redacted = line.strip()
    for pattern in patterns:
        redacted = pattern.sub(lambda match: _mask(match.group(0)), redacted)
    return redacted[:240]


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:3]}{'*' * max(8, len(value) - 7)}{value[-4:]}"


__all__ = ["HardcodedSecretAnalyzer"]
