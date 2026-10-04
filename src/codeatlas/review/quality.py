"""Deterministic, explainable finding-quality assessment for Phase 11B.

This module is intentionally pure: provider text is treated as an observation,
never as policy or trust.  All returned values are bounded, deterministic, and
safe to serialize into existing finding/manifest/artifact contracts.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, TYPE_CHECKING

from codeatlas.findings.models import Finding

if TYPE_CHECKING:
    from codeatlas.review.packet import ReviewPacket


QUALITY_VERSION = "11B.1"
QUALITY_DECISIONS = {
    "report",
    "report_with_uncertainty",
    "review_only",
    "abstain",
    "suppress_duplicate",
    "suppress_low_evidence",
}

_SEVERITY_SCORE = {"info": 0.20, "low": 0.40, "medium": 0.60, "high": 0.82, "blocker": 1.0}
_EVIDENCE_SCORE = {"none": 0.0, "weak": 0.25, "supported": 0.60, "strong": 0.82, "reproduced": 1.0}
_SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "blocker": 4}


def clamp01(value: Any, default: float = 0.0) -> float:
    """Convert a value to a finite number in the inclusive [0, 1] range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in {float("inf"), float("-inf")}:
        return default
    return round(max(0.0, min(1.0, number)), 4)


def normalize_category(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def normalize_claim(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower())).strip()


def _origins(finding: Finding | Mapping[str, Any]) -> set[str]:
    provenance = finding.provenance if isinstance(finding, Finding) else finding.get("provenance", {})
    if not isinstance(provenance, Mapping):
        provenance = {}
    values = {str(provenance.get("origin", ""))}
    sources = provenance.get("sources", [])
    if isinstance(sources, list):
        values.update(str(item) for item in sources)
    if provenance.get("provider") or provenance.get("reviewer_finding_id"):
        values.add("reviewer")
    if provenance.get("analyzer") or provenance.get("deterministic_finding_id"):
        values.add("deterministic")
    return values


def has_deterministic_support(finding: Finding | Mapping[str, Any]) -> bool:
    origins = _origins(finding)
    return bool(origins & {"deterministic", "analyzer"})


def has_reviewer_support(finding: Finding | Mapping[str, Any]) -> bool:
    origins = _origins(finding)
    return bool(origins & {"reviewer", "provider"})


def _line_support(finding: Finding, packet: ReviewPacket | None) -> tuple[float, str | None]:
    if finding.start_line < 1 or finding.end_line < finding.start_line or not finding.file:
        return 0.0, "file or location is invalid"
    if packet is None:
        return clamp01(finding.provenance.get("changed_line_support", 1.0)), None
    ranges = packet.changed_line_ranges.get(finding.file.replace("\\", "/"), [])
    if not ranges:
        if finding.provenance.get("context_only") is True:
            return 0.0, "finding is not mapped to a changed line"
        return 0.0, "required changed-line mapping is unavailable"
    overlap = any(not (finding.end_line < start or finding.start_line > end) for start, end in ranges)
    return (1.0 if overlap else 0.0), None if overlap else "line mapping is ambiguous or outside the changed range"


def _context_support(finding: Finding, packet: ReviewPacket | None) -> float:
    if packet is None:
        return clamp01(finding.provenance.get("repository_context_support", 0.25), 0.25)
    file = finding.file.replace("\\", "/")
    candidates = [item for item in packet.context_candidates if item.file == file]
    if candidates:
        return 1.0 if any(item.truncation_status == "full" for item in candidates) else 0.35
    if any(symbol.get("file") == file for symbol in packet.changed_symbols):
        return 0.75
    return 0.0


def _test_support(finding: Finding, packet: ReviewPacket | None) -> float:
    if finding.tests_consulted:
        return 1.0
    if packet is not None and packet.relevant_tests:
        return 0.25
    return 0.0


def quality_components(
    finding: Finding,
    *,
    packet: ReviewPacket | None = None,
    deterministic_support: float | None = None,
    reviewer_support: float | None = None,
    provider_conflict: bool = False,
    duplicate_status: str = "none",
) -> dict[str, float]:
    """Calculate every bounded score component used by the quality decision."""
    changed, _ = _line_support(finding, packet)
    deterministic = (
        clamp01(deterministic_support)
        if deterministic_support is not None
        else (1.0 if has_deterministic_support(finding) else 0.0)
    )
    reviewer = (
        clamp01(reviewer_support)
        if reviewer_support is not None
        else (0.45 if has_reviewer_support(finding) and not deterministic else 0.0)
    )
    context = _context_support(finding, packet)
    tests = _test_support(finding, packet)
    evidence = _EVIDENCE_SCORE.get(finding.evidence_strength, 0.0)
    ambiguity = clamp01(
        1.0
        if provider_conflict or finding.provenance.get("ambiguous_location") or finding.provenance.get("unsupported_flow")
        else finding.provenance.get("ambiguity_score", 0.0)
    )
    truncation = clamp01(
        1.0
        if packet is not None and packet.truncated
        else finding.provenance.get("truncation_penalty", 0.0)
    )
    duplicate = 1.0 if duplicate_status == "suppressed" else 0.0
    severity = _SEVERITY_SCORE.get(finding.severity, 0.2)
    return {
        "severity": severity,
        "changed_line_support": changed,
        "deterministic_support": deterministic,
        "repository_context_support": context,
        "test_support": tests,
        "reviewer_support": reviewer,
        "evidence_strength": evidence,
        "ambiguity_penalty": ambiguity,
        "truncation_penalty": truncation,
        "duplicate_penalty": duplicate,
    }


