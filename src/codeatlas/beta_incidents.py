"""Repository-scoped, metadata-only beta incidents with bounded JSON storage."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.beta import MAX_BETA_REPORT_BYTES, _is_linked_path, write_beta_export
from codeatlas.service.persistence import repository_id

INCIDENT_REPORT_VERSION: Literal["beta-incidents-v1"] = "beta-incidents-v1"
MAX_INCIDENTS = 25
MAX_INCIDENT_BYTES = MAX_BETA_REPORT_BYTES
MAX_INCIDENT_AGE_SECONDS = 90 * 86400

IncidentComponent = Literal[
    "service", "provider", "review", "recovery", "session", "tests", "patch",
    "github", "extension", "worktree", "tools", "database",
]
IncidentReasonCode = Literal[
    "service_unavailable", "service_timeout", "provider_unavailable", "provider_failed",
    "stale_review", "pending_recovery", "recovery_failed", "session_cancelled",
    "targeted_tests_failed", "targeted_tests_cancelled", "patch_conflict",
    "unsafe_patch_rejected", "github_failed", "github_auth_failed",
    "github_network_unavailable", "github_rate_limited", "github_revision_mismatch",
    "github_response_invalid", "extension_failed", "extension_disconnected",
    "orphan_worktree", "tools_unavailable", "database_unavailable",
    "database_corrupted", "database_migration_failed",
]
IncidentSeverity = Literal["info", "warning", "error"]
IncidentRecoveryStatus = Literal[
    "not_required", "pending_recovery", "recovered", "recovery_failed", "expired",
    "cancelled", "manual_review_required",
]
IncidentMessage = Literal[
    "The local service is unavailable.", "The local service request timed out.",
    "The selected provider is unavailable.", "The selected provider request failed.",
    "The review is stale and cannot authorize further actions.",
    "The review requires recovery.", "Review recovery failed.",
    "The session was cancelled.", "Targeted tests failed.", "Targeted tests were cancelled.",
    "The patch conflicts with the current workspace.", "The patch was rejected by safety policy.",
    "The GitHub review operation failed.", "GitHub authorization is unavailable.",
    "The GitHub network request failed.", "GitHub rate limits blocked the request.",
    "The GitHub revision no longer matches the review.", "The GitHub response was invalid.",
    "The extension operation failed.", "The extension disconnected from the local service.",
    "An isolated worktree may require manual inspection.", "Required tools are unavailable.",
    "The service database is unavailable.", "The service database integrity check failed.",
    "The service database migration validation failed.",
]
IncidentRemediation = Literal[
    "Check local service availability before explicitly retrying.",
    "Check the selected provider configuration before explicitly retrying.",
    "Create a new review of the current repository revision.",
    "Inspect recovery status through the owning service; recovered approvals remain historical.",
    "Inspect recovery status through the owning service; do not apply historical approvals.",
    "Start a new session only when explicitly requested.",
    "Inspect test results locally before requesting another approved test run.",
    "Inspect the workspace locally and request a new patch; do not force application.",
    "Request a safe replacement proposal; do not bypass safety policy.",
    "Check GitHub availability and authorization locally before explicitly retrying a dry-run.",
    "Check the extension and local service before explicitly reconnecting.",
    "Inspect worktree ownership locally; persisted paths never authorize automatic deletion.",
    "Check tool availability locally; do not install dependencies automatically.",
    "Inspect storage through the owning service; do not open, repair, or migrate its database here.",
]
RepositoryIdentity = Annotated[str, Field(pattern=r"^repo-[0-9a-f]{64}$")]
SessionIdentity = Annotated[str, Field(pattern=r"^beta-session-[0-9a-f]{32}$")]
RunIdentity = Annotated[str, Field(pattern=r"^(?:rev-[0-9a-f]{12}|run-[0-9a-f]{32})$")]


class _IncidentSpec(NamedTuple):
    component: IncidentComponent
    severity: IncidentSeverity
    message: IncidentMessage
    remediation: IncidentRemediation


_SERVICE: IncidentRemediation = "Check local service availability before explicitly retrying."
_PROVIDER: IncidentRemediation = "Check the selected provider configuration before explicitly retrying."
_REVIEW: IncidentRemediation = "Create a new review of the current repository revision."
_GITHUB: IncidentRemediation = "Check GitHub availability and authorization locally before explicitly retrying a dry-run."
_EXTENSION: IncidentRemediation = "Check the extension and local service before explicitly reconnecting."
_DATABASE: IncidentRemediation = "Inspect storage through the owning service; do not open, repair, or migrate its database here."
_SPECS: dict[IncidentReasonCode, _IncidentSpec] = {
    "service_unavailable": _IncidentSpec("service", "error", "The local service is unavailable.", _SERVICE),
    "service_timeout": _IncidentSpec("service", "error", "The local service request timed out.", _SERVICE),
    "provider_unavailable": _IncidentSpec("provider", "error", "The selected provider is unavailable.", _PROVIDER),
    "provider_failed": _IncidentSpec("provider", "error", "The selected provider request failed.", _PROVIDER),
    "stale_review": _IncidentSpec("review", "warning", "The review is stale and cannot authorize further actions.", _REVIEW),
    "pending_recovery": _IncidentSpec("recovery", "warning", "The review requires recovery.", "Inspect recovery status through the owning service; recovered approvals remain historical."),
    "recovery_failed": _IncidentSpec("recovery", "error", "Review recovery failed.", "Inspect recovery status through the owning service; do not apply historical approvals."),
    "session_cancelled": _IncidentSpec("session", "info", "The session was cancelled.", "Start a new session only when explicitly requested."),
    "targeted_tests_failed": _IncidentSpec("tests", "error", "Targeted tests failed.", "Inspect test results locally before requesting another approved test run."),
    "targeted_tests_cancelled": _IncidentSpec("tests", "info", "Targeted tests were cancelled.", "Inspect test results locally before requesting another approved test run."),
    "patch_conflict": _IncidentSpec("patch", "warning", "The patch conflicts with the current workspace.", "Inspect the workspace locally and request a new patch; do not force application."),
    "unsafe_patch_rejected": _IncidentSpec("patch", "warning", "The patch was rejected by safety policy.", "Request a safe replacement proposal; do not bypass safety policy."),
    "github_failed": _IncidentSpec("github", "error", "The GitHub review operation failed.", _GITHUB),
    "github_auth_failed": _IncidentSpec("github", "error", "GitHub authorization is unavailable.", _GITHUB),
    "github_network_unavailable": _IncidentSpec("github", "error", "The GitHub network request failed.", _GITHUB),
    "github_rate_limited": _IncidentSpec("github", "warning", "GitHub rate limits blocked the request.", _GITHUB),
    "github_revision_mismatch": _IncidentSpec("github", "warning", "The GitHub revision no longer matches the review.", _REVIEW),
    "github_response_invalid": _IncidentSpec("github", "error", "The GitHub response was invalid.", _GITHUB),
    "extension_failed": _IncidentSpec("extension", "error", "The extension operation failed.", _EXTENSION),
    "extension_disconnected": _IncidentSpec("extension", "warning", "The extension disconnected from the local service.", _EXTENSION),
    "orphan_worktree": _IncidentSpec("worktree", "warning", "An isolated worktree may require manual inspection.", "Inspect worktree ownership locally; persisted paths never authorize automatic deletion."),
    "tools_unavailable": _IncidentSpec("tools", "warning", "Required tools are unavailable.", "Check tool availability locally; do not install dependencies automatically."),
    "database_unavailable": _IncidentSpec("database", "error", "The service database is unavailable.", _DATABASE),
    "database_corrupted": _IncidentSpec("database", "error", "The service database integrity check failed.", _DATABASE),
    "database_migration_failed": _IncidentSpec("database", "error", "The service database migration validation failed.", _DATABASE),
}


def _bounded_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("incident_timestamp_invalid")
    value = value.astimezone(UTC)
    if not datetime(2000, 1, 1, tzinfo=UTC) <= value < datetime(2100, 1, 1, tzinfo=UTC):
        raise ValueError("incident_timestamp_invalid")
    return value


class BetaIncident(BaseModel):
    """Fixed safe metadata; workspace state is unknown unless explicitly observed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    incident_id: str = Field(pattern=r"^beta-incident-[0-9a-f]{32}$")
    timestamp: datetime
    component: IncidentComponent
    reason_code: IncidentReasonCode
    severity: IncidentSeverity
    session_id: SessionIdentity | None = None
    run_id: RunIdentity | None = None
    repository_identity_hash: RepositoryIdentity
    recovery_status: IncidentRecoveryStatus = "not_required"
    safe_message: IncidentMessage
    remediation: IncidentRemediation
    redaction_status: Literal["safe_metadata_only"] = "safe_metadata_only"
    workspace_unchanged: bool | None = None

    @model_validator(mode="after")
    def validate_safe_projection(self) -> BetaIncident:
        _bounded_timestamp(self.timestamp)
        spec = _SPECS[self.reason_code]
        if (self.component, self.severity, self.safe_message, self.remediation) != spec:
            raise ValueError("incident_projection_invalid")
        return self


