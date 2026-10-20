"""Bounded controlled-beta sessions with explicit, non-automatic stages."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.beta import (
    MAX_BETA_REPORT_BYTES,
    ControlledBetaConfig,
    write_beta_export,
)
from codeatlas.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNetworkError,
    GitHubRateLimitError,
    GitHubResponseError,
    SHAMismatchError,
)
from codeatlas.service.persistence import repository_id

SESSION_VERSION: Literal["beta-session-v1"] = "beta-session-v1"
MAX_SESSION_EVENTS = 50
MAX_SESSION_RETRIES = 3

ExecutionState = Literal["offline", "live", "fallback", "unavailable", "blocked", "not_run"]
LifecycleStatus = Literal["active", "completed", "cancelled", "failed"]
SessionStage = Literal[
    "created", "offline_review", "live_review", "github_dry_run", "fix_proposal",
    "validation_approval", "validation", "targeted_tests", "apply", "revert",
    "retry", "cancel", "complete", "cleanup",
]
CleanupStatus = Literal["not_run", "completed", "retained", "not_applicable", "failed"]
ResultStatus = Literal["not_run", "passed", "failed", "blocked", "unavailable"]
ApplyStatus = Literal["not_run", "applied", "not_applied", "reverted", "failed", "blocked", "unavailable"]
FailureCode = Literal[
    "session_cancelled", "session_timeout", "session_retry_limit", "session_scope_mismatch",
    "invalid_stage", "offline_review_failed", "live_provider_not_authorized",
    "provider_unavailable", "provider_failed", "github_auth_failed",
    "github_network_unavailable", "github_rate_limited", "github_response_invalid",
    "github_revision_mismatch", "github_review_failed", "proposal_unavailable",
    "proposal_rejected", "validation_approval_failed", "validation_failed",
    "targeted_tests_failed", "apply_approval_failed", "apply_failed",
    "revert_failed", "service_unavailable", "operation_timeout", "cleanup_failed",
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SafeIdentifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")]


class SeverityDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    critical: int = Field(default=0, ge=0, le=10_000)
    high: int = Field(default=0, ge=0, le=10_000)
    medium: int = Field(default=0, ge=0, le=10_000)
    low: int = Field(default=0, ge=0, le=10_000)
    info: int = Field(default=0, ge=0, le=10_000)

    @property
    def total(self) -> int:
        return self.critical + self.high + self.medium + self.low + self.info


class BetaSessionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sequence: int = Field(ge=1, le=1_000_000)
    stage: SessionStage
    result: ExecutionState
    at: datetime
    failure_code: FailureCode | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> BetaSessionEvent:
        if self.result in {"blocked", "unavailable"} and self.failure_code is None:
            raise ValueError("blocked and unavailable events require a stable failure code")
        return self


class BetaSession(BaseModel):
    """Metadata-only session record. It contains no source, prompt, token, or transcript fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session_version: Literal["beta-session-v1"] = SESSION_VERSION
    session_id: str = Field(pattern=r"^beta-session-[0-9a-f]{32}$")
    revision: int = Field(default=1, ge=1, le=1_000_000)
    repository_identity_hash: str = Field(pattern=r"^repo-[0-9a-f]{64}$")
    case_id: SafeIdentifier
    language: Literal["python", "javascript", "typescript", "go", "java", "rust"]
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    provider_mode: ExecutionState
    lifecycle_status: LifecycleStatus = "active"
    created_at: datetime
    updated_at: datetime
    events: tuple[BetaSessionEvent, ...] = Field(min_length=1, max_length=MAX_SESSION_EVENTS)
    events_total: int = Field(ge=1, le=1_000_000)
    events_truncated: bool = False
    cleanup_status: CleanupStatus = "not_run"
    failure_code: FailureCode | None = None
    review_duration_ms: float | None = Field(default=None, ge=0, le=86_400_000, allow_inf_nan=False)
    finding_count: int = Field(default=0, ge=0, le=10_000)
    severity_distribution: SeverityDistribution = Field(default_factory=SeverityDistribution)
    accepted_count: int = Field(default=0, ge=0, le=10_000)
    rejected_count: int = Field(default=0, ge=0, le=10_000)
    abstained_count: int = Field(default=0, ge=0, le=10_000)
    proposal_count: int = Field(default=0, ge=0, le=1_000)
    validation_approved: bool = False
    apply_approved: bool = False
    validation_result: ResultStatus = "not_run"
    test_result: ResultStatus = "not_run"
    github_dry_run_result: ResultStatus = "not_run"
    apply_status: ApplyStatus = "not_run"
    revert_status: ApplyStatus = "not_run"
    retry_count: int = Field(default=0, ge=0, le=MAX_SESSION_RETRIES)
    cancelled: bool = False
    timed_out: bool = False
    safe_model_identifier: SafeIdentifier | None = None
    context_hash: Digest | None = None
    patch_hash: Digest | None = None
    production_ready: Literal[False] = False
    automatic_actions_enabled: Literal[False] = False

    @model_validator(mode="after")
    def validate_consistency(self) -> BetaSession:
        if self.events_total < len(self.events) or self.events_truncated != (self.events_total > len(self.events)):
            raise ValueError("event counters contradict retained history")
        if self.finding_count != self.severity_distribution.total:
            raise ValueError("finding count must equal the severity distribution")
        if self.accepted_count + self.rejected_count + self.abstained_count > self.finding_count:
            raise ValueError("finding decisions exceed the finding count")
        if self.cancelled != (self.lifecycle_status == "cancelled"):
            raise ValueError("cancellation state contradicts lifecycle status")
        if self.lifecycle_status == "failed" and self.failure_code is None:
            raise ValueError("failed sessions require a stable failure code")
        return self


