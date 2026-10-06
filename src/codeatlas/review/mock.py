"""Deterministic MockReviewer implementation for testing and controlled evaluation."""

from __future__ import annotations

from typing import Any
from codeatlas.review.packet import ReviewPacket
from codeatlas.review.provider import ReviewerResult


class MockReviewer:
    """Deterministic, offline reviewer provider for pipeline verification."""

    name: str = "mock"
    version: str = "1.0.0"
    network_access: bool = False

    def __init__(
        self,
        mode: str = "clean",
        *,
        custom_findings: list[dict[str, Any]] | None = None,
    ) -> None:
        self.mode = mode
        self.custom_findings = custom_findings or []

    def review(self, packet: ReviewPacket) -> ReviewerResult:
        """Deterministically produce reviewer findings based on packet and configured mode."""
        findings: list[dict[str, Any]] = []
        limitations: list[str] = []
        abstentions: list[str] = []

        if self.mode == "clean":
            return ReviewerResult(
                provider_name=self.name,
                provider_version=self.version,
                findings=[],
                summary="Clean review: no reviewer issues identified",
            )

        if self.mode == "custom" and self.custom_findings:
            return ReviewerResult(
                provider_name=self.name,
                provider_version=self.version,
                findings=list(self.custom_findings),
                summary="Custom findings provided",
            )

        first_file = next(
            (f for f in packet.changed_files if "__pycache__" not in f and not f.endswith(".pyc")),
            packet.changed_files[0] if packet.changed_files else "unknown.py",
        )
        ranges = packet.changed_line_ranges.get(first_file, [])
        first_range = ranges[0] if ranges else [1, 1]

        if self.mode == "echo_changed":
            findings.append({
                "id": "CA-REV-ECHO-1",
                "file": first_file,
                "start_line": first_range[0],
                "end_line": max(first_range[0], first_range[1]),
                "severity": "low",
                "category": "CODE_STYLE",
                "claim": f"Mock review observation on changed file {first_file}",
                "impact": "Code style observation for reviewer feedback",
                "evidence_strength": "supported",
                "confidence": 0.85,
                "evidence": [f"Diff line range {first_range}"],
                "tools_consulted": [],
                "tests_consulted": [],
                "fixability": "review_required",
                "status": "detected",
                "limitations": [],
                "provenance": {"origin": "reviewer", "provider": "mock"},
            })

        elif self.mode == "invalid_json":
            # Missing required fields like id, claim, severity
            findings.append({
                "file": first_file,
                "start_line": 1,
            })

        elif self.mode == "invalid_path":
            findings.append({
                "id": "CA-REV-BADPATH",
                "file": "nonexistent/external/escaped.py",
                "start_line": 1,
                "end_line": 1,
                "severity": "low",
                "category": "STYLE",
                "claim": "Path outside repository",
                "impact": "None",
                "evidence_strength": "none",
                "confidence": 0.9,
                "evidence": [],
                "tools_consulted": [],
                "tests_consulted": [],
                "fixability": "review_required",
                "status": "detected",
                "limitations": [],
                "provenance": {},
            })

        elif self.mode == "invalid_line_range":
            findings.append({
                "id": "CA-REV-BADRANGE",
                "file": first_file,
                "start_line": 50,
                "end_line": 10,  # end < start
                "severity": "low",
                "category": "STYLE",
                "claim": "Invalid line range",
                "impact": "None",
                "evidence_strength": "none",
                "confidence": 0.9,
                "evidence": [],
                "tools_consulted": [],
                "tests_consulted": [],
                "fixability": "review_required",
                "status": "detected",
                "limitations": [],
                "provenance": {},
            })

        elif self.mode == "duplicate_id":
            dup1 = {
                "id": "CA-REV-DUP1",
                "file": first_file,
                "start_line": first_range[0],
                "end_line": max(first_range[0], first_range[1]),
                "severity": "low",
                "category": "STYLE",
                "claim": "Duplicate claim 1",
                "impact": "None",
                "evidence_strength": "supported",
                "confidence": 0.85,
                "evidence": [],
                "tools_consulted": [],
                "tests_consulted": [],
                "fixability": "review_required",
                "status": "detected",
                "limitations": [],
                "provenance": {},
            }
            dup2 = dict(dup1)
            findings.extend([dup1, dup2])

        elif self.mode in {"duplicate_finding", "duplicate_overlap"}:
            cat = packet.deterministic_findings[0].get("category", "HARDCODED_SECRET") if packet.deterministic_findings else "HARDCODED_SECRET"
            findings.append({
                "id": "CA-REV-DUP-MERGE",
                "file": first_file,
                "start_line": first_range[0],
                "end_line": max(first_range[0], first_range[1]),
                "severity": "medium",
                "category": cat,
                "claim": f"Reviewer observation matching deterministic {cat}",
                "impact": "Security overlap observation",
                "evidence_strength": "supported",
                "confidence": 0.85,
                "evidence": ["Reviewer confirms line range"],
                "tools_consulted": [],
                "tests_consulted": [],
                "fixability": "review_required",
                "status": "detected",
                "limitations": [],
                "provenance": {"origin": "reviewer"},
            })

        elif self.mode == "unsupported_validated":
            findings.append({
                "id": "CA-REV-UNSUPPORTED-VAL",
                "file": first_file,
                "start_line": first_range[0],
                "end_line": first_range[1],
                "severity": "high",
                "category": "LOGIC",
                "claim": "Claims validated fix",
                "impact": "Unsupported claim",
                "evidence_strength": "supported",
                "confidence": 0.95,
                "evidence": [],
                "tools_consulted": [],
                "tests_consulted": [],
                "fixability": "validated",  # Illegal without isolated verification
                "status": "validated",
                "limitations": [],
                "provenance": {},
            })

        elif self.mode == "fabricated_test":
            findings.append({
                "id": "CA-REV-FABRICATED-TEST",
                "file": first_file,
                "start_line": first_range[0],
                "end_line": first_range[1],
                "severity": "medium",
                "category": "TEST_FAILURE",
                "claim": "Fabricated test failure",
                "impact": "Misleading test consulting claim",
                "evidence_strength": "supported",
                "confidence": 0.9,
                "evidence": [],
                "tools_consulted": [],
                "tests_consulted": ["tests/nonexistent_test.py"],
                "fixability": "review_required",
                "status": "detected",
                "limitations": [],
                "provenance": {},
            })

        elif self.mode == "low_confidence":
            findings.append({
                "id": "CA-REV-LOWCONF",
                "file": first_file,
                "start_line": first_range[0],
                "end_line": first_range[1],
                "severity": "low",
                "category": "STYLE",
                "claim": "Low confidence suggestion",
                "impact": "Minor style issue",
                "evidence_strength": "weak",
                "confidence": 0.35,  # Below standard threshold
                "evidence": [],
                "tools_consulted": [],
                "tests_consulted": [],
                "fixability": "review_required",
                "status": "detected",
                "limitations": [],
                "provenance": {},
            })

        elif self.mode == "secret_leak":
            findings.append({
                "id": "CA-REV-SECRETLEAK",
                "file": first_file,
                "start_line": first_range[0],
                "end_line": first_range[1],
                "severity": "high",
                "category": "SECURITY",
                "claim": "Unredacted secret AKIA1234567890EXAMPLE",
                "impact": "Leaked raw credential",
                "evidence_strength": "supported",
                "confidence": 0.95,
                "evidence": ["AKIA1234567890EXAMPLE"],
                "tools_consulted": [],
                "tests_consulted": [],
                "fixability": "review_required",
                "status": "detected",
                "limitations": [],
                "provenance": {},
            })

        return ReviewerResult(
            provider_name=self.name,
            provider_version=self.version,
            findings=findings,
            summary=f"MockReviewer run mode: {self.mode}",
            limitations=limitations,
            abstentions=abstentions,
        )


