"""Phase 11B deterministic finding-quality and trust-boundary tests."""

from __future__ import annotations

import json

from codeatlas.findings.models import EvidenceStrength, Finding, Severity
from codeatlas.findings.rendering import render_json, render_markdown
from codeatlas.github.adapter import render_finding_comment
from codeatlas.review.packet import ContextItem, RedactionAudit, ReviewPacket, audit_packet_redaction
from codeatlas.review.quality import assess_finding, quality_summary
from codeatlas.review.ranking import merge_and_rank_findings
from codeatlas.review.validator import validate_provider_output


def _finding(
    finding_id: str,
    *,
    origin: str = "deterministic",
    claim: str = "A changed value reaches a sensitive sink",
    severity: Severity = "high",
    evidence_strength: EvidenceStrength = "supported",
    confidence: float = 0.9,
    file: str = "src/app.py",
    start: int = 3,
    end: int = 3,
    unsupported: bool = False,
) -> Finding:
    return Finding(
        id=finding_id,
        file=file,
        start_line=start,
        end_line=max(end, start),
        severity=severity,
        category="SENSITIVE_DATA_EXPOSURE",
        claim=claim,
        impact="Sensitive data may be disclosed.",
        evidence_strength=evidence_strength,
        confidence=confidence,
        evidence=["rule=bounded; redacted_match=<redacted>"] if evidence_strength != "none" else [],
        tools_consulted=["deterministic-analyzer"] if origin == "deterministic" else [],
        fixability="review_required",
        status="detected",
        provenance={"origin": origin, "unsupported_flow": unsupported},
        unsupported_flow=unsupported,
    )


def _packet(*, truncated: bool = False) -> ReviewPacket:
    context = ContextItem(
        file="src/app.py",
        line_range=[3, 3],
        reason="changed code",
        ranking_score=1.0,
        source_type="changed_code",
        truncation_status="truncated" if truncated else "full",
    )
    return ReviewPacket(
        packet_id="pkt-quality",
        repository="repo",
        changed_files=["src/app.py"],
        changed_line_ranges={"src/app.py": [[3, 3]]},
        context_candidates=[context],
        relevant_tests=["tests/test_app.py"],
        redaction_status=RedactionAudit(redacted=True),
        truncated=truncated,
    )


def test_quality_score_components_are_bounded_and_explained():
    result = assess_finding(_finding("CA-Q-1"), packet=_packet(), deterministic_support=1.0)
    assert result.quality_version == "11B.1"
    assert 0 <= result.confidence <= 1
    assert 0 <= result.quality_score <= 1
    assert all(0 <= value <= 1 for value in result.score_components.values())
    assert "Quality decision" in result.provenance["quality_explanation"]
    assert result.changed_line_support == 1


def test_exact_and_overlapping_duplicates_preserve_strongest_and_provenance():
    deterministic = _finding("CA-Q-DET", claim="Same issue")
    reviewer = _finding("CA-Q-REV", origin="reviewer", claim="Same issue", confidence=0.99)
    merged = merge_and_rank_findings([deterministic], [reviewer], packet=_packet())
    assert len(merged) == 1
    assert merged[0].id.startswith("CA-MRG-")
    assert merged[0].suppressed_finding_ids == ["CA-Q-REV"]
    assert merged[0].duplicate_group_id
    assert merged[0].provenance["deterministic_finding_ids"] == ["CA-Q-DET"]
    assert merged[0].provenance["reviewer_finding_ids"] == ["CA-Q-REV"]

    overlapping = merge_and_rank_findings(
        [_finding("CA-Q-DET2", claim="Shared evidence", start=3, end=5)],
        [_finding("CA-Q-REV2", origin="reviewer", claim="Shared evidence", start=4, end=4)],
        packet=_packet(),
    )
    assert len(overlapping) == 1
    assert overlapping[0].start_line == 3 and overlapping[0].end_line == 5


def test_provider_agreement_is_uncertain_but_conflict_abstains():
    agreement = merge_and_rank_findings(
        [_finding("CA-Q-DET3", claim="Same issue")],
        [_finding("CA-Q-REV3", origin="reviewer", claim="Same issue")],
        packet=_packet(),
    )[0]
    assert agreement.deterministic_support == 1
    assert agreement.quality_decision in {"report", "report_with_uncertainty"}
    assert agreement.severity == "high"

    conflict = merge_and_rank_findings(
        [_finding("CA-Q-DET4", claim="Deterministic observation")],
        [_finding("CA-Q-REV4", origin="reviewer", claim="Conflicting provider observation", severity="blocker")],
        packet=_packet(),
    )[0]
    assert conflict.quality_decision == "abstain"
    assert conflict.status == "abstained"
    assert conflict.severity == "high", "provider severity must not inflate deterministic severity"
    assert conflict.abstention_reason and "conflict" in conflict.abstention_reason.lower()