def quality_score(components: Mapping[str, Any]) -> float:
    """Return the deterministic quality score from persisted components."""
    score = (
        0.14 * clamp01(components.get("severity"))
        + 0.18 * clamp01(components.get("changed_line_support"))
        + 0.20 * clamp01(components.get("deterministic_support"))
        + 0.12 * clamp01(components.get("repository_context_support"))
        + 0.08 * clamp01(components.get("test_support"))
        + 0.08 * clamp01(components.get("reviewer_support"))
        + 0.20 * clamp01(components.get("evidence_strength"))
        - 0.22 * clamp01(components.get("ambiguity_penalty"))
        - 0.18 * clamp01(components.get("truncation_penalty"))
        - 0.30 * clamp01(components.get("duplicate_penalty"))
    )
    return round(clamp01(score), 4)


def explain_quality(
    finding: Finding,
    components: Mapping[str, Any],
    *,
    decision: str,
    reasons: Sequence[str] = (),
) -> str:
    """Produce a bounded, human-readable explanation of the score."""
    observed = (
        f"severity={clamp01(components.get('severity')):.2f}, "
        f"changed-line={clamp01(components.get('changed_line_support')):.2f}, "
        f"deterministic={clamp01(components.get('deterministic_support')):.2f}, "
        f"context={clamp01(components.get('repository_context_support')):.2f}, "
        f"tests={clamp01(components.get('test_support')):.2f}, "
        f"reviewer={clamp01(components.get('reviewer_support')):.2f}, "
        f"evidence={clamp01(components.get('evidence_strength')):.2f}, "
        f"ambiguity={clamp01(components.get('ambiguity_penalty')):.2f}, "
        f"truncation={clamp01(components.get('truncation_penalty')):.2f}"
    )
    reason_text = "; ".join(str(reason) for reason in reasons if reason)
    suffix = f" {reason_text}" if reason_text else ""
    return f"Quality decision '{decision}' for {finding.id}: {observed}.{suffix}"[:2000]


def assess_finding(
    finding: Finding,
    *,
    packet: ReviewPacket | None = None,
    deterministic_support: float | None = None,
    reviewer_support: float | None = None,
    provider_conflict: bool = False,
    duplicate_status: str = "none",
    forced_decision: str | None = None,
    extra_reasons: Sequence[str] = (),
) -> Finding:
    """Attach quality metadata without trusting provider-supplied quality fields."""
    provider_conflict = provider_conflict or bool(finding.provenance.get("quality_conflict"))
    components = quality_components(
        finding,
        packet=packet,
        deterministic_support=deterministic_support,
        reviewer_support=reviewer_support,
        provider_conflict=provider_conflict,
        duplicate_status=duplicate_status,
    )
    score = quality_score(components)
    reasons = list(extra_reasons)
    _, line_reason = _line_support(finding, packet)
    if line_reason:
        reasons.append(line_reason)
    if provider_conflict:
        reasons.append("deterministic and provider observations conflict")
    if components["truncation_penalty"] > 0:
        reasons.append("required context was truncated")
    unsupported = bool(finding.unsupported_flow or finding.provenance.get("unsupported_flow"))
    if unsupported:
        reasons.append("flow is unsupported by the bounded analyzer")

    if forced_decision:
        decision = forced_decision
    elif duplicate_status == "suppressed":
        decision = "suppress_duplicate"
    elif finding.status == "abstained" or unsupported or provider_conflict or line_reason or components["truncation_penalty"] >= 1.0:
        decision = "abstain"
        if finding.status == "abstained" and finding.abstention_reason:
            reasons.append(finding.abstention_reason[:500])
    elif components["evidence_strength"] <= 0.0 or score < 0.30:
        decision = "suppress_low_evidence"
    elif not has_deterministic_support(finding):
        decision = "report_with_uncertainty" if score >= 0.30 else "review_only"
        reasons.append("provider observation is not independently supported")
    elif score >= 0.68 and finding.confidence >= 0.70:
        decision = "report"
    elif score >= 0.38:
        decision = "report_with_uncertainty"
    else:
        decision = "review_only"

    if decision not in QUALITY_DECISIONS:
        decision = "review_only"
    confidence = clamp01(finding.confidence, 0.0)
    if not has_deterministic_support(finding):
        confidence = min(confidence, 0.65)
    if components["evidence_strength"] <= 0.25:
        confidence = min(confidence, 0.55)
    if provider_conflict or unsupported or components["truncation_penalty"]:
        confidence = min(confidence, 0.60)
    if decision in {"abstain", "suppress_low_evidence", "suppress_duplicate"}:
        confidence = min(confidence, 0.50)

    limitations = list(dict.fromkeys([*finding.quality_limitations, *reasons]))[:20]
    abstention = None
    if decision == "abstain":
        abstention = (
            f"Observed: {finding.claim[:300]}. Uncertain: {('; '.join(reasons) or 'quality evidence is incomplete')[:500]}. "
            "Missing or conflicting support prevents a reliable report; human review is recommended."
        )
    elif finding.abstention_reason and decision != "report":
        abstention = finding.abstention_reason

    evidence_sources = list(dict.fromkeys([*finding.evidence_sources, *finding.tools_consulted]))[:20]
    provenance = dict(finding.provenance)
    # Keep the bounded pre-quality observation available to the deterministic
    # policy gate.  The provider cannot use this field to authorize anything;
    # it only prevents the quality display clamp from changing the legacy
    # policy contract after provider results are merged.
    provenance.setdefault("observed_confidence", clamp01(finding.confidence))
    provenance["quality_version"] = QUALITY_VERSION
    provenance["quality_explanation"] = explain_quality(finding, components, decision=decision, reasons=reasons)
    return finding.model_copy(
        update={
            "quality_version": QUALITY_VERSION,
            "confidence": confidence,
            "evidence_sources": evidence_sources,
            "deterministic_support": components["deterministic_support"],
            "reviewer_support": components["reviewer_support"],
            "changed_line_support": components["changed_line_support"],
            "repository_context_support": components["repository_context_support"],
            "test_support": components["test_support"],
            "ambiguity_score": components["ambiguity_penalty"],
            "truncation_penalty": components["truncation_penalty"],
            "unsupported_flow": unsupported,
            "abstention_reason": abstention,
            "quality_decision": decision,
            "quality_score": score,
            "status": "abstained" if decision == "abstain" else (
                "review_only" if decision == "suppress_low_evidence" else finding.status
            ),
            "score_components": dict(components),
            "quality_limitations": limitations,
            "provenance": provenance,
        }
    )


