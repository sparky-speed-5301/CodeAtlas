"""Fail-closed limited-release gate over explicitly supplied evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.beta import write_beta_export
from codeatlas.beta_plan import BetaEvidencePlan, load_evidence_plan
from codeatlas.orchestrator.repair_models import repair_payload_is_safe
from codeatlas.release_artifacts import (
    ReleaseArtifactEvidence,
    build_artifact_evidence,
    load_artifact_evidence,
)
from codeatlas.release_evidence import ReleaseEvidence, load_release_evidence
from codeatlas.release_kpi import KPIValidationReport, validate_kpi_catalog

ReleaseGateDecision = Literal[
    "release_candidate", "release_candidate_with_limitations", "blocked"
]
GateStatus = Literal["passed", "failed", "blocked", "unavailable", "not_run"]
GateBasis = Literal["measured", "contract_only", "unavailable", "not_run", "blocked"]

DEFAULT_REQUIRED_DOCUMENTS: tuple[str, ...] = (
    "docs/controlled-beta-evidence-plan.md",
    "docs/controlled-beta-operations.md",
    "docs/controlled-beta-feedback.md",
    "docs/controlled-beta-validation.md",
    "docs/limited-release-operations.md",
)
CRITICAL_ISSUES = frozenset(
    {
        "unsafe_patch_rejected",
        "unauthorized_apply",
        "artifact_verification_failure",
        "missing_required_evidence",
        "cross_repository_scope_failure",
        "approval_binding_failure",
        "network_boundary_failure",
        "credential_boundary_failure",
        "rollback_failure",
        "cleanup_failure",
    }
)


class ReleaseGateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Literal[
        "evidence_plan",
        "release_evidence",
        "kpi_catalog",
        "artifact_identity",
        "required_documentation",
        "rollback_evidence",
        "cleanup_evidence",
        "safety_gates",
    ]
    status: GateStatus
    evidence_basis: GateBasis
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class ReleaseCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    report_version: Literal["limited-release-check-v1"] = "limited-release-check-v1"
    decision: ReleaseGateDecision
    checks: tuple[ReleaseGateCheck, ...] = Field(min_length=8, max_length=8)
    blocking_conditions: tuple[str, ...] = Field(default=(), max_length=20)
    limitations: tuple[str, ...] = Field(default=(), max_length=30)
    production_ready: Literal[False] = False

    def model_post_init(self, __context: object) -> None:
        if not repair_payload_is_safe(self.model_dump(mode="json")):
            raise ValueError("release check contains secret-like material")


def _check(
    name: str,
    status: GateStatus,
    basis: GateBasis,
    reason_code: str,
) -> ReleaseGateCheck:
    return ReleaseGateCheck(
        name=name, status=status, evidence_basis=basis, reason_code=reason_code
    )


def _documentation_check(root: Path, required_documents: tuple[str, ...]) -> ReleaseGateCheck:
    for relative in required_documents:
        path = root / relative
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_nlink != 1
            or not path.read_text(encoding="utf-8").strip()
        ):
            return _check(
                "required_documentation",
                "blocked",
                "blocked",
                "missing_required_documentation",
            )
    return _check("required_documentation", "passed", "measured", "documentation_verified")


def _artifact_check(
    artifact_evidence: ReleaseArtifactEvidence | None,
    *,
    source_root: str | Path | None,
    artifact_directory: str | Path | None,
) -> ReleaseGateCheck:
    if artifact_evidence is None:
        return _check("artifact_identity", "blocked", "blocked", "missing_artifact_evidence")
    if artifact_evidence.status != "passed":
        return _check(
            "artifact_identity",
            "blocked" if artifact_evidence.status in {"blocked", "failed"} else artifact_evidence.status,
            artifact_evidence.status,
            "artifact_verification_failed",
        )
    if source_root is None or artifact_directory is None:
        return _check("artifact_identity", "blocked", "blocked", "independent_artifact_inputs_missing")
    try:
        independent = build_artifact_evidence(
            source_root,
            artifact_directory,
            reproducible=True,
        )
    except (OSError, TypeError, ValueError):
        return _check("artifact_identity", "blocked", "blocked", "artifact_verification_failed")
    if independent.status != "passed":
        return _check("artifact_identity", "blocked", independent.status, "artifact_verification_failed")
    if independent.reproducibility_identity != artifact_evidence.reproducibility_identity:
        return _check("artifact_identity", "blocked", "blocked", "artifact_identity_mismatch")
    return _check("artifact_identity", "passed", "measured", "artifact_identity_verified")


def _recovery_check(
    name: Literal["rollback_evidence", "cleanup_evidence"],
    observations: tuple,
) -> ReleaseGateCheck:
    if not observations:
        return _check(name, "blocked", "blocked", "missing_required_evidence")
    if any(item.result in {"failed", "blocked"} for item in observations):
        return _check(name, "blocked", "blocked", "rollback_or_cleanup_failed")
    if any(item.result in {"unavailable", "not_run"} for item in observations):
        return _check(name, "blocked", "blocked", "rollback_or_cleanup_not_verified")
    basis: GateBasis = "measured" if any(item.observation.status == "measured" for item in observations) else "contract_only"
    return _check(name, "passed", basis, "rollback_or_cleanup_verified")


def _safety_check(evidence: ReleaseEvidence | None) -> ReleaseGateCheck:
    if evidence is None:
        return _check("safety_gates", "blocked", "blocked", "missing_required_evidence")
    if evidence.assessment.critical_safety_failures:
        return _check("safety_gates", "blocked", "blocked", "critical_safety_failure")
    if any(
        item.issue_code in CRITICAL_ISSUES and item.count > 0
        for item in evidence.product_issues
    ):
        return _check("safety_gates", "blocked", "measured", "critical_product_issue")
    if evidence.assessment.blocking_conditions:
        return _check("safety_gates", "blocked", "blocked", "assessment_blocked")
    return _check("safety_gates", "passed", "contract_only", "safety_gates_clear")


def run_release_check(
    *,
    repository_root: str | Path = ".",
    plan_path: str | Path = "config/controlled-beta-evidence-plan.json",
    evidence_path: str | Path | None = None,
    artifact_evidence_path: str | Path | None = None,
    artifact_directory: str | Path | None = None,
    source_root: str | Path | None = None,
    kpi_catalog_path: str | Path = "eval/product-validation/metrics-v1.json",
    required_documents: tuple[str, ...] = DEFAULT_REQUIRED_DOCUMENTS,
) -> ReleaseCheckResult:
    """Run the release gate using only explicitly named files and directories."""
    checks: list[ReleaseGateCheck] = []
    limitations: list[str] = []
    blocking: list[str] = []
    plan: BetaEvidencePlan | None = None
    evidence: ReleaseEvidence | None = None
    artifact_evidence: ReleaseArtifactEvidence | None = None

    try:
        plan = load_evidence_plan(plan_path)
        if plan.status == "configured":
            checks.append(_check("evidence_plan", "passed", "measured", "evidence_plan_configured"))
        else:
            checks.append(_check("evidence_plan", "blocked", "blocked", "evidence_plan_not_configured"))
            blocking.append("evidence_plan_not_configured")
    except (OSError, TypeError, ValueError):
        checks.append(_check("evidence_plan", "blocked", "blocked", "evidence_plan_invalid"))
        blocking.append("evidence_plan_invalid")

    if evidence_path is None:
        checks.append(_check("release_evidence", "blocked", "blocked", "missing_required_evidence"))
        blocking.append("missing_required_evidence")
    else:
        try:
            evidence = load_release_evidence(evidence_path)
            checks.append(_check("release_evidence", "passed", "contract_only", "release_evidence_loaded"))
        except (OSError, TypeError, ValueError):
            checks.append(_check("release_evidence", "blocked", "blocked", "release_evidence_invalid"))
            blocking.append("release_evidence_invalid")

    if evidence is None:
        checks.append(_check("kpi_catalog", "blocked", "blocked", "missing_required_evidence"))
    else:
        try:
            kpi_report: KPIValidationReport = validate_kpi_catalog(evidence, kpi_catalog_path)
            if not kpi_report.valid:
                checks.append(_check("kpi_catalog", "blocked", kpi_report.status, "kpi_catalog_invalid"))
                blocking.append("kpi_catalog_invalid")
            else:
                checks.append(_check("kpi_catalog", "passed", kpi_report.status, "kpi_catalog_verified"))
                if kpi_report.status == "contract_only":
                    limitations.extend(kpi_report.limitations)
        except (OSError, TypeError, ValueError):
            checks.append(_check("kpi_catalog", "blocked", "blocked", "kpi_catalog_unavailable"))
            blocking.append("kpi_catalog_unavailable")

    if artifact_evidence_path is not None:
        try:
            artifact_evidence = load_artifact_evidence(artifact_evidence_path)
        except (OSError, TypeError, ValueError):
            artifact_evidence = None
            blocking.append("artifact_evidence_invalid")
    artifact_check = _artifact_check(
        artifact_evidence,
        source_root=source_root,
        artifact_directory=artifact_directory,
    )
    checks.append(artifact_check)
    if artifact_check.status != "passed":
        blocking.append(artifact_check.reason_code)

    checks.append(_documentation_check(Path(repository_root).absolute(), required_documents))
    if checks[-1].status != "passed":
        blocking.append(checks[-1].reason_code)

    if evidence is None:
        checks.append(_check("rollback_evidence", "blocked", "blocked", "missing_required_evidence"))
        checks.append(_check("cleanup_evidence", "blocked", "blocked", "missing_required_evidence"))
        blocking.extend(("missing_rollback_evidence", "missing_cleanup_evidence"))
    else:
        rollback_check = _recovery_check("rollback_evidence", evidence.rollback_observations)
        cleanup_check = _recovery_check("cleanup_evidence", evidence.cleanup_observations)
        checks.extend((rollback_check, cleanup_check))
        if rollback_check.status != "passed":
            blocking.append(rollback_check.reason_code)
        if cleanup_check.status != "passed":
            blocking.append(cleanup_check.reason_code)

    safety_check = _safety_check(evidence)
    checks.append(safety_check)
    if safety_check.status != "passed":
        blocking.append(safety_check.reason_code)

    unique_blocking = tuple(dict.fromkeys(blocking))
    if unique_blocking:
        decision: ReleaseGateDecision = "blocked"
    else:
        if evidence is not None:
            if any(item.observation.status in {"unavailable", "not_run", "blocked", "contract_only"} for item in evidence.kpis):
                limitations.append("Some KPI populations remain contract-only, unavailable, or not-run.")
            if any(item.observation.status in {"unavailable", "not_run", "blocked", "contract_only"} for item in evidence.provider_mode_coverage):
                limitations.append("Provider-mode coverage includes unavailable, blocked, or not-run modes.")
        decision = "release_candidate_with_limitations" if limitations else "release_candidate"

    result = ReleaseCheckResult(
        decision=decision,
        checks=tuple(checks),
        blocking_conditions=unique_blocking,
        limitations=tuple(dict.fromkeys(limitations)),
    )
    return result


def write_release_check(result: ReleaseCheckResult, destination: str | Path) -> None:
    write_beta_export(result, destination)


__all__ = [
    "DEFAULT_REQUIRED_DOCUMENTS",
    "ReleaseCheckResult",
    "ReleaseGateCheck",
    "run_release_check",
    "write_release_check",
]
