"""Finding merge, precedence, and explainable ranking."""

from __future__ import annotations

from typing import Any, Sequence
from codeatlas.findings.models import Finding

SEVERITY_WEIGHTS = {
    "blocker": 5.0,
    "high": 4.0,
    "medium": 3.0,
    "low": 2.0,
    "info": 1.0,
}

EVIDENCE_WEIGHTS = {
    "reproduced": 5.0,
    "strong": 4.0,
    "supported": 3.0,
    "weak": 2.0,
    "none": 1.0,
}


def compute_finding_rank_score(finding: Finding, is_diff_scoped: bool = True) -> float:
    """Compute an explainable ranking score for a finding."""
    sev_score = SEVERITY_WEIGHTS.get(finding.severity, 1.0)
    ev_score = EVIDENCE_WEIGHTS.get(finding.evidence_strength, 1.0)
    conf_score = finding.confidence

    base = (sev_score * 0.4) + (ev_score * 0.3) + (conf_score * 2.0)
    if is_diff_scoped:
        base += 1.0
    if finding.limitations:
        base -= 0.5 * len(finding.limitations)
    return round(max(base, 0.0), 3)


def _findings_overlap(f1: Finding, f2: Finding) -> bool:
    """Check if two findings represent duplicate reports on the same code."""
    if f1.file.replace("\\", "/") != f2.file.replace("\\", "/"):
        return False
    if f1.category != f2.category:
        return False
    # Line overlap
    return not (f1.end_line < f2.start_line or f1.start_line > f2.end_line)


def merge_and_rank_findings(
    deterministic_findings: Sequence[Finding | dict[str, Any]],
    reviewer_findings: Sequence[Finding | dict[str, Any]],
) -> list[Finding]:
    """Merge deterministic and reviewer findings respecting evidence precedence and rank them."""
    det_list = [f if isinstance(f, Finding) else Finding.model_validate(f) for f in deterministic_findings]
    rev_list = [f if isinstance(f, Finding) else Finding.model_validate(f) for f in reviewer_findings]

    # Tag origins
    for f in det_list:
        if "origin" not in f.provenance:
            f.provenance["origin"] = "deterministic"
    for f in rev_list:
        if "origin" not in f.provenance:
            f.provenance["origin"] = "reviewer"

    merged: list[Finding] = []
    used_rev_indices: set[int] = set()

    for det in det_list:
        matched_rev: Finding | None = None
        for r_idx, rev in enumerate(rev_list):
            if r_idx not in used_rev_indices and _findings_overlap(det, rev):
                matched_rev = rev
                used_rev_indices.add(r_idx)
                break

        if matched_rev:
            # Deterministic evidence takes precedence, merge additional details
            combined_evidence = list(dict.fromkeys(det.evidence + matched_rev.evidence))
            combined_tools = list(dict.fromkeys(det.tools_consulted + matched_rev.tools_consulted))
            combined_tests = list(dict.fromkeys(det.tests_consulted + matched_rev.tests_consulted))
            combined_limitations = list(dict.fromkeys(det.limitations + matched_rev.limitations))

            prov = dict(det.provenance)
            prov["origin"] = "merged"
            prov["reviewer_claim"] = matched_rev.claim
            prov["reviewer_finding_id"] = matched_rev.id

            merged_finding = Finding(
                id=f"CA-MRG-{det.id.replace('CA-', '')}",
                file=det.file,
                start_line=min(det.start_line, matched_rev.start_line),
                end_line=max(det.end_line, matched_rev.end_line),
                severity=det.severity,  # deterministic evidence has precedence
                category=det.category,
                claim=det.claim,
                impact=f"{det.impact}; Reviewer notes: {matched_rev.impact}",
                evidence_strength=det.evidence_strength,  # do not upgrade without evidence
                confidence=max(det.confidence, matched_rev.confidence),
                evidence=combined_evidence,
                tools_consulted=combined_tools,
                tests_consulted=combined_tests,
                fixability=det.fixability,
                status=det.status,
                limitations=combined_limitations,
                abstention_reason=det.abstention_reason or matched_rev.abstention_reason,
                provenance=prov,
            )
            merged.append(merged_finding)
        else:
            merged.append(det)

    # Append remaining non-duplicate reviewer findings
    for r_idx, rev in enumerate(rev_list):
        if r_idx not in used_rev_indices:
            merged.append(rev)

    # Rank descending by explainable rank score, then file, start_line
    merged.sort(key=lambda f: (-compute_finding_rank_score(f), f.file, f.start_line))
    return merged


__all__ = [
    "SEVERITY_WEIGHTS",
    "EVIDENCE_WEIGHTS",
    "compute_finding_rank_score",
    "merge_and_rank_findings",
]
