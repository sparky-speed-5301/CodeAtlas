"""Finding merge, duplicate grouping, and explainable Phase 11B ranking."""

from __future__ import annotations

from typing import Any, Sequence, TYPE_CHECKING

from codeatlas.findings.models import Finding

# Keep the public legacy constants stable.  The actual quality calculation is
# delegated to ``review.quality`` so every output carries identical components.
from codeatlas.review.quality import (
    QUALITY_VERSION,
    assess_finding,
    duplicate_group_id,
    findings_duplicate,
    has_deterministic_support,
    normalize_claim,
    quality_score,
    quality_summary,
    strongest_finding,
)

if TYPE_CHECKING:
    from codeatlas.review.packet import ReviewPacket


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


def compute_finding_rank_score(
    finding: Finding,
    is_diff_scoped: bool = True,
    *,
    packet: ReviewPacket | None = None,
) -> float:
    """Return the stable ranking score while persisting its components."""
    assessed = assess_finding(finding, packet=packet)
    score = assessed.quality_score
    if is_diff_scoped:
        score += 0.01 * assessed.changed_line_support
    return round(score, 4)


def _provider_safe(finding: Finding) -> Finding:
    """Mark provider provenance without allowing provider quality fields to persist."""
    # Provider provenance is an untrusted observation. Preserve only bounded
    # identity metadata; arbitrary provider dictionaries never enter output.
    provenance = {
        "origin": "reviewer",
        "provider": str(finding.provenance.get("provider", "reviewer"))[:64],
        "provider_observation": True,
    }
    # Quality and feedback are backend-owned.  Provider supplied values must
    # not seed score components, duplicate identities, or operator feedback.
    return finding.model_copy(update={
        "provenance": provenance,
        "quality_version": QUALITY_VERSION,
        "evidence_sources": [],
        "deterministic_support": 0.0,
        "reviewer_support": 0.0,
        "changed_line_support": 0.0,
        "repository_context_support": 0.0,
        "test_support": 0.0,
        "ambiguity_score": 0.0,
        "truncation_penalty": 0.0,
        "duplicate_group_id": None,
        "suppressed_finding_ids": [],
        "quality_decision": "review_only",
        "quality_score": 0.0,
        "score_components": {},
        "quality_limitations": [],
        "feedback": None,
    })


def _merge_group(
    group: Sequence[Finding],
    *,
    packet: ReviewPacket | None,
) -> Finding:
    """Merge one deterministic duplicate group, preserving strongest evidence."""
    strongest = strongest_finding(group)
    group = sorted(group, key=lambda item: (item.id != strongest.id, item.id))
    deterministic = [item for item in group if has_deterministic_support(item)]
    reviewer = [item for item in group if not has_deterministic_support(item)]
    claims = {normalize_claim(item.claim) for item in group}
    conflict = bool(deterministic and reviewer and len(claims) > 1)
    all_evidence = list(dict.fromkeys(evidence for item in group for evidence in item.evidence))
    all_tools = list(dict.fromkeys(tool for item in group for tool in item.tools_consulted))
    all_tests = list(dict.fromkeys(test for item in group for test in item.tests_consulted))
    all_limitations = list(dict.fromkeys(limit for item in group for limit in item.limitations))
    all_sources = list(dict.fromkeys(source for item in group for source in item.evidence_sources))
    suppressed = [item.id for item in group if item.id != strongest.id]
    provenance: dict[str, Any] = dict(strongest.provenance)
    if deterministic:
        provenance = dict(deterministic[0].provenance)
    provenance.update(
        {
            "origin": "merged" if len(group) > 1 else provenance.get("origin", "unknown"),
            "sources": sorted({origin for item in group for origin in item.provenance.get("sources", [])} | {
                "deterministic" if has_deterministic_support(item) else "reviewer" for item in group
            }),
            "deterministic_finding_ids": [item.id for item in deterministic][:20],
            "reviewer_finding_ids": [item.id for item in reviewer][:20],
            # Preserve the singular linkage keys consumed by the existing
            # patch-suggestion materializer while also exposing bounded lists.
            "deterministic_finding_id": deterministic[0].id if deterministic else None,
            "reviewer_finding_id": reviewer[0].id if reviewer else None,
        }
    )
    if conflict:
        provenance["quality_conflict"] = True
    merged = strongest.model_copy(
        update={
            "id": (
                f"CA-MRG-{deterministic[0].id.replace('CA-', '')}"
                if deterministic and len(group) > 1
                else strongest.id
            ),
            "start_line": min(item.start_line for item in group),
            "end_line": max(item.end_line for item in group),
            "evidence": all_evidence[:30],
            "tools_consulted": all_tools[:30],
            "tests_consulted": all_tests[:30],
            "limitations": all_limitations[:20],
            "evidence_sources": all_sources[:20],
            "suppressed_finding_ids": suppressed[:50],
            "duplicate_group_id": duplicate_group_id(group) if len(group) > 1 else strongest.duplicate_group_id,
            "provenance": provenance,
        }
    )
    return assess_finding(
        merged,
        packet=packet,
        deterministic_support=1.0 if deterministic else 0.0,
        reviewer_support=1.0 if reviewer else 0.0,
        provider_conflict=conflict,
        extra_reasons=(
            [
                f"merged {len(group)} overlapping observations; suppressed IDs: {', '.join(suppressed[:10])}",
            ]
            if len(group) > 1
            else []
        ),
    )


def merge_and_rank_findings(
    deterministic_findings: Sequence[Finding | dict[str, Any]],
    reviewer_findings: Sequence[Finding | dict[str, Any]],
    *,
    packet: ReviewPacket | None = None,
) -> list[Finding]:
    """Merge, quality-assess, suppress duplicates, and deterministically rank findings."""
    det_list = [
        (f if isinstance(f, Finding) else Finding.model_validate(f)).model_copy(
            update={"provenance": {**(f.provenance if isinstance(f, Finding) else f.get("provenance", {})), "origin": "deterministic"}}
        )
        for f in deterministic_findings
    ]
    rev_list = [_provider_safe(f if isinstance(f, Finding) else Finding.model_validate(f)) for f in reviewer_findings]
    observed = [
        assess_finding(item, packet=packet, deterministic_support=1.0, reviewer_support=0.0)
        for item in det_list
    ] + [
        assess_finding(item, packet=packet, deterministic_support=0.0, reviewer_support=0.45)
        for item in rev_list
    ]

    groups: list[list[Finding]] = []
    for finding in sorted(observed, key=lambda item: (item.file, item.start_line, item.end_line, item.id)):
        matches = [group for group in groups if any(findings_duplicate(finding, existing) for existing in group)]
        if not matches:
            groups.append([finding])
        else:
            # A bridging observation may connect multiple existing groups.
            # Merge all of them so grouping is independent of input order.
            combined = [finding]
            for match in matches:
                combined.extend(match)
                groups.remove(match)
            groups.append(combined)

    merged = [_merge_group(group, packet=packet) for group in groups]
    merged.sort(
        key=lambda finding: (
            -(finding.quality_score + 0.01 * finding.changed_line_support),
            finding.file.replace("\\", "/"),
            finding.start_line,
            finding.id,
        )
    )
    return merged


__all__ = [
    "SEVERITY_WEIGHTS",
    "EVIDENCE_WEIGHTS",
    "compute_finding_rank_score",
    "merge_and_rank_findings",
    "quality_score",
    "quality_summary",
]