def duplicate_group_id(findings: Sequence[Finding]) -> str:
    """Return a stable ID for a deterministic duplicate group."""
    identity = "|".join(
        sorted(
            f"{f.file.replace('\\', '/')}:" f"{f.start_line}-{f.end_line}:" f"{normalize_category(f.category)}:{normalize_claim(f.claim)}"
            for f in findings
        )
    )
    return "dq-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def findings_duplicate(a: Finding, b: Finding) -> bool:
    """Apply the deterministic duplicate relation used by ranking."""
    if a.file.replace("\\", "/") != b.file.replace("\\", "/"):
        return False
    if normalize_category(a.category) != normalize_category(b.category):
        return False
    if a.end_line < b.start_line or b.end_line < a.start_line:
        return False
    claims_match = normalize_claim(a.claim) == normalize_claim(b.claim)
    shared_evidence = bool(set(a.evidence) & set(b.evidence))
    origins_differ = has_deterministic_support(a) != has_deterministic_support(b)
    return claims_match or shared_evidence or origins_differ


def strongest_finding(findings: Sequence[Finding]) -> Finding:
    """Choose the strongest finding with deterministic evidence taking precedence."""
    return max(
        findings,
        key=lambda f: (
            1 if has_deterministic_support(f) else 0,
            f.quality_score,
            _EVIDENCE_SCORE.get(f.evidence_strength, 0.0),
            _SEVERITY_ORDER.get(f.severity, 0),
            clamp01(f.confidence),
            f.id,
        ),
    )


def quality_summary(findings: Sequence[Finding]) -> dict[str, Any]:
    """Return bounded aggregate quality metrics for manifests and packets."""
    counts = {decision: 0 for decision in sorted(QUALITY_DECISIONS)}
    scores = []
    for finding in findings:
        counts[finding.quality_decision] = counts.get(finding.quality_decision, 0) + 1
        scores.append(finding.quality_score)
    return {
        "quality_version": QUALITY_VERSION,
        "finding_count": len(findings),
        "decision_counts": counts,
        "mean_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "abstention_count": counts.get("abstain", 0),
        "suppressed_duplicate_count": sum(len(f.suppressed_finding_ids) for f in findings),
        "limitations": list(dict.fromkeys(lim for f in findings for lim in f.quality_limitations))[:20],
    }


__all__ = [
    "QUALITY_VERSION",
    "QUALITY_DECISIONS",
    "assess_finding",
    "clamp01",
    "duplicate_group_id",
    "explain_quality",
    "findings_duplicate",
    "has_deterministic_support",
    "has_reviewer_support",
    "normalize_category",
    "normalize_claim",
    "quality_components",
    "quality_score",
    "quality_summary",
    "strongest_finding",
]
