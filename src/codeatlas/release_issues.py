"""Stable, metadata-only product-issue aggregation for release evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.beta_incidents import BetaIncidentReport
from codeatlas.beta_session import BetaSession
from codeatlas.orchestrator.repair_models import repair_payload_is_safe
from codeatlas.release_evidence import ProductIssueCode

IssueEvidenceBasis = Literal["measured", "contract_only", "unavailable", "not_run", "blocked"]
ISSUE_CODES: tuple[ProductIssueCode, ...] = (
    "provider_unavailable",
    "provider_failure",
    "timeout",
    "cancellation",
    "stale_run",
    "recovery_failure",
    "orphan_worktree",
    "extension_failure",
    "unsupported_tool",
    "storage_failure",
    "unsafe_patch_rejected",
    "stale_proposal",
    "unauthorized_apply",
    "artifact_verification_failure",
    "missing_required_evidence",
    "cross_repository_scope_failure",
    "approval_binding_failure",
    "network_boundary_failure",
    "credential_boundary_failure",
    "rollback_failure",
    "cleanup_failure",
)

_SESSION_CODE_MAP: dict[str, ProductIssueCode] = {
    "provider_unavailable": "provider_unavailable",
    "provider_failed": "provider_failure",
    "operation_timeout": "timeout",
    "session_timeout": "timeout",
    "session_cancelled": "cancellation",
    "session_scope_mismatch": "stale_run",
    "cleanup_failed": "recovery_failure",
    "revert_failed": "recovery_failure",
    "apply_failed": "unsafe_patch_rejected",
    "apply_approval_failed": "unauthorized_apply",
    "validation_failed": "missing_required_evidence",
}
_INCIDENT_CODE_MAP: dict[str, ProductIssueCode] = {
    "provider_unavailable": "provider_unavailable",
    "provider_failed": "provider_failure",
    "service_timeout": "timeout",
    "session_cancelled": "cancellation",
    "stale_review": "stale_run",
    "recovery_failed": "recovery_failure",
    "orphan_worktree": "orphan_worktree",
    "extension_failed": "extension_failure",
    "extension_disconnected": "extension_failure",
    "tools_unavailable": "unsupported_tool",
    "database_unavailable": "storage_failure",
    "database_corrupted": "storage_failure",
    "database_migration_failed": "storage_failure",
    "unsafe_patch_rejected": "unsafe_patch_rejected",
    "patch_conflict": "unsafe_patch_rejected",
    "pending_recovery": "recovery_failure",
}


class ProductIssueAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    issue_code: ProductIssueCode
    count: int = Field(ge=0, le=100_000)
    session_count: int = Field(ge=0, le=50)
    incident_count: int = Field(ge=0, le=25)
    evidence_basis: IssueEvidenceBasis


class ProductIssueAggregation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    report_version: Literal["product-issue-aggregation-v1"] = "product-issue-aggregation-v1"
    evidence_basis: IssueEvidenceBasis
    session_count: int = Field(ge=0, le=50)
    incident_count: int = Field(ge=0, le=25)
    issues: tuple[ProductIssueAggregate, ...] = Field(min_length=21, max_length=21)
    limitations: tuple[str, ...] = Field(default=(), max_length=10)
    production_ready: Literal[False] = False


def aggregate_product_issues(
    sessions: tuple[BetaSession, ...] = (),
    *,
    incidents: BetaIncidentReport | None = None,
    evidence_basis: IssueEvidenceBasis = "measured",
) -> ProductIssueAggregation:
    """Count only explicit stable codes; absence of a code is not proof of no issue."""
    if len(sessions) > 50:
        raise ValueError("too many sessions for product issue aggregation")
    validated_sessions = tuple(BetaSession.model_validate(item.model_dump()) for item in sessions)
    if len({item.session_id for item in validated_sessions}) != len(validated_sessions):
        raise ValueError("duplicate sessions cannot inflate issue counts")
    validated_incidents = (
        BetaIncidentReport.model_validate(incidents.model_dump()) if incidents is not None else None
    )
    if validated_incidents is not None and len(validated_incidents.incidents) > 25:
        raise ValueError("too many incidents for product issue aggregation")
    session_repositories = {item.repository_identity_hash for item in validated_sessions}
    if validated_incidents is not None and session_repositories and session_repositories != {
        validated_incidents.repository_identity_hash
    }:
        raise ValueError("incident and session repository scopes differ")

    counts: dict[ProductIssueCode, list[int]] = {
        code: [0, 0, 0] for code in ISSUE_CODES
    }
    for session in validated_sessions:
        code = _SESSION_CODE_MAP.get(session.failure_code or "")
        if code is not None:
            counts[code][0] += 1
            counts[code][1] += 1
    if validated_incidents is not None:
        for incident in validated_incidents.incidents:
            code = _INCIDENT_CODE_MAP.get(incident.reason_code)
            if code is not None:
                counts[code][0] += 1
                counts[code][2] += 1

    limitations: tuple[str, ...] = ()
    if not validated_sessions and validated_incidents is None:
        limitations = ("No session or incident inputs were supplied; issue absence is not established.",)
    elif evidence_basis in {"unavailable", "not_run", "blocked"}:
        limitations = ("Issue inputs are not complete execution evidence.",)

    result = ProductIssueAggregation(
        evidence_basis=evidence_basis,
        session_count=len(validated_sessions),
        incident_count=len(validated_incidents.incidents) if validated_incidents is not None else 0,
        issues=tuple(
            ProductIssueAggregate(
                issue_code=code,
                count=values[0],
                session_count=values[1],
                incident_count=values[2],
                evidence_basis=evidence_basis,
            )
            for code, values in counts.items()
        ),
        limitations=limitations,
    )
    if not repair_payload_is_safe(result.model_dump(mode="json")):
        raise ValueError("product issue aggregation failed the existing secret audit")
    return result


__all__ = [
    "ISSUE_CODES",
    "ProductIssueAggregate",
    "ProductIssueAggregation",
    "aggregate_product_issues",
]
