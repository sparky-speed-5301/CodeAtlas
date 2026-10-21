"""Validate release KPI observations against the versioned metric catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.benchmark.product_validation import load_metric_catalog
from codeatlas.orchestrator.repair_models import repair_payload_is_safe
from codeatlas.release_evidence import (
    EvidenceStatus,
    MetricGroup,
    ReleaseEvidence,
)

MAX_KPI_CATALOG_BYTES = 64_000
KPIValidationStatus = Literal[
    "measured", "contract_only", "unavailable", "not_run", "blocked"
]


class KPIGroupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    group: MetricGroup
    required_metrics: int = Field(ge=1, le=200)
    observed_metrics: int = Field(ge=0, le=200)
    status: KPIValidationStatus
    missing_metrics: tuple[str, ...] = Field(default=(), max_length=200)
    unexpected_metrics: tuple[str, ...] = Field(default=(), max_length=200)


class KPIValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    report_version: Literal["release-kpi-validation-v1"] = "release-kpi-validation-v1"
    status: KPIValidationStatus
    valid: bool
    benchmark_version: str = Field(min_length=1, max_length=64)
    catalog_version: str = Field(min_length=1, max_length=64)
    required_metrics: int = Field(ge=1, le=200)
    observed_metrics: int = Field(ge=0, le=200)
    groups: tuple[KPIGroupResult, ...] = Field(min_length=4, max_length=4)
    missing_metrics: tuple[str, ...] = Field(default=(), max_length=200)
    unexpected_metrics: tuple[str, ...] = Field(default=(), max_length=200)
    limitations: tuple[str, ...] = Field(default=(), max_length=20)
    production_ready: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> KPIValidationReport:
        groups = tuple(result.group for result in self.groups)
        if len(set(groups)) != len(groups):
            raise ValueError("KPI groups must be unique")
        if self.valid and (self.missing_metrics or self.unexpected_metrics):
            raise ValueError("valid KPI reports cannot contain missing or unexpected metrics")
        if self.status in {"unavailable", "not_run", "blocked"} and not self.limitations:
            raise ValueError("unavailable, not-run, and blocked KPI reports require limitations")
        if not repair_payload_is_safe(self.model_dump(mode="json")):
            raise ValueError("KPI validation report contains secret-like material")
        return self


def _catalog(path: str | Path):
    source = Path(path).expanduser().absolute()
    try:
        stat = source.stat()
    except OSError as error:
        raise ValueError("KPI catalog must be an accessible regular file") from error
    if source.is_symlink() or not source.is_file() or stat.st_nlink != 1:
        raise ValueError("KPI catalog must be an unlinked regular file")
    if stat.st_size > MAX_KPI_CATALOG_BYTES:
        raise ValueError("KPI catalog exceeds its fixed bound")
    return load_metric_catalog(source)


def _group_status(statuses: list[EvidenceStatus]) -> KPIValidationStatus:
    if not statuses:
        return "blocked"
    if any(status == "blocked" for status in statuses):
        return "blocked"
    if any(status == "unavailable" for status in statuses):
        return "unavailable"
    if any(status == "not_run" for status in statuses):
        return "not_run"
    if any(status == "measured" for status in statuses):
        return "measured"
    return "contract_only"


def validate_kpi_catalog(
    evidence: ReleaseEvidence,
    catalog_path: str | Path = "eval/product-validation/metrics-v1.json",
) -> KPIValidationReport:
    """Validate all catalog metrics without calculating a composite quality score."""
    catalog = _catalog(catalog_path)
    expected = {metric.name: metric for metric in catalog.metrics}
    observed = {metric.metric_name: metric for metric in evidence.kpis}
    missing = tuple(sorted(set(expected) - set(observed)))
    unexpected = tuple(sorted(set(observed) - set(expected)))
    inconsistent: list[str] = []
    group_results: list[KPIGroupResult] = []
    statuses: list[EvidenceStatus] = []

    for group in ("review_usefulness", "autofix_usefulness", "reliability", "trust_and_safety"):
        required_names = {name for name, metric in expected.items() if metric.group == group}
        observed_names = {name for name, metric in observed.items() if expected.get(name) and expected[name].group == group}
        group_missing = tuple(sorted(required_names - observed_names))
        group_unexpected = tuple(sorted(
            name for name, metric in observed.items()
            if metric.metric_group == group and name not in required_names
        ))
        group_statuses: list[EvidenceStatus] = []
        for name in sorted(required_names & observed_names):
            item = observed[name]
            definition = expected[name]
            if item.metric_group != definition.group or item.observation.unit != definition.unit:
                group_unexpected += (name,)
                inconsistent.append(name)
                continue
            group_statuses.append(item.observation.status)
            statuses.append(item.observation.status)
        status = _group_status(group_statuses) if not group_missing and not group_unexpected else "blocked"
        group_results.append(
            KPIGroupResult(
                group=group,
                required_metrics=len(required_names),
                observed_metrics=len(observed_names),
                status=status,
                missing_metrics=group_missing,
                unexpected_metrics=tuple(sorted(set(group_unexpected))),
            )
        )

    benchmark_match = catalog.benchmark_version == evidence.benchmark.benchmark_version
    if not benchmark_match:
        missing = (*missing, "catalog_benchmark_version")

    unexpected = tuple(sorted(set((*unexpected, *inconsistent))))
    valid = not missing and not unexpected and benchmark_match and all(
        group.status in {"measured", "contract_only"} for group in group_results
    )
    status: KPIValidationStatus
    if not valid:
        status = "blocked"
    elif any(item.observation.status == "measured" for item in evidence.kpis):
        status = "measured"
    else:
        status = "contract_only"
    limitations: tuple[str, ...] = ()
    if not valid:
        limitations = ("KPI evidence is incomplete, inconsistent, or does not match the catalog.",)
    elif status == "contract_only":
        limitations = ("KPI observations are contract-only and do not establish external beta quality.",)

    report = KPIValidationReport(
        status=status,
        valid=valid,
        benchmark_version=evidence.benchmark.benchmark_version,
        catalog_version=catalog.catalog_version,
        required_metrics=len(expected),
        observed_metrics=len(observed),
        groups=tuple(group_results),
        missing_metrics=tuple(sorted(set(missing))),
        unexpected_metrics=tuple(sorted(set(unexpected))),
        limitations=limitations,
    )
    if not repair_payload_is_safe(report.model_dump(mode="json")):
        raise ValueError("KPI validation report failed the existing secret audit")
    return report


__all__ = ["KPIGroupResult", "KPIValidationReport", "validate_kpi_catalog"]