def create_beta_incident(
    *, repository: str | Path, reason_code: IncidentReasonCode,
    session_id: str | None = None, run_id: str | None = None,
    recovery_status: IncidentRecoveryStatus = "not_required",
    workspace_unchanged: bool | None = None, timestamp: datetime | None = None,
    incident_id: str | None = None,
) -> BetaIncident:
    """Project an explicitly selected reason; never accept exceptions or free text."""
    if not isinstance(reason_code, str) or reason_code not in _SPECS:
        raise ValueError("incident_reason_invalid")
    spec = _SPECS[reason_code]
    return BetaIncident.model_validate({
        "incident_id": incident_id if incident_id is not None else f"beta-incident-{uuid.uuid4().hex}",
        "timestamp": _bounded_timestamp(timestamp if timestamp is not None else datetime.now(UTC)),
        "component": spec.component, "reason_code": reason_code, "severity": spec.severity,
        "session_id": session_id, "run_id": run_id,
        "repository_identity_hash": repository_id(repository), "recovery_status": recovery_status,
        "safe_message": spec.message, "remediation": spec.remediation,
        "workspace_unchanged": workspace_unchanged,
    })


class BetaIncidentReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    report_version: Literal["beta-incidents-v1"] = INCIDENT_REPORT_VERSION
    repository_identity_hash: RepositoryIdentity
    incidents: tuple[BetaIncident, ...] = Field(default=(), max_length=MAX_INCIDENTS)

    @model_validator(mode="after")
    def validate_scope(self) -> BetaIncidentReport:
        seen: set[str] = set()
        for incident in self.incidents:
            if incident.repository_identity_hash != self.repository_identity_hash:
                raise ValueError("incident_scope_mismatch")
            if incident.incident_id in seen:
                raise ValueError("incident_duplicate_id")
            seen.add(incident.incident_id)
        return self