def test_weak_unsupported_and_truncated_findings_do_not_report():
    weak = merge_and_rank_findings(
        [],
        [_finding("CA-Q-WEAK", origin="reviewer", evidence_strength="none", confidence=0.2)],
        packet=_packet(),
    )[0]
    assert weak.quality_decision in {"suppress_low_evidence", "review_only"}

    unsupported = assess_finding(_finding("CA-Q-UNSUPPORTED", unsupported=True), packet=_packet())
    assert unsupported.quality_decision == "abstain"
    assert unsupported.status == "abstained"
    assert "unsupported" in (unsupported.abstention_reason or "").lower()

    truncated = assess_finding(_finding("CA-Q-TRUNCATED"), packet=_packet(truncated=True))
    assert truncated.quality_decision == "abstain"
    assert "truncated" in (truncated.abstention_reason or "").lower()


def test_stable_ranking_and_schema_compatibility():
    findings = merge_and_rank_findings(
        [_finding("CA-Q-LOW", severity="low", start=3), _finding("CA-Q-HIGH", severity="high", start=4)],
        [],
        packet=None,
    )
    assert [item.id for item in findings] == ["CA-Q-HIGH", "CA-Q-LOW"]
    assert [item.id for item in merge_and_rank_findings(
        [_finding("CA-Q-LOW", severity="low", start=3), _finding("CA-Q-HIGH", severity="high", start=4)], [], packet=None
    )] == ["CA-Q-HIGH", "CA-Q-LOW"]

    old_payload = _finding("CA-Q-OLD").model_dump(mode="json")
    for key in list(old_payload):
        if key.startswith("quality_") or key in {
            "evidence_sources", "deterministic_support", "reviewer_support", "changed_line_support",
            "repository_context_support", "test_support", "ambiguity_score", "truncation_penalty",
            "unsupported_flow", "duplicate_group_id", "suppressed_finding_ids", "quality_score",
            "score_components", "feedback",
        }:
            old_payload.pop(key, None)
    compatible = Finding.model_validate(old_payload)
    assert compatible.quality_version == "11B.1"


def test_renderers_include_quality_and_remain_redacted_and_bounded():
    finding = assess_finding(_finding("CA-Q-RENDER"), packet=_packet())
    payload = render_json([finding])
    markdown = render_markdown([finding])
    github = render_finding_comment(1, "a" * 40, "run-quality", finding.model_dump(mode="json"))
    for output in (payload, markdown, github):
        assert "quality_decision" in output or "Quality" in output
        assert "AKIA" not in output
        assert len(output.encode("utf-8")) < 50_000
    assert audit_packet_redaction(json.loads(render_json([finding]))[0]).redacted


def test_quality_summary_is_bounded():
    findings = [assess_finding(_finding(f"CA-Q-{i}"), packet=_packet()) for i in range(20)]
    summary = quality_summary(findings)
    assert summary["quality_version"] == "11B.1"
    assert summary["finding_count"] == 20
    assert sum(summary["decision_counts"].values()) == 20
    assert len(summary["limitations"]) <= 20


def test_invalid_provider_output_cannot_enter_quality_pipeline():
    packet = _packet()
    result = validate_provider_output(
        packet,
        [{"id": "CA-Q-BAD", "file": "src/app.py", "start_line": 3, "end_line": 3, "severity": "high"}],
    )
    assert not result.is_valid
    assert result.valid_findings == []


def test_provider_quality_claims_feedback_and_provenance_are_recomputed():
    provider = _finding("CA-Q-PROVIDER", origin="reviewer")
    provider = provider.model_copy(
        update={
            "quality_decision": "report",
            "quality_score": 1.0,
            "deterministic_support": 1.0,
            "feedback": "accepted",
            "provenance": {
                "origin": "reviewer",
                "provider": "untrusted",
                "quality_decision": "report",
                "quality_score": 1.0,
                "prompt": "must be shown",
            },
        }
    )
    result = merge_and_rank_findings([], [provider], packet=_packet())[0]
    assert result.deterministic_support == 0
    assert result.quality_decision != "report"
    assert result.feedback is None
    assert "prompt" not in result.provenance
    assert result.provenance["provider_observation"] is True


def test_duplicate_grouping_is_input_order_independent_for_bridging_ranges():
    first = _finding("CA-Q-BRIDGE-A", start=3, end=3)
    bridge = _finding("CA-Q-BRIDGE-B", start=3, end=5)
    last = _finding("CA-Q-BRIDGE-C", start=5, end=5)
    result = merge_and_rank_findings([first, last, bridge], [], packet=_packet())
    assert len(result) == 1
    assert len(result[0].suppressed_finding_ids) == 2
    assert set(result[0].suppressed_finding_ids) <= {first.id, last.id, bridge.id}