class MockRepairReviewer:
    """Deterministic, offline repair provider for controlled proposal generation.

    Produces one placeholder draft that neutralizes the flagged lines with a
    language-appropriate no-op, anchored to the exact finding location from the
    bounded ``RepairContext``. It is explicitly a draft: the placeholder must be
    replaced by a real fix during human review. No network, no file access, no
    approval, and no execution claims.
    """

    name: str = "mock-repair"
    version: str = "11C-B.1"
    network_access: bool = False

    def propose(self, context: Any) -> ReviewerResult:
        lines = context.code_context.splitlines()
        start, end = context.changed_line_range
        if start < 1 or end > len(lines) or end < start:
            return ReviewerResult(
                provider_name=self.name,
                provider_version=self.version,
                abstentions=["finding location outside supplied code context"],
            )
        first = lines[start - 1]
        indent = first[: len(first) - len(first.lstrip(" \t"))]
        if context.language == "python":
            replacement = f"{indent}pass  # TODO(codeatlas): draft fix for {context.finding_id}; requires human review"
        else:
            replacement = f"{indent}// TODO(codeatlas): draft fix for {context.finding_id}; requires human review"
        new_lines = lines[: start - 1] + [replacement] + lines[end:]
        hunk_lines: list[str] = []
        for offset, line in enumerate(lines, start=context.code_line_range[0]):
            if start <= offset <= end:
                hunk_lines.append("-" + line)
            else:
                hunk_lines.append(" " + line)
            if offset == end:
                hunk_lines.append("+" + replacement)
        old_count = len(lines)
        new_count = len(new_lines)
        unified_diff = (
            f"--- a/{context.changed_file}\n"
            f"+++ b/{context.changed_file}\n"
            f"@@ -{context.code_line_range[0]},{old_count} +{context.code_line_range[0]},{new_count} @@\n"
            + "\n".join(hunk_lines)
            + "\n"
        )
        suggestion = {
            "suggestion_id": f"{context.finding_id}-mock-draft",
            "finding_id": context.finding_id,
            "target_files": [context.changed_file],
            "unified_diff": unified_diff,
            "rationale": (
                "Placeholder draft: replace the flagged lines with a neutral no-op so the "
                "reported problem is removed without inventing behavior."
            ),
            "expected_behavior": (
                "The flagged lines are removed pending a real fix; a human must supply the "
                "intended replacement before approval."
            ),
            "risk_level": "medium",
            "limitations": [
                "The placeholder removes the flagged code instead of repairing it.",
                "A human must replace the placeholder with the intended fix before approval.",
            ],
            "provider_provenance": {"origin": "mock-repair"},
        }
        return ReviewerResult(
            provider_name=self.name,
            provider_version=self.version,
            patch_suggestions=[suggestion],
        )


__all__ = ["MockReviewer", "MockRepairReviewer"]