def _payload(report: BetaIncidentReport) -> bytes:
    content = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True, allow_nan=False) + "\n"
    payload = content.encode("utf-8")
    if len(payload) > MAX_INCIDENT_BYTES:
        raise ValueError("incident_size_limit")
    return payload


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("incident_duplicate_key")
        result[key] = value
    return result


class BetaIncidentStore:
    """Explicit single-writer JSON store; never opens a service database or repository files."""

    def __init__(
        self, path: str | Path, *, repository: str | Path,
        max_incidents: int = MAX_INCIDENTS,
        max_age_seconds: int = MAX_INCIDENT_AGE_SECONDS,
    ) -> None:
        if (type(max_incidents) is not int or not 1 <= max_incidents <= MAX_INCIDENTS
                or type(max_age_seconds) is not int or not 60 <= max_age_seconds <= MAX_INCIDENT_AGE_SECONDS):
            raise ValueError("incident_retention_invalid")
        self.path = Path(path).expanduser().absolute()
        self.repository_identity_hash = repository_id(repository)
        self.max_incidents = max_incidents
        self.max_age_seconds = max_age_seconds

    def _validate_path(self) -> None:
        path = self.path
        if (not path.name or "\0" in str(path) or not path.parent.is_dir()
                or _is_linked_path(path)
                or (path.exists() and not path.is_file())
                or (hasattr(os.path, "isreserved") and os.path.isreserved(path))):
            raise ValueError("incident_storage_path_invalid")

    def _retain(self, report: BetaIncidentReport, now: datetime) -> BetaIncidentReport:
        if report.repository_identity_hash != self.repository_identity_hash:
            raise ValueError("incident_scope_mismatch")
        if any(item.timestamp > now for item in report.incidents):
            raise ValueError("incident_timestamp_future")
        cutoff = now - timedelta(seconds=self.max_age_seconds)
        retained = sorted(
            (item for item in report.incidents if item.timestamp >= cutoff),
            key=lambda item: (item.timestamp, item.incident_id),
        )[-self.max_incidents:]
        while True:
            result = BetaIncidentReport(repository_identity_hash=self.repository_identity_hash, incidents=tuple(retained))
            try:
                _payload(result)
                return result
            except ValueError:
                if not retained:
                    raise
                retained.pop(0)

    def _load_raw(self) -> BetaIncidentReport:
        self._validate_path()
        try:
            with self.path.open("rb") as stream:
                content = stream.read(MAX_INCIDENT_BYTES + 1)
        except OSError:
            raise ValueError("incident_storage_unavailable") from None
        if len(content) > MAX_INCIDENT_BYTES:
            raise ValueError("incident_size_limit")
        try:
            value = json.loads(content, object_pairs_hook=_unique_keys)
            if not isinstance(value, dict):
                raise TypeError("incident_payload_invalid")
            report = BetaIncidentReport.model_validate_json(content)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("incident_payload_invalid") from None
        if report.repository_identity_hash != self.repository_identity_hash:
            raise ValueError("incident_scope_mismatch")
        return report

    def create(self) -> BetaIncidentReport:
        self._validate_path()
        if self.path.exists():
            raise ValueError("incident_storage_exists")
        report = BetaIncidentReport(repository_identity_hash=self.repository_identity_hash)
        self._publish(_payload(report), replace=False)
        return report

    def load(self, *, now: datetime | None = None) -> BetaIncidentReport:
        """Return retained metadata without rewriting or deleting the stored file."""
        selected_now = _bounded_timestamp(now if now is not None else datetime.now(UTC))
        return self._retain(self._load_raw(), selected_now)

    def append(self, incident: BetaIncident, *, now: datetime | None = None) -> BetaIncidentReport:
        """Persist an incident; identical retained IDs are idempotent, conflicting IDs fail."""
        incident = BetaIncident.model_validate(incident.model_dump(mode="python"))
        if incident.repository_identity_hash != self.repository_identity_hash:
            raise ValueError("incident_scope_mismatch")
        selected_now = _bounded_timestamp(now if now is not None else datetime.now(UTC))
        if incident.timestamp > selected_now:
            raise ValueError("incident_timestamp_future")
        self._validate_path()
        exists = self.path.exists()
        current = self._load_raw() if exists else BetaIncidentReport(repository_identity_hash=self.repository_identity_hash)
        for previous in current.incidents:
            if previous.incident_id == incident.incident_id:
                if previous != incident:
                    raise ValueError("incident_duplicate_conflict")
                return self._retain(current, selected_now)
        retained = self._retain(current, selected_now)
        # Make room before validating the hard tuple bound.
        items = (*retained.incidents, incident)
        items = tuple(sorted(items, key=lambda item: (item.timestamp, item.incident_id))[-self.max_incidents:])
        report = self._retain(BetaIncidentReport(repository_identity_hash=self.repository_identity_hash, incidents=items), selected_now)
        self._publish(_payload(report), replace=exists)
        return report

    def export(self, destination: str | Path, *, now: datetime | None = None) -> BetaIncidentReport:
        report = self.load(now=now)
        write_beta_export(report, destination)
        return report

    def _publish(self, payload: bytes, *, replace: bool) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".codeatlas-incidents-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._validate_path()
            if replace:
                os.replace(temporary, self.path)
                temporary = None
            else:
                os.link(temporary, self.path)
        except FileExistsError:
            raise ValueError("incident_storage_exists") from None
        except OSError:
            raise ValueError("incident_storage_unavailable") from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


__all__ = [
    "INCIDENT_REPORT_VERSION",
    "MAX_INCIDENTS",
    "MAX_INCIDENT_AGE_SECONDS",
    "MAX_INCIDENT_BYTES",
    "BetaIncident",
    "BetaIncidentReport",
    "BetaIncidentStore",
    "IncidentComponent",
    "IncidentMessage",
    "IncidentReasonCode",
    "IncidentRecoveryStatus",
    "IncidentRemediation",
    "IncidentSeverity",
    "create_beta_incident",
]
