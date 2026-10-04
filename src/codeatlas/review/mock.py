"""Deterministic MockReviewer implementation for testing and controlled evaluation."""

from __future__ import annotations

from typing import Any
from codeatlas.review.packet import ReviewPacket
from codeatlas.review.provider import ReviewerResult


class MockReviewer:
    """Deterministic, offline reviewer provider for pipeline verification."""

    name: str = "mock"
    version: str = "1.0.0"

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


__all__ = ["MockReviewer"]
