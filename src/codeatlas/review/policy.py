"""Deterministic policy engine for review gating and approval requirements."""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence
from pydantic import BaseModel, ConfigDict, Field

from codeatlas.findings.models import Finding
from codeatlas.review.packet import ReviewPacket


class PolicyDecision(BaseModel):
    """The outcome of evaluating policy rules against a review packet and findings."""

    model_config = ConfigDict(extra="ignore")

    allowed: bool = True
    decision: Literal["allowed", "review_only", "blocked", "abstain", "requires_human_approval"] = "allowed"
    reasons: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    human_approval_required: bool = False
    policy_name: str = "default_safety_policy"


def evaluate_policy(
    packet: ReviewPacket,
    findings: Sequence[Finding | dict[str, Any]],
    *,
    config: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Evaluate explicit safety and gating rules deterministically."""
    cfg = dict(config or {})
    pol_cfg = cfg.get("policy", {}) if isinstance(cfg.get("policy", {}), dict) else {}

    min_confidence = float(pol_cfg.get("min_inline_confidence", 0.80))
    require_evidence_for_high = bool(pol_cfg.get("require_evidence_for_high", True))
    require_human_for_blocker = bool(pol_cfg.get("require_human_for_blocker", True))
    require_human_for_security = bool(pol_cfg.get("require_human_for_security", True))
    abstain_on_truncated = bool(pol_cfg.get("abstain_on_truncated_changed_code", True))
    allow_unverified = bool(pol_cfg.get("allow_unverified_findings", False))

    reasons: list[str] = []
    blocking_reasons: list[str] = []
    needs_human = False

    # Rule 1: Redaction failure is a fatal blocker
    if not packet.redaction_status.redacted or packet.redaction_status.failed_checks:
        msg = f"Redaction validation failed with {packet.redaction_status.raw_value_matches} raw match(es)"
        blocking_reasons.append(msg)
        reasons.append(msg)

    # Rule 2: Abstain on truncated changed code if required by policy
    if abstain_on_truncated:
        has_truncated_changed_code = any(
            c.source_type in {"changed_code", "symbol"} and c.truncation_status != "full"
            for c in packet.context_candidates
        )
        if has_truncated_changed_code:
            msg = "Changed code was truncated due to budget constraints; policy mandates abstention"
            reasons.append(msg)
            return PolicyDecision(
                allowed=True,
                decision="abstain",
                reasons=reasons,
                blocking_reasons=[],
                human_approval_required=False,
            )

    # Evaluate findings
    findings_list = [f if isinstance(f, Finding) else Finding.model_validate(f) for f in findings]

    for f in findings_list:
        # Rule 3: Blocker severity requires human approval
        if f.severity == "blocker" and require_human_for_blocker:
            needs_human = True
            reasons.append(f"Finding {f.id} has blocker severity; requires human review")

        # Rule 4: High severity security requires human approval
        if f.severity == "high" and require_human_for_security:
            needs_human = True
            reasons.append(f"Finding {f.id} has high security severity; requires human review")

        # Rule 5: High severity requires supported evidence
        if f.severity in {"high", "blocker"} and require_evidence_for_high:
            if f.evidence_strength in {"none", "weak"}:
                msg = f"Finding {f.id} lacks supported evidence for high/blocker severity ({f.evidence_strength})"
                reasons.append(msg)
                # Demote or flag
                needs_human = True

        # Rule 6: Confidence thresholding
        if f.confidence < min_confidence:
            reasons.append(
                f"Finding {f.id} confidence {f.confidence:.2f} below threshold {min_confidence:.2f}; marked review-only"
            )

        # Rule 7: Prevent unsupported validated claims
        if f.fixability == "validated" and not allow_unverified:
            reasons.append(f"Finding {f.id} claims validated fix without an isolated verification run")

    # Rule 8: Changes to sensitive files (workflows, credentials, permissions) require human review
    for path in packet.changed_files:
        norm = path.replace("\\", "/").lower()
        if norm.startswith(".github/workflows/") or norm.endswith((".pem", ".key", ".pfx")):
            needs_human = True
            reasons.append(f"Sensitive configuration or workflow change in {path}; requires human approval")

    # Determine final decision state
    if blocking_reasons:
        return PolicyDecision(
            allowed=False,
            decision="blocked",
            reasons=reasons,
            blocking_reasons=blocking_reasons,
            human_approval_required=needs_human,
        )

    if needs_human:
        return PolicyDecision(
            allowed=True,
            decision="requires_human_approval",
            reasons=reasons,
            blocking_reasons=[],
            human_approval_required=True,
        )

    if any("review-only" in r for r in reasons):
        return PolicyDecision(
            allowed=True,
            decision="review_only",
            reasons=reasons,
            blocking_reasons=[],
            human_approval_required=False,
        )

    reasons.append("All policy checks passed")
    return PolicyDecision(
        allowed=True,
        decision="allowed",
        reasons=reasons,
        blocking_reasons=[],
        human_approval_required=False,
    )


__all__ = ["PolicyDecision", "evaluate_policy"]
