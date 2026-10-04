"""Phase 11B deterministic reliability and finding-quality evaluation."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from codeatlas.findings.models import Finding
from codeatlas.findings.rendering import render_json, render_markdown
from codeatlas.github.adapter import render_finding_comment
from codeatlas.review.packet import ContextItem, RedactionAudit, ReviewPacket, audit_packet_redaction
from codeatlas.review.quality import assess_finding
from codeatlas.review.ranking import merge_and_rank_findings
from codeatlas.review.validator import validate_provider_output
from codeatlas.service.models import ReviewCreateRequest
from codeatlas.service.state import ReviewRunRecord, ReviewStateManager


def _finding(finding_id: str, *, origin: str = "deterministic", claim: str = "same issue", start: int = 3, end: int = 3, **kwargs) -> Finding:
    return Finding(
        id=finding_id,
        file=kwargs.get("file", "src/app.py"),
        start_line=start,
        end_line=max(end, start),
        severity=kwargs.get("severity", "high"),
        category="QUALITY",
        claim=claim,
        impact="bounded impact",
        evidence_strength=kwargs.get("evidence_strength", "supported"),
        confidence=kwargs.get("confidence", 0.9),
        evidence=[] if kwargs.get("evidence_strength") == "none" else ["redacted evidence"],
        tools_consulted=["deterministic"] if origin == "deterministic" else [],
        fixability="review_required",
        status="detected",
        provenance={"origin": origin, "unsupported_flow": kwargs.get("unsupported", False)},
        unsupported_flow=kwargs.get("unsupported", False),
    )


def _packet(truncated: bool = False) -> ReviewPacket:
    return ReviewPacket(
        packet_id="pkt-11b",
        repository="quality-eval",
        changed_files=["src/app.py"],
        changed_line_ranges={"src/app.py": [[3, 3]]},
        context_candidates=[ContextItem(
            file="src/app.py", line_range=[3, 3], reason="changed", ranking_score=1.0,
            source_type="changed_code", truncation_status="truncated" if truncated else "full",
        )],
        redaction_status=RedactionAudit(redacted=True),
        truncated=truncated,
    )


def evaluate_case(case: dict) -> dict:
    kind = case["kind"]
    packet = _packet(truncated=kind == "truncated")
    checks: dict[str, bool] = {}
    if kind == "exact_duplicates":
        result = merge_and_rank_findings([_finding("CA-Q-DET", claim="same")], [_finding("CA-Q-REV", origin="reviewer", claim="same")], packet=packet)
        checks = {"one_strongest": len(result) == 1, "suppressed_id": result[0].suppressed_finding_ids == ["CA-Q-REV"], "grouped": bool(result[0].duplicate_group_id)}
    elif kind == "overlapping_duplicates":
        result = merge_and_rank_findings([_finding("CA-Q-DET", claim="shared", start=3, end=5)], [_finding("CA-Q-REV", origin="reviewer", claim="shared", start=4, end=4)], packet=packet)
        checks = {"one_group": len(result) == 1, "range_merged": result[0].start_line == 3 and result[0].end_line == 5}
    elif kind == "agreement":
        result = merge_and_rank_findings([_finding("CA-Q-DET", claim="agree")], [_finding("CA-Q-REV", origin="reviewer", claim="agree")], packet=packet)[0]
        checks = {"deterministic_retained": result.deterministic_support == 1, "not_abstained": result.quality_decision != "abstain", "provider_not_inflated": result.severity == "high"}
    elif kind == "conflict":
        result = merge_and_rank_findings([_finding("CA-Q-DET", claim="deterministic")], [_finding("CA-Q-REV", origin="reviewer", claim="provider", severity="blocker")], packet=packet)[0]
        checks = {"abstains": result.quality_decision == "abstain", "severity_preserved": result.severity == "high", "reason": bool(result.abstention_reason)}
    elif kind == "weak":
        result = merge_and_rank_findings([], [_finding("CA-Q-WEAK", origin="reviewer", evidence_strength="none", confidence=0.2)], packet=packet)[0]
        checks = {"not_report": result.quality_decision in {"suppress_low_evidence", "review_only"}, "bounded": 0 <= result.confidence <= 1}
    elif kind == "unsupported":
        result = assess_finding(_finding("CA-Q-UNSUPPORTED", unsupported=True), packet=packet)
        checks = {"abstains": result.quality_decision == "abstain", "explains": "unsupported" in (result.abstention_reason or "").lower()}
    elif kind == "ambiguous":
        result = assess_finding(_finding("CA-Q-AMBIG", start=8, end=9), packet=packet)
        checks = {"abstains": result.quality_decision == "abstain", "explains": bool(result.abstention_reason)}
    elif kind == "truncated":
        result = assess_finding(_finding("CA-Q-TRUNC"), packet=packet)
        checks = {"abstains": result.quality_decision == "abstain", "penalty": result.truncation_penalty == 1}
    elif kind == "clean":
        result = merge_and_rank_findings([], [], packet=packet)
        checks = {"no_findings": result == [], "empty_quality": True}
    elif kind == "invalid_provider":
        result = validate_provider_output(packet, [{"id": "CA-Q-BAD", "file": "src/app.py", "start_line": 1}])
        checks = {"rejected": not result.is_valid, "no_user_finding": result.valid_findings == []}
    elif kind == "stable_ranking":
        args = [_finding("CA-Q-LOW", severity="low", start=3), _finding("CA-Q-HIGH", severity="high", start=4)]
        first = [item.id for item in merge_and_rank_findings(args, [], packet=None)]
        second = [item.id for item in merge_and_rank_findings(list(reversed(args)), [], packet=None)]
        checks = {"stable": first == second == ["CA-Q-HIGH", "CA-Q-LOW"]}
    elif kind == "feedback":
        with tempfile.TemporaryDirectory(prefix="codeatlas-quality-feedback-") as directory:
            manager = ReviewStateManager()
            record = ReviewRunRecord("quality-feedback", ReviewCreateRequest(repo=directory), Path(directory))
            record.findings = [_finding("CA-Q-FEEDBACK").model_dump(mode="json")]
            manager._reviews[record.run_id] = record
            first = manager.set_finding_feedback(record.run_id, "CA-Q-FEEDBACK", "false_positive")
            second = manager.set_finding_feedback(record.run_id, "CA-Q-FEEDBACK", "useful")
            checks = {"persisted": first.feedback == "false_positive", "reversible": second.reversed and second.previous_feedback == "false_positive", "policy_unchanged": record.policy_decision == "unknown"}
    elif kind == "redaction":
        finding = assess_finding(_finding("CA-Q-REDACT"), packet=packet)
        outputs = [render_json([finding]), render_markdown([finding]), render_finding_comment(1, "a" * 40, "quality", finding.model_dump(mode="json"))]
        checks = {"bounded": all(len(value.encode()) < 50_000 for value in outputs), "redacted": all("AKIA" not in value for value in outputs), "packet_audit": audit_packet_redaction(finding.model_dump(mode="json")).redacted}
    else:
        checks = {"known_case": False}
    return {"case_id": case["case_id"], "checks": checks, "pass": all(checks.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "quality" / "cases.json")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    results = [evaluate_case(case) for case in json.loads(args.cases.read_text(encoding="utf-8"))]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("".join(json.dumps(result, sort_keys=True) + "\n" for result in results), encoding="utf-8")
    for result in results:
        print(json.dumps(result, sort_keys=True))
    passed = sum(result["pass"] for result in results)
    print(f"Phase 11B: {passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
