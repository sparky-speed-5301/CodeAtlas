"""Read-only diagnostics projected from the public, loopback-only v1 API."""

from __future__ import annotations

import math
import time
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codeatlas import __version__
from codeatlas.client import ClientError, CodeAtlasClient
from codeatlas.service import models as m

MAX_DIAGNOSTIC_SECONDS = 10.0
MAX_DIAGNOSTIC_REVIEWS = 50
MAX_COUNT = 2_147_483_647

CheckName = Literal[
    "health", "version", "database", "migration", "lock", "recovery", "jobs",
    "queue", "workers", "limits", "provider", "github", "artifacts", "retention",
    "limitations",
]
CheckStatus = Literal["ok", "warning", "error", "unknown", "not_applicable"]
MetricName = Literal[
    "active_reviews", "total_reviews", "sampled_reviews", "sample_limit",
    "completed", "failed", "cancelled", "expired", "pending_recovery",
    "recovery_failed", "recovered", "not_required",
]


class DiagnosticCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: CheckName
    status: CheckStatus
    code: str = Field(max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    detail: str = Field(max_length=256)
    metrics: dict[MetricName, Annotated[int, Field(ge=0, le=MAX_COUNT)]] = Field(default_factory=dict, max_length=16)


class ServiceDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["ok", "warning", "error"]
    version: str = Field(max_length=64)
    service_required: Literal[True] = True
    checks: list[DiagnosticCheck] = Field(min_length=15, max_length=15)
    limitations: list[Annotated[str, Field(max_length=256)]] = Field(max_length=8)


_ERROR_CODES = {
    "invalid_service_url": "Service URL must be a loopback HTTP origin.",
    "invalid_timeout": "Timeout must be a positive finite number.",
    "unreachable": "The local service is unreachable.",
    "timeout": "The diagnostic request timed out.",
    "incompatible_api": "The service does not advertise API v1.",
    "invalid_response": "The response does not match the bounded API contract.",
    "response_too_large": "The response exceeds the client size limit.",
    "http_error": "The service rejected the diagnostic request.",
    "diagnostic_deadline": "The diagnostic request budget was exhausted.",
}
_LIMITATIONS = [
    "Only public read-only v1 endpoints are queried; the service database is never opened by the CLI.",
    "Job and recovery counts describe at most the newest 50 reviews, not the entire service history.",
    "Health is not evidence of database integrity, migration validity, lock ownership, or provider readiness.",
    "Queue depth, worker capacity, configured limits, artifacts, and retention settings are not exposed by v1.",
    "Providers, GitHub credentials, repository scripts, tests, and workspace mutations are never exercised.",
    "Requests are sequential, never retried, and use a shared timeout budget capped at ten seconds; this is not a hard wall-clock deadline.",
]
Model = TypeVar("Model", bound=BaseModel)


def _check(name: CheckName, status: CheckStatus, code: str, detail: str,
           **metrics: int) -> DiagnosticCheck:
    return DiagnosticCheck.model_validate({
        "name": name, "status": status, "code": code, "detail": detail, "metrics": metrics,
    })


def _error(name: CheckName, error: ClientError) -> DiagnosticCheck:
    # Even syntactically valid server error codes can contain sensitive input.
    code = error.code if error.code in _ERROR_CODES else "http_error"
    return _check(name, "error", code, _ERROR_CODES[code])


def collect_service_diagnostics(service_url: str, *, timeout: float = 30.0) -> ServiceDiagnosticsResponse:
    """Collect a fixed-size, redacted report without mutations or local DB access."""
    checks: dict[CheckName, DiagnosticCheck] = {}
    health: m.HealthResponse | None = None
    capabilities: m.CapabilitiesResponse | None = None
    reviews: m.ReviewsListResponse | None = None
    try:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ClientError("invalid_timeout", _ERROR_CODES["invalid_timeout"])
        budget = min(timeout, MAX_DIAGNOSTIC_SECONDS)
        api = CodeAtlasClient(service_url, timeout=budget)
        deadline = time.monotonic() + budget

        def fetch(model: type[Model], endpoint: str) -> Model:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError("diagnostic_deadline", _ERROR_CODES["diagnostic_deadline"])
            raw = api.request("GET", endpoint, timeout=remaining)
            if model is m.ReviewsListResponse:
                rows = raw.get("reviews")
                if not isinstance(rows, list) or len(rows) > MAX_DIAGNOSTIC_REVIEWS:
                    raise ClientError("invalid_response", _ERROR_CODES["invalid_response"])
            try:
                return model.model_validate(raw, strict=True)
            except ValidationError:
                raise ClientError("invalid_response", _ERROR_CODES["invalid_response"]) from None

        health = fetch(m.HealthResponse, "/health")
        if not 0 <= health.active_reviews <= MAX_COUNT:
            raise ClientError("invalid_response", _ERROR_CODES["invalid_response"])
        healthy = health.status == "ok" and health.service == "codeatlas-service"
        checks["health"] = _check(
            "health", "ok" if healthy else "error", "healthy" if healthy else "unhealthy",
            "The service reports healthy." if healthy else "The service did not report healthy.",
            active_reviews=health.active_reviews,
        )
    except ClientError as error:
        health = None
        checks["health"] = _error("health", error)

    if health is not None:
        same_version = health.version == __version__
        checks["version"] = _check(
            "version", "ok" if same_version else "warning",
            "version_match" if same_version else "version_mismatch",
            "API v1 and the installed package version match." if same_version
            else "API v1 is compatible, but the service package version differs.",
        )
        try:
            capabilities = fetch(m.CapabilitiesResponse, "/capabilities")
        except ClientError as error:
            checks["database"] = _error("database", error)
        try:
            reviews = fetch(m.ReviewsListResponse, f"/reviews?limit={MAX_DIAGNOSTIC_REVIEWS}&offset=0")
            if (not 0 <= reviews.total <= MAX_COUNT or reviews.limit != MAX_DIAGNOSTIC_REVIEWS
                    or reviews.offset != 0 or len(reviews.reviews) > reviews.total):
                raise ClientError("invalid_response", _ERROR_CODES["invalid_response"])
        except ClientError as error:
            reviews = None
            checks["jobs"] = _error("jobs", error)
            checks["recovery"] = _check("recovery", "unknown", "jobs_unavailable", "Recovery counts are unavailable.")

    if capabilities is not None:
        persistent = capabilities.state_lifetime == "persistent"
        checks["database"] = _check(
            "database", "unknown" if persistent else "not_applicable",
            "persistent_integrity_unknown" if persistent else "process_state",
            "Persistent storage is reported; integrity is not exposed by v1." if persistent
            else "The service reports process-local state, not persistent storage.",
        )
        for name in ("migration", "lock", "retention"):
            checks[name] = _check(
                name, "unknown" if persistent else "not_applicable",
                "not_exposed" if persistent else "process_state",
                "The configured persistent storage state is not exposed by v1." if persistent
                else "Persistent storage is not configured according to the service.",
            )

    if reviews is not None:
        states = {state: sum(row.status == state for row in reviews.reviews)
                  for state in ("completed", "failed", "cancelled", "expired")}
        recovery = {state: sum(row.recovery_state == state for row in reviews.reviews)
                    for state in ("not_required", "pending_recovery", "recovered", "recovery_failed", "expired")}
        failed = recovery["recovery_failed"] or recovery["pending_recovery"] or any(
            row.status in {"pending_recovery", "recovery_failed"} for row in reviews.reviews)
        checks["recovery"] = _check(
            "recovery", "warning" if failed else "ok",
            "sample_recovery_attention" if failed else "sample_recovery_clear",
            "Recovery counts cover only the bounded review sample; recovered fixes remain historical.",
            **recovery,
        )
        sampled = len(reviews.reviews) < reviews.total
        checks["jobs"] = _check(
            "jobs", "warning" if sampled else "ok", "jobs_sampled" if sampled else "jobs_listed",
            "Read-only counts from the newest reviews; no jobs were started, retried, or cancelled.",
            total_reviews=reviews.total, sampled_reviews=len(reviews.reviews),
            sample_limit=MAX_DIAGNOSTIC_REVIEWS, **states,
        )

    if health is not None:
        checks["provider"] = _check(
            "provider", "unknown", "provider_readiness_unknown",
            f"Reported review provider mode: {health.provider}. Configuration and readiness are not exposed by v1.",
        )
    details: dict[CheckName, str] = {
        "version": "Service version could not be checked.",
        "database": "Storage lifetime and integrity could not be checked.",
        "migration": "Database migration state is not exposed by v1.",
        "lock": "Database lock ownership is not exposed by v1; no lock is acquired by the CLI.",
        "recovery": "Recovery counts could not be checked.",
        "jobs": "Job counts could not be checked.",
        "queue": "Queue depth is not exposed by v1; active reviews are not queue depth.",
        "workers": "Worker count and capacity are not exposed by v1.",
        "limits": "Configured service limits are not exposed by v1; documented defaults are not observed settings.",
        "provider": "Provider configuration and readiness could not be checked.",
        "github": "GitHub configuration and readiness are not exposed by v1; no credentials or transport are probed.",
        "artifacts": "Artifact availability, size, and integrity are not exposed by v1; no artifacts are fetched.",
        "retention": "Configured retention settings are not exposed by v1.",
        "limitations": "Diagnostics are bounded observations, not validation or repair execution evidence.",
    }
    for name, detail in details.items():
        checks.setdefault(name, _check(name, "unknown", "not_exposed", detail))
    order: list[CheckName] = [
        "health", "version", "database", "migration", "lock", "recovery", "jobs",
        "queue", "workers", "limits", "provider", "github", "artifacts", "retention", "limitations",
    ]
    status: Literal["ok", "warning", "error"] = (
        "error" if any(check.status == "error" for check in checks.values()) else "warning"
    )
    return ServiceDiagnosticsResponse(
        status=status, version=__version__, checks=[checks[name] for name in order],
        limitations=list(_LIMITATIONS),
    )