class BetaSessionStore:
    """Explicit JSON persistence for one safe session record; no hidden state directory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().absolute()

    def _validate_path(self, *, existing: bool) -> None:
        path = self.path
        linked = any(
            candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction())
            for candidate in (path, *path.parents)
        )
        if (not path.name or "\0" in str(path) or not path.parent.is_dir() or linked
                or (hasattr(os.path, "isreserved") and os.path.isreserved(path))):
            raise ValueError("session storage requires a regular file in an existing unlinked directory")
        if existing and (not path.is_file() or path.is_symlink()):
            raise ValueError("session record is unavailable")
        if not existing and path.exists():
            raise ValueError("session record already exists")

    @staticmethod
    def _payload(session: BetaSession) -> bytes:
        payload = (json.dumps(session.model_dump(mode="json"), indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        if len(payload) > MAX_BETA_REPORT_BYTES:
            raise ValueError("session record exceeds its fixed size bound")
        return payload

    def create(self, session: BetaSession) -> None:
        self._validate_path(existing=False)
        self._publish(self._payload(session), replace=False)

    def load(self) -> BetaSession:
        self._validate_path(existing=True)
        with self.path.open("rb") as stream:
            payload = stream.read(MAX_BETA_REPORT_BYTES + 1)
        if len(payload) > MAX_BETA_REPORT_BYTES:
            raise ValueError("session record exceeds its fixed size bound")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise TypeError("session record must be a JSON object")
        return BetaSession.model_validate_json(payload)

    def save(self, session: BetaSession) -> None:
        self._validate_path(existing=True)
        current = self.load()
        if current.session_id != session.session_id or current.repository_identity_hash != session.repository_identity_hash:
            raise ValueError("session identity cannot be replaced")
        if session.revision <= current.revision:
            raise ValueError("session revision must advance monotonically")
        self._publish(self._payload(session), replace=True)

    def _publish(self, payload: bytes, *, replace: bool) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".codeatlas-session-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, self.path)
                temporary = None
            else:
                os.link(temporary, self.path)
        except FileExistsError:
            raise ValueError("session record already exists") from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


class BetaSessionRunner:
    """Runs only the explicitly selected stage and returns an updated safe projection."""

    def __init__(self, session: BetaSession) -> None:
        self.session = session

    @classmethod
    def create(
        cls, *, repository: str | Path, case_id: str, language: str,
        base_sha: str, head_sha: str, config: ControlledBetaConfig,
    ) -> BetaSessionRunner:
        mode: ExecutionState = "live" if config.provider_mode == "live_provider" else "offline"
        now = datetime.now(UTC)
        event = BetaSessionEvent(sequence=1, stage="created", result=mode, at=now)
        session = BetaSession(
            session_id=f"beta-session-{uuid.uuid4().hex}",
            repository_identity_hash=repository_id(repository), case_id=case_id,
            language=cast(Literal["python", "javascript", "typescript", "go", "java", "rust"], language),
            base_sha=base_sha, head_sha=head_sha, provider_mode=mode,
            created_at=now, updated_at=now, events=(event,), events_total=1,
        )
        return cls(session)

    def _event(
        self, stage: SessionStage, result: ExecutionState, *, failure_code: FailureCode | None = None,
        updates: dict[str, object] | None = None,
    ) -> BetaSession:
        if self.session.lifecycle_status != "active" and stage not in {"cleanup"}:
            raise ValueError("terminal sessions cannot run another stage")
        event = BetaSessionEvent(
            sequence=self.session.events_total + 1, stage=stage, result=result,
            at=datetime.now(UTC), failure_code=failure_code,
        )
        retained = (*self.session.events, event)[-MAX_SESSION_EVENTS:]
        values: dict[str, object] = {
            "revision": self.session.revision + 1,
            "events": retained, "events_total": self.session.events_total + 1,
            "events_truncated": self.session.events_total + 1 > len(retained),
            "updated_at": event.at,
        }
        if failure_code is not None:
            values["failure_code"] = failure_code
        if updates:
            values.update(updates)
        self.session = BetaSession.model_validate({
            **self.session.model_dump(mode="python"), **values,
        })
        return self.session

    def _check_scope(self, repository: str | Path, base_sha: str, head_sha: str) -> None:
        if (repository_id(repository) != self.session.repository_identity_hash
                or base_sha != self.session.base_sha or head_sha != self.session.head_sha):
            raise ValueError("session repository or revision scope does not match")

    def require_active(self) -> None:
        if self.session.lifecycle_status != "active":
            raise ValueError("terminal sessions cannot run another stage")

    def record_service_failure(
        self, *, stage: SessionStage, timed_out: bool = False, blocked: bool = False,
        failure_code: FailureCode | None = None,
    ) -> BetaSession:
        code: FailureCode = failure_code or ("operation_timeout" if timed_out else "service_unavailable")
        result: ExecutionState = "blocked" if blocked else "unavailable"
        updates: dict[str, object] = {"timed_out": timed_out or self.session.timed_out}
        if stage == "validation":
            updates["validation_result"] = "blocked" if blocked else "unavailable"
        elif stage == "targeted_tests":
            updates["test_result"] = "blocked" if blocked else "unavailable"
        elif stage == "apply":
            updates["apply_status"] = "blocked" if blocked else "unavailable"
        elif stage == "revert":
            updates["revert_status"] = "blocked" if blocked else "unavailable"
        return self._event(stage, result, failure_code=code, updates=updates)

    @staticmethod
    def _review_projection(manifest: object) -> dict[str, object]:
        findings = list(getattr(manifest, "findings", ()))
        severity = {key: 0 for key in ("critical", "high", "medium", "low", "info")}
        accepted = rejected = abstained = 0
        for finding in findings[:10_000]:
            if not isinstance(finding, dict):
                continue
            level = str(finding.get("severity", "info")).lower()
            if level == "blocker":
                level = "critical"
            severity[level if level in severity else "info"] += 1
            status = str(finding.get("status", ""))
            feedback = str(finding.get("feedback", ""))
            decision = str(finding.get("quality_decision", ""))
            if feedback == "accepted":
                accepted += 1
            elif feedback in {"rejected", "not_useful", "false_positive", "dismissed"}:
                rejected += 1
            elif feedback == "abstained" or status == "abstained" or decision == "abstain":
                abstained += 1
        model = getattr(manifest, "model_name", None) or getattr(manifest, "model", None)
        safe_model = model if isinstance(model, str) and 1 <= len(model) <= 128 else None
        if safe_model is not None and not all(character.isalnum() or character in "._:/-" for character in safe_model):
            safe_model = None
        return {
            "review_duration_ms": min(float(getattr(manifest, "duration_ms", 0.0)), 86_400_000),
            "finding_count": sum(severity.values()), "severity_distribution": SeverityDistribution(**severity),
            "accepted_count": accepted, "rejected_count": rejected, "abstained_count": abstained,
            "safe_model_identifier": safe_model,
        }

    def run_offline_review(self, *, repository: str | Path) -> BetaSession:
        from codeatlas.orchestrator.review import run_review

        if self.session.provider_mode != "offline":
            return self._event("offline_review", "blocked", failure_code="invalid_stage")
        self._check_scope(repository, self.session.base_sha, self.session.head_sha)
        started = time.monotonic()
        result = run_review(repository, self.session.base_sha, self.session.head_sha)
        updates = self._review_projection(result.manifest)
        updates["review_duration_ms"] = min((time.monotonic() - started) * 1000, 86_400_000)
        if result.manifest.errors:
            return self._event("offline_review", "unavailable", failure_code="offline_review_failed", updates=updates)
        return self._event("offline_review", "offline", updates=updates)

    def run_live_review(
        self, *, repository: str | Path, config: ControlledBetaConfig,
        allow_live_provider: bool,
    ) -> BetaSession:
        from codeatlas.orchestrator.review import run_review

        if self.session.provider_mode != "live":
            return self._event("live_review", "blocked", failure_code="invalid_stage")
        if not allow_live_provider:
            return self._event("live_review", "blocked", failure_code="live_provider_not_authorized")
        if config.provider_mode != "live_provider" or config.live_provider is None:
            return self._event("live_review", "blocked", failure_code="provider_unavailable")
        self._check_scope(repository, self.session.base_sha, self.session.head_sha)
        result = run_review(
            repository, self.session.base_sha, self.session.head_sha,
            review_provider="live", provider_model=config.live_provider.model_name,
            provider_config_path=str(config._config_directory / config.live_provider.provider_config_path),
        )
        updates = self._review_projection(result.manifest)
        if result.manifest.errors or result.manifest.provider_output_valid is False:
            return self._event("live_review", "unavailable", failure_code="provider_failed", updates=updates)
        return self._event("live_review", "live", updates=updates)

    def run_github_dry_run(
        self, *, repository: str | Path, github_repository: str, pull_request: int,
    ) -> BetaSession:
        from codeatlas.github import run_github_pr_review

        self._check_scope(repository, self.session.base_sha, self.session.head_sha)
        try:
            report = run_github_pr_review(
                github_repository, pull_request, local_repo=repository, post=False,
                inline=False, summary_comment=False, check_run=False,
            )
            if report.mode != "dry_run" or report.write_calls != 0:
                return self._event("github_dry_run", "blocked", failure_code="github_review_failed",
                                   updates={"github_dry_run_result": "blocked"})
            result: ResultStatus = "failed" if report.errors else "passed"
            code: FailureCode | None = "github_review_failed" if report.errors else None
            state: ExecutionState = "unavailable" if report.errors else "offline"
            return self._event("github_dry_run", state, failure_code=code,
                               updates={"github_dry_run_result": result})
        except GitHubAuthError:
            code = "github_auth_failed"
        except GitHubRateLimitError:
            code = "github_rate_limited"
        except GitHubNetworkError:
            code = "github_network_unavailable"
        except SHAMismatchError:
            code = "github_revision_mismatch"
        except GitHubResponseError:
            code = "github_response_invalid"
        except GitHubError:
            code = "github_review_failed"
        return self._event("github_dry_run", "unavailable", failure_code=code,
                           updates={"github_dry_run_result": "unavailable"})

    def record_fix_proposal(
        self, *, available: bool, context_hash: str | None = None, patch_hash: str | None = None,
    ) -> BetaSession:
        if not available or patch_hash is None:
            return self._event("fix_proposal", "unavailable", failure_code="proposal_unavailable")
        return self._event("fix_proposal", self.session.provider_mode, updates={
            "proposal_count": self.session.proposal_count + 1,
            "context_hash": context_hash, "patch_hash": patch_hash,
        })

    def record_review_failure(self, *, stage: Literal["offline_review", "live_review"],
                              code: FailureCode) -> BetaSession:
        return self._event(stage, self.session.provider_mode, failure_code=code, updates={
            "lifecycle_status": "failed",
        })

    def record_validation_approval(self, *, approved: bool) -> BetaSession:
        if not approved:
            return self._event("validation_approval", "blocked", failure_code="validation_approval_failed")
        return self._event("validation_approval", self.session.provider_mode, updates={
            "validation_approved": True,
        })

    def record_validation(
        self, *, passed: bool, unavailable: bool = False, test_result: ResultStatus | None = None,
        cleanup_status: CleanupStatus | None = None,
    ) -> BetaSession:
        if not self.session.validation_approved:
            return self._event("validation", "blocked", failure_code="validation_approval_failed",
                               updates={"validation_result": "blocked"})
        updates: dict[str, object] = {}
        if test_result is not None:
            updates["test_result"] = test_result
        if cleanup_status is not None:
            updates["cleanup_status"] = cleanup_status
        if unavailable:
            return self._event("validation", "unavailable", failure_code="service_unavailable",
                               updates={**updates, "validation_result": "unavailable"})
        return self._event("validation", self.session.provider_mode if passed else "blocked",
                           failure_code=None if passed else "validation_failed",
                           updates={**updates, "validation_result": "passed" if passed else "failed"})

    def record_targeted_tests(self, *, result: ResultStatus) -> BetaSession:
        if not self.session.validation_approved:
            return self._event("targeted_tests", "blocked", failure_code="validation_approval_failed",
                               updates={"test_result": "blocked"})
        if result == "not_run":
            return self._event("targeted_tests", "not_run", updates={"test_result": result})
        state: ExecutionState = self.session.provider_mode if result in {"passed", "failed"} else (
            "unavailable" if result == "unavailable" else "blocked"
        )
        code: FailureCode | None = "targeted_tests_failed" if result == "failed" else None
        if result == "unavailable":
            code = "service_unavailable"
        updates: dict[str, object] = {"test_result": result}
        if result == "failed":
            updates["lifecycle_status"] = "failed"
        return self._event("targeted_tests", state, failure_code=code, updates=updates)

    def record_apply(self, *, status: ApplyStatus, explicitly_approved: bool) -> BetaSession:
        if not explicitly_approved:
            return self._event("apply", "blocked", failure_code="apply_approval_failed",
                               updates={"apply_status": "blocked"})
        if self.session.validation_result != "passed" or self.session.patch_hash is None:
            return self._event("apply", "blocked", failure_code="validation_failed",
                               updates={"apply_status": "blocked"})
        if status == "unavailable":
            return self._event("apply", "unavailable", failure_code="service_unavailable",
                               updates={"apply_status": status, "timed_out": True})
        success = status == "applied"
        state: ExecutionState = self.session.provider_mode if success else "blocked"
        return self._event("apply", state, failure_code=None if success else "apply_failed",
                           updates={"apply_status": status, "apply_approved": success})

    def record_revert(self, *, status: ApplyStatus) -> BetaSession:
        if self.session.apply_status != "applied":
            return self._event("revert", "blocked", failure_code="revert_failed",
                               updates={"revert_status": "blocked"})
        if status == "unavailable":
            return self._event("revert", "unavailable", failure_code="service_unavailable",
                               updates={"revert_status": status, "timed_out": True})
        success = status == "reverted"
        state: ExecutionState = self.session.provider_mode if success else "blocked"
        return self._event("revert", state, failure_code=None if success else "revert_failed",
                           updates={"revert_status": status})

    def retry(self) -> BetaSession:
        if self.session.retry_count >= MAX_SESSION_RETRIES:
            return self._event("retry", "blocked", failure_code="session_retry_limit")
        return self._event("retry", "not_run", updates={"retry_count": self.session.retry_count + 1})

    def cancel(self) -> BetaSession:
        return self._event("cancel", "not_run", failure_code="session_cancelled", updates={
            "lifecycle_status": "cancelled", "cancelled": True,
        })

    def complete(self) -> BetaSession:
        return self._event("complete", self.session.provider_mode, updates={
            "lifecycle_status": "completed",
        })

    def record_cleanup(self, *, succeeded: bool) -> BetaSession:
        return self._event("cleanup", self.session.provider_mode if succeeded else "blocked",
                           failure_code=None if succeeded else "cleanup_failed",
                           updates={"cleanup_status": "completed" if succeeded else "failed"})

    def export(self, destination: str | Path) -> None:
        write_beta_export(self.session, destination)


__all__ = [
    "MAX_SESSION_EVENTS",
    "MAX_SESSION_RETRIES",
    "SESSION_VERSION",
    "ApplyStatus",
    "BetaSession",
    "BetaSessionEvent",
    "BetaSessionRunner",
    "BetaSessionStore",
    "CleanupStatus",
    "ExecutionState",
    "FailureCode",
    "LifecycleStatus",
    "ResultStatus",
    "SeverityDistribution",
]
