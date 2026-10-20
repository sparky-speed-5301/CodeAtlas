"""Strict local controlled-beta configuration, preflight, and reporting."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from codeatlas.beta_execution import BetaExecutionSummary
from codeatlas.git.errors import GitExecutableError
from codeatlas.git.executable import git_executable
from codeatlas.providers.errors import ProviderConfigError
from codeatlas.service.contract import openapi_document
from codeatlas.service.diagnostics import (
    ServiceDiagnosticsResponse,
    collect_service_diagnostics,
)

BETA_CONFIG_VERSION: Literal["controlled-beta-v1"] = "controlled-beta-v1"
BETA_REPORT_VERSION: Literal["beta-report-v1"] = "beta-report-v1"
MAX_BETA_REPORT_BYTES: Literal[32768] = 32_768
MAX_BETA_CONFIG_BYTES = 16_384

KNOWN_LIMITATIONS = (
    "Controlled beta evidence does not establish broad public production readiness.",
    "Live-provider quality and cost depend on the explicitly selected provider and have not been statistically generalized.",
    "GitHub beta use is review-only dry-run unless a separate existing command receives an explicit publish request.",
    "A process killed during validation may leave an isolated worktree; persisted paths never authorize automatic deletion.",
    "Network policy is enforced by command policy, but operating-system network isolation is not independently verified.",
    "Repository-wide historical Ruff and mypy findings remain baseline debt.",
)


class LiveProviderSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: Literal["live"] = "live"
    model_name: str = Field(min_length=1, max_length=128, pattern=r"^[^\s]+$")
    provider_config_path: str = Field(min_length=1, max_length=512)
    api_key_env_var: str = Field(default="CODEATLAS_API_KEY", pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    privacy_mode: Literal["zero_retention", "no_training", "standard"] = "zero_retention"

    @model_validator(mode="after")
    def validate_path(self) -> LiveProviderSelection:
        value = self.provider_config_path
        parts = value.replace("\\", "/").split("/")
        if (
            Path(value).is_absolute() or PureWindowsPath(value).drive
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(character) < 32 for character in value)
            or ":" in value
        ):
            raise ValueError("provider_config_path must be a safe configuration-relative path")
        return self


class BetaSafetyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    automatic_apply: Literal[False] = False
    automatic_approval: Literal[False] = False
    automatic_commit: Literal[False] = False
    automatic_merge: Literal[False] = False
    automatic_push: Literal[False] = False
    arbitrary_commands: Literal[False] = False
    dependency_installation: Literal[False] = False
    unrestricted_network_access: Literal[False] = False


class BetaDiagnosticsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    export_enabled: Literal[True] = True
    max_report_bytes: Literal[32768] = MAX_BETA_REPORT_BYTES
    max_service_reviews: Literal[50] = 50
    timeout_seconds: float = Field(default=10.0, gt=0, le=10)
    include_repository_paths: Literal[False] = False
    include_provider_transcripts: Literal[False] = False
    include_credentials: Literal[False] = False


class BetaCleanupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Literal["owned_temporary_resources_only"] = "owned_temporary_resources_only"
    automatic_persisted_path_deletion: Literal[False] = False
    retain_failed_sandbox: Literal[False] = False
    foreign_process_termination: Literal[False] = False


class ControlledBetaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    _config_directory: Path = PrivateAttr(default_factory=Path.cwd)

    config_version: Literal["controlled-beta-v1"] = BETA_CONFIG_VERSION
    beta_mode: Literal[True] = True
    provider_mode: Literal["offline_deterministic", "live_provider"] = "offline_deterministic"
    live_provider: LiveProviderSelection | None = None
    workflow: Literal["cli", "vscode"] = "cli"
    github_mode: Literal["review_only_dry_run"] = "review_only_dry_run"
    diagnostics: BetaDiagnosticsPolicy = BetaDiagnosticsPolicy()
    cleanup: BetaCleanupPolicy = BetaCleanupPolicy()
    safety: BetaSafetyPolicy = BetaSafetyPolicy()

    @model_validator(mode="after")
    def validate_provider_mode(self) -> ControlledBetaConfig:
        if self.provider_mode == "live_provider" and self.live_provider is None:
            raise ValueError("live_provider mode requires an explicit live_provider selection")
        if self.provider_mode == "offline_deterministic" and self.live_provider is not None:
            raise ValueError("offline_deterministic mode cannot include live_provider settings")
        return self


class BetaCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["python", "git", "api_contract", "configuration", "provider"]
    status: Literal["ok", "warning", "error", "not_applicable"]
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    detail: str = Field(min_length=1, max_length=256)


class BetaCheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_version: Literal["beta-check-v1"] = "beta-check-v1"
    status: Literal["ready_with_limitations", "blocked"]
    beta_ready: bool
    production_ready: Literal[False] = False
    config_version: Literal["controlled-beta-v1"] = BETA_CONFIG_VERSION
    provider_mode: Literal["offline_deterministic", "live_provider"]
    workflow: Literal["cli", "vscode"]
    github_mode: Literal["review_only_dry_run"]
    checks: tuple[BetaCheck, ...] = Field(min_length=5, max_length=5)
    limitations: tuple[Annotated[str, Field(max_length=256)], ...] = Field(max_length=8)


class BetaStartPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: Literal["beta-start-v1"] = "beta-start-v1"
    status: Literal["configured"] = "configured"
    beta_mode: Literal[True] = True
    production_ready: Literal[False] = False
    provider_mode: Literal["offline_deterministic", "live_provider"]
    workflow: Literal["cli", "vscode"]
    github_mode: Literal["review_only_dry_run"]
    config_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    automatic_actions_enabled: Literal[False] = False
    commands: tuple[Annotated[str, Field(max_length=256)], ...] = Field(min_length=3, max_length=4)
    cleanup_policy: Literal["owned_temporary_resources_only"] = "owned_temporary_resources_only"
    limitations: tuple[Annotated[str, Field(max_length=256)], ...] = Field(max_length=8)


class BetaReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_version: Literal["beta-report-v1"] = BETA_REPORT_VERSION
    generated_at: datetime
    status: Literal["ready_with_limitations", "blocked"]
    beta_ready: bool
    production_ready: Literal[False] = False
    configuration: dict[Literal[
        "config_version", "provider_mode", "workflow", "github_mode",
        "automatic_actions_enabled", "unrestricted_network_access",
    ], str | bool] = Field(max_length=6)
    check: BetaCheckReport
    service_diagnostics: ServiceDiagnosticsResponse | None = None
    execution: BetaExecutionSummary | None = None
    cleanup: dict[Literal[
        "strategy", "automatic_persisted_path_deletion", "foreign_process_termination",
    ], str | bool] = Field(max_length=3)
    limitations: tuple[Annotated[str, Field(max_length=256)], ...] = Field(max_length=8)


def default_beta_config() -> ControlledBetaConfig:
    return ControlledBetaConfig()


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise ValueError("configuration must be a regular file")
    with path.open("rb") as stream:
        content = stream.read(MAX_BETA_CONFIG_BYTES + 1)
    if len(content) > MAX_BETA_CONFIG_BYTES:
        raise ValueError("configuration exceeds the input size bound")

    def unique_keys(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("configuration contains duplicate keys")
            result[key] = value
        return result

    payload = json.loads(content, object_pairs_hook=unique_keys)
    if not isinstance(payload, dict):
        raise TypeError("configuration must be a JSON object")
    return payload


def load_beta_config(path: str | Path | None = None) -> ControlledBetaConfig:
    if path is None:
        return default_beta_config()
    source = Path(path).expanduser().absolute()
    config = ControlledBetaConfig.model_validate(_read_json(source))
    config._config_directory = source.parent
    return config


def _check(name: str, status: str, code: str, detail: str) -> BetaCheck:
    return BetaCheck.model_validate({"name": name, "status": status, "code": code, "detail": detail})


def check_beta(config: ControlledBetaConfig) -> BetaCheckReport:
    checks: list[BetaCheck] = []
    python_ok = sys.version_info >= (3, 11)
    checks.append(_check("python", "ok" if python_ok else "error", "python_supported" if python_ok else "python_unsupported", platform.python_version()))

    try:
        result = subprocess.run(
            [str(git_executable()), "--version"], capture_output=True, text=True,
            check=False, timeout=5,
        )
        git_ok = result.returncode == 0
    except (OSError, subprocess.SubprocessError, GitExecutableError):
        git_ok = False
    checks.append(_check("git", "ok" if git_ok else "error", "git_available" if git_ok else "git_unavailable", "Git is available." if git_ok else "Git is unavailable."))

    try:
        contract = openapi_document()
        api_ok = contract.get("openapi") == "3.1.0" and bool(contract.get("paths"))
    except (OSError, TypeError, ValueError, RuntimeError):
        api_ok = False
    checks.append(_check("api_contract", "ok" if api_ok else "error", "api_contract_loaded" if api_ok else "api_contract_unavailable", "The local API v1 contract is available." if api_ok else "The local API contract is unavailable."))

    checks.append(_check("configuration", "ok", "safe_defaults_locked", "Beta mode is selected; automatic capabilities are disabled in this configuration."))
    if config.provider_mode == "offline_deterministic":
        checks.append(_check("provider", "not_applicable", "offline_selected", "Offline deterministic mode makes no live-provider request."))
    else:
        assert config.live_provider is not None
        try:
            from codeatlas.providers.config import load_provider_config

            source = config._config_directory / config.live_provider.provider_config_path
            if not source.resolve().is_relative_to(config._config_directory.resolve()):
                raise ValueError("provider configuration escapes its configuration directory")
            provider = load_provider_config(overrides=_read_json(source))
            config_ok = (
                provider.model_name == config.live_provider.model_name
                and provider.api_key_env_var == config.live_provider.api_key_env_var
                and provider.privacy_mode == config.live_provider.privacy_mode
            )
        except (OSError, TypeError, ValueError, ProviderConfigError):
            config_ok = False
        credential_available = bool(os.environ.get(config.live_provider.api_key_env_var, "").strip())
        available = config_ok and credential_available
        code = "provider_available" if available else (
            "provider_config_unavailable" if not config_ok else "credential_unavailable"
        )
        detail = "The explicitly selected live-provider configuration and credential are available." if available else (
            "The selected provider configuration is unavailable or does not match the beta selection."
            if not config_ok else
            "The selected provider credential environment variable is unavailable; no credential value was exported."
        )
        checks.append(_check("provider", "ok" if available else "error", code, detail))

    blocked = any(item.status == "error" for item in checks)
    return BetaCheckReport(
        status="blocked" if blocked else "ready_with_limitations",
        beta_ready=not blocked,
        provider_mode=config.provider_mode,
        workflow=config.workflow,
        github_mode=config.github_mode,
        checks=tuple(checks),
        limitations=KNOWN_LIMITATIONS,
    )


def start_beta(config: ControlledBetaConfig, *, allow_live_provider: bool = False) -> BetaStartPlan:
    report = check_beta(config)
    if config.provider_mode == "live_provider" and not allow_live_provider:
        raise ValueError("live-provider beta mode requires --allow-live-provider on this invocation")
    if config.provider_mode == "live_provider" and next(
        item for item in report.checks if item.name == "provider"
    ).status != "ok":
        raise ValueError("the explicitly selected live provider is not available")
    if report.status == "blocked":
        raise ValueError("beta preflight is blocked")
    identity = hashlib.sha256(config.model_dump_json().encode("utf-8")).hexdigest()
    review = "python -m codeatlas review --repo <REPOSITORY> --base <BASE> --head <HEAD>"
    if config.provider_mode == "live_provider":
        review += " --review-provider live --provider <MODEL> --provider-config <PROVIDER_CONFIG>"
    commands: tuple[str, ...] = (
        review,
        "python -m codeatlas github review --repo <OWNER/REPO> --pr <NUMBER> --local-repo <REPOSITORY> --dry-run",
        "python -m codeatlas beta-report --service",
    )
    if config.workflow == "vscode":
        commands = ("python -m codeatlas serve --host 127.0.0.1", *commands)
    return BetaStartPlan(
        provider_mode=config.provider_mode,
        workflow=config.workflow,
        github_mode=config.github_mode,
        config_identity=identity,
        commands=commands,
        limitations=KNOWN_LIMITATIONS,
    )


def _is_loopback_service_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and (port is None or 1 <= port <= 65_535)
        and parsed.path in {"", "/"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def build_beta_report(
    config: ControlledBetaConfig,
    *,
    service_url: str | None = None,
    timeout: float | None = None,
    execution: BetaExecutionSummary | None = None,
) -> BetaReport:
    check = check_beta(config)
    diagnostics = None
    if service_url is not None:
        if not _is_loopback_service_url(service_url):
            raise ValueError("service diagnostics require a loopback HTTP origin")
        selected_timeout = config.diagnostics.timeout_seconds if timeout is None else timeout
        if not math.isfinite(selected_timeout) or selected_timeout <= 0:
            raise ValueError("diagnostic timeout must be positive and finite")
        diagnostics = collect_service_diagnostics(service_url, timeout=min(selected_timeout, 10.0))
    blocked = check.status == "blocked" or (diagnostics is not None and diagnostics.status == "error")
    report = BetaReport(
        generated_at=datetime.now(UTC),
        status="blocked" if blocked else "ready_with_limitations",
        beta_ready=not blocked,
        configuration={
            "config_version": config.config_version,
            "provider_mode": config.provider_mode,
            "workflow": config.workflow,
            "github_mode": config.github_mode,
            "automatic_actions_enabled": False,
            "unrestricted_network_access": False,
        },
        check=check,
        service_diagnostics=diagnostics,
        execution=execution,
        cleanup={
            "strategy": config.cleanup.strategy,
            "automatic_persisted_path_deletion": False,
            "foreign_process_termination": False,
        },
        limitations=KNOWN_LIMITATIONS,
    )
    if len(report.model_dump_json().encode("utf-8")) > MAX_BETA_REPORT_BYTES:
        raise ValueError("beta report exceeds its fixed export bound")
    return report


def _is_linked_path(path: Path) -> bool:
    return any(
        candidate.is_symlink()
        or (hasattr(candidate, "is_junction") and candidate.is_junction())
        for candidate in (path, *path.parents)
    )


def write_beta_export(report: BaseModel, destination: str | Path) -> None:
    path = Path(destination).expanduser().absolute()
    if (
        not path.name or "\0" in str(path) or path.exists()
        or not path.parent.is_dir() or _is_linked_path(path)
        or (hasattr(os.path, "isreserved") and os.path.isreserved(path))
    ):
        raise ValueError("beta export requires a new regular file in an existing unlinked directory")
    from codeatlas.orchestrator.repair_models import repair_payload_is_safe

    validated = type(report).model_validate_json(report.model_dump_json())
    data = validated.model_dump(mode="json")
    if not repair_payload_is_safe(data):
        raise ValueError("beta export failed the existing secret audit")
    payload = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > MAX_BETA_REPORT_BYTES:
        raise ValueError("beta export exceeds its fixed size bound")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=".codeatlas-beta-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise ValueError("beta export destination already exists") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


__all__ = [
    "BETA_CONFIG_VERSION",
    "BETA_REPORT_VERSION",
    "KNOWN_LIMITATIONS",
    "MAX_BETA_REPORT_BYTES",
    "BetaCheckReport",
    "BetaReport",
    "BetaStartPlan",
    "ControlledBetaConfig",
    "build_beta_report",
    "check_beta",
    "default_beta_config",
    "load_beta_config",
    "start_beta",
    "write_beta_export",
]
