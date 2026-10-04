"""Validation for reviewer provider output against schemas and snapshot bounds."""

from __future__ import annotations

import json
from typing import Any
from pydantic import ValidationError

from codeatlas.findings.models import Finding
from codeatlas.review.packet import ReviewPacket, SECRET_PATTERNS, SECRET_RAW_MARKERS


class ValidationResult:
    """Result of validating provider findings."""

    def __init__(
        self,
        valid_findings: list[Finding],
        validation_errors: list[str],
    ) -> None:
        self.valid_findings = valid_findings
        self.validation_errors = validation_errors

    @property
    def is_valid(self) -> bool:
        return len(self.validation_errors) == 0


def validate_provider_output(
    packet: ReviewPacket,
    raw_findings: list[dict[str, Any]],
) -> ValidationResult:
    """Strictly validate provider findings against schema, snapshot, diff, and safety constraints."""
    valid_findings: list[Finding] = []
    errors: list[str] = []

    allowed_files = set(packet.changed_files) | {c.file for c in packet.context_candidates}
    seen_ids: set[str] = set()

    for idx, raw in enumerate(raw_findings):
        # 1. Pydantic schema validation
        try:
            finding = Finding.model_validate(raw)
        except ValidationError as err:
            errors.append(f"Finding[{idx}] failed schema validation: {err.errors()[0]['msg']}")
            continue

        # 2. Duplicate ID check
        if finding.id in seen_ids:
            errors.append(f"Finding[{idx}] has duplicate ID: {finding.id}")
            continue
        seen_ids.add(finding.id)

        # 3. File path inside snapshot / packet
        norm_file = finding.file.replace("\\", "/")
        if norm_file not in allowed_files:
            errors.append(f"Finding[{idx}] refers to file outside review packet: {finding.file}")
            continue

        # 4. Valid line range
        if finding.start_line < 1 or finding.end_line < finding.start_line:
            errors.append(f"Finding[{idx}] has invalid line range: [{finding.start_line}, {finding.end_line}]")
            continue

        # 5. Diff-scope check: must overlap with changed line ranges, or be explicitly marked context_only
        is_context_only = finding.provenance.get("context_only") is True
        if not is_context_only:
            file_ranges = packet.changed_line_ranges.get(norm_file, [])
            overlaps = any(
                not (finding.end_line < r[0] or finding.start_line > r[1])
                for r in file_ranges
            )
            if not overlaps:
                errors.append(
                    f"Finding[{idx}] ({finding.id}) at {finding.file}:{finding.start_line} "
                    f"is not diff-scoped and lacks context_only provenance"
                )
                continue

        # 6. Unsupported 'validated' claim check
        if finding.fixability == "validated":
            errors.append(
                f"Finding[{idx}] ({finding.id}) claims fixability='validated' without isolated runner proof"
            )
            continue

        # 7. Fabricated test results check
        known_tests = set(packet.relevant_tests)
        fabricated_tests = [t for t in finding.tests_consulted if t not in known_tests]
        if fabricated_tests:
            errors.append(f"Finding[{idx}] ({finding.id}) claims unexecuted test: {fabricated_tests[0]}")
            continue

        # 8. Raw secret check in claims, impacts, or evidence
        finding_text = json.dumps(
            finding.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
        )
        has_raw_secret = any(pat.search(finding_text) for pat in SECRET_PATTERNS) or any(
            marker.lower() in finding_text.lower() for marker in SECRET_RAW_MARKERS
        ) or any(marker in finding_text for marker in ("sk-", "Bearer ", "CAT-APP-"))
        if has_raw_secret:
            errors.append(f"Finding[{idx}] ({finding.id}) contains unredacted secret data")
            continue

        valid_findings.append(finding)

    return ValidationResult(valid_findings=valid_findings, validation_errors=errors)


__all__ = ["ValidationResult", "validate_provider_output"]
