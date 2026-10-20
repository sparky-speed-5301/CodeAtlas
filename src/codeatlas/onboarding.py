"""Bounded, read-only controlled-repository onboarding."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas import __version__
from codeatlas.beta import (
    MAX_BETA_REPORT_BYTES,
    ControlledBetaConfig,
    check_beta,
    load_beta_config,
)
from codeatlas.client import ClientError, CodeAtlasClient
from codeatlas.core.language import Language, detect_language
from codeatlas.git.errors import GitExecutableError
from codeatlas.git.executable import git_executable
from codeatlas.service.models import (
    CapabilitiesResponse,
    HealthResponse,
    StorageHealthResponse,
)
from codeatlas.service.persistence import repository_id
from codeatlas.verification.runner_selection import find_js_runner

OnboardingStatus = Literal["ready", "ready_with_limitations", "blocked", "unavailable"]
CheckStatus = Literal["ok", "limitation", "blocked", "unavailable", "not_applicable"]
CheckName = Literal[
    "configuration", "python", "cli", "provider", "live_authorization", "repository",
    "cleanliness", "revisions", "language", "test_tool", "service", "health",
    "database", "migration", "extension_api", "offline_benchmark", "github", "cleanup",
]
FailureCode = Literal[
    "invalid_beta_configuration", "unsupported_python", "cli_unavailable", "missing_provider",
    "invalid_provider_configuration", "live_provider_not_authorized", "invalid_repository",
    "repository_identity_mismatch", "dirty_worktree", "stale_revision", "unsupported_language",
    "unavailable_test_tool", "unavailable_service", "corrupted_database", "migration_invalid",
    "extension_service_mismatch", "github_permission_failure", "github_unavailable",
    "check_unavailable", "cleanup_unavailable", "offline_benchmark_failed", "ok",
    "not_requested", "not_applicable", "storage_health_not_exposed", "extension_connection_unverified",
    "github_review_not_run", "validation_not_run",
]
SupportedLanguage = Literal["python", "javascript", "typescript", "go", "java", "rust"]
AddCheck = Callable[[CheckName, CheckStatus, FailureCode, str], None]


class OnboardingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repository: str = Field(min_length=1, max_length=4096)
    base: str = Field(min_length=1, max_length=256)
    head: str = Field(default="HEAD", min_length=1, max_length=256)
    expected_repository_id: str | None = Field(default=None, pattern=r"^repo-[0-9a-f]{64}$")
    expected_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    expected_head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    allow_live_provider: bool = False
    service_url: str | None = Field(default=None, max_length=512)
    extension_version: str | None = Field(default=None, max_length=64, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    github_repository: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    github_pr: int | None = Field(default=None, ge=1, le=2_147_483_647)
    timeout_seconds: float = Field(default=20.0, gt=0, le=30, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_inputs(self) -> OnboardingRequest:
        if any("\0" in value or any(ord(char) < 32 for char in value) for value in (self.repository, self.base, self.head)):
            raise ValueError("onboarding inputs contain control characters")
        if (self.github_repository is None) != (self.github_pr is None):
            raise ValueError("GitHub onboarding requires both repository and PR")
        return self


class OnboardingCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: CheckName
    status: CheckStatus
    code: FailureCode
    detail: str = Field(min_length=1, max_length=256)


class OnboardingReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    report_version: Literal["beta-onboarding-v1"] = "beta-onboarding-v1"
    status: OnboardingStatus
    beta_ready: bool
    production_ready: Literal[False] = False
    automatic_actions_enabled: Literal[False] = False
    provider_mode: Literal["offline_deterministic", "live_provider"] | None = None
    live_provider_authorized: bool = False
    repository_identity: str | None = Field(default=None, pattern=r"^repo-[0-9a-f]{64}$")
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    languages: tuple[SupportedLanguage, ...] = Field(default=(), max_length=6)
    checks: tuple[OnboardingCheck, ...] = Field(min_length=1, max_length=18)
    limitations: tuple[Annotated[str, Field(max_length=256)], ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def consistent_status(self) -> OnboardingReport:
        if len({check.name for check in self.checks}) != len(self.checks):
            raise ValueError("onboarding check names must be unique")
        expected = _status(self.checks)
        if self.status != expected or self.beta_ready != (expected in {"ready", "ready_with_limitations"}):
            raise ValueError("onboarding readiness contradicts checks")
        return self


def _status(checks: tuple[OnboardingCheck, ...]) -> OnboardingStatus:
    if any(check.status == "blocked" for check in checks):
        return "blocked"
    if any(check.status == "unavailable" for check in checks):
        return "unavailable"
    if any(check.status == "limitation" for check in checks):
        return "ready_with_limitations"
    return "ready"


class ProbeUnavailable(RuntimeError):
    """An owned probe exceeded its deadline, output bound, or launch capability."""


def _probe(command: list[str], *, cwd: Path, deadline: float, git: bool = False) -> tuple[int, bytes]:
    remaining = min(5.0, deadline - time.monotonic())
    if remaining <= 0:
        raise ProbeUnavailable()
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA") if key in os.environ}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"})
    try:
        if git:
            command = [str(git_executable()), "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *command]
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    except (OSError, ValueError, GitExecutableError):
        raise ProbeUnavailable() from None
    output: list[bytes] = [b"", b""]
    exceeded = threading.Event()

    def consume(index: int) -> None:
        stream = process.stdout if index == 0 else process.stderr
        assert stream is not None
        data = stream.read(65_537)
        output[index] = data
        if len(data) > 65_536:
            exceeded.set()
            try:
                process.kill()
            except OSError:
                pass
        stream.close()

    readers = [threading.Thread(target=consume, args=(index,), daemon=True) for index in (0, 1)]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise ProbeUnavailable() from None
    finally:
        for reader in readers:
            reader.join(timeout=1)
    if exceeded.is_set() or any(reader.is_alive() for reader in readers):
        raise ProbeUnavailable()
    return process.returncode, output[0]


def _offline_smoke() -> bool:
    from codeatlas.benchmark.repair import (
        RepairBenchmarkCase,
        run_deterministic_benchmark,
    )

    case = RepairBenchmarkCase(
        case_id="beta-offline-smoke-v1", language="python", target_file="calc.py",
        source="def add(a, b):\n    return a - b\n", expected_source="def add(a, b):\n    return a + b\n",
        changed_line_range=(1, 2),
    )
    candidate = {
        "diagnosis": "Addition uses subtraction.",
        "patch": "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n",
        "target_files": ["calc.py"], "explanation": "Use addition.", "expected_behavior_change": "Return the sum.",
        "risk_level": "low", "confidence": 1.0,
    }
    summary = run_deterministic_benchmark([case], {case.case_id: candidate})
    return summary.total == 1 and summary.useful == 1 and summary.safe == 1


def _cleanup_smoke() -> bool:
    with tempfile.TemporaryDirectory(prefix="codeatlas-onboarding-") as directory:
        owned = Path(directory)
        marker = owned / "probe"
        marker.write_bytes(b"owned onboarding resource")
        if marker.read_bytes() != b"owned onboarding resource":
            return False
    return not owned.exists()


def onboard_repository(request: OnboardingRequest, *, config_path: str | Path | None = None) -> OnboardingReport:
    """Observe local readiness; never execute repository tests or publish GitHub writes."""
    checks: list[OnboardingCheck] = []
    config: ControlledBetaConfig | None = None
    identity: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    languages: tuple[SupportedLanguage, ...] = ()
    deadline = time.monotonic() + request.timeout_seconds

    def add(name: CheckName, status: CheckStatus, code: FailureCode, detail: str) -> None:
        checks.append(OnboardingCheck(name=name, status=status, code=code, detail=detail))

    def finish() -> OnboardingReport:
        selected = tuple(checks)
        status = _status(selected)
        limitations = (
            "Onboarding is point-in-time evidence, not approval, test-pass evidence, or production readiness.",
            "The offline smoke evaluates an embedded sanitized fixture, not this repository's review quality.",
            "GitHub probing verifies exact PR pins and read access only; a full review dry-run remains a separate explicit command.",
            "Extension version matching is a compatibility check, not proof of a running extension connection.",
            "Cleanup probes only newly owned temporary files; persisted paths never authorize deletion or process termination.",
            "Fixed local probes share a deadline; operating-system network isolation is not independently verified.",
        )
        report = OnboardingReport(
            status=status, beta_ready=status in {"ready", "ready_with_limitations"},
            provider_mode=config.provider_mode if config is not None else None,
            live_provider_authorized=bool(config is not None and config.provider_mode == "live_provider" and request.allow_live_provider),
            repository_identity=identity, base_sha=base_sha, head_sha=head_sha, languages=languages,
            checks=selected, limitations=limitations,
        )
        if len(report.model_dump_json().encode()) > MAX_BETA_REPORT_BYTES:
            raise ValueError("onboarding report exceeds the beta report bound")
        return report

    try:
        config = load_beta_config(config_path)
    except (OSError, TypeError, ValueError):
        add("configuration", "blocked", "invalid_beta_configuration", "The beta configuration is unavailable or invalid.")
        return finish()
    add("configuration", "ok", "ok", "Strict beta safety policies are locked.")
    _runtime_checks(config, request, add)
    root = Path(request.repository).expanduser().absolute()
    try:
        result, raw = _probe(["rev-parse", "--show-toplevel"], cwd=root, deadline=deadline, git=True)
        if result:
            add("repository", "blocked", "invalid_repository", "The selected path is not a Git worktree.")
            return finish()
        root = Path(raw.decode("utf-8").strip()).resolve()
        result, raw = _probe(["rev-parse", "--is-inside-work-tree"], cwd=root, deadline=deadline, git=True)
        if result or raw.strip() != b"true":
            add("repository", "blocked", "invalid_repository", "The selected repository is not an ordinary Git worktree.")
            return finish()
        identity = repository_id(root)
        mismatch = request.expected_repository_id is not None and request.expected_repository_id != identity
        add("repository", "blocked" if mismatch else "ok", "repository_identity_mismatch" if mismatch else "ok", "Local canonical repository identity was checked; this does not prove remote origin ownership.")
        result, raw = _probe(["status", "--porcelain=v1", "-z", "--untracked-files=normal"], cwd=root, deadline=deadline, git=True)
        add("cleanliness", "unavailable" if result else "blocked" if raw else "ok", "check_unavailable" if result else "dirty_worktree" if raw else "ok", "Repository cleanliness was checked without modifying the index or worktree.")
        resolved = []
        for revision in (request.base, request.head):
            result, raw = _probe(["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"], cwd=root, deadline=deadline, git=True)
            sha = raw.decode("ascii", errors="replace").strip()
            if result or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
                add("revisions", "blocked", "stale_revision", "A requested revision cannot be resolved to an immutable commit.")
                return finish()
            resolved.append(sha)
        base_sha, head_sha = resolved
        stale = (request.expected_base_sha is not None and request.expected_base_sha != base_sha) or (request.expected_head_sha is not None and request.expected_head_sha != head_sha)
        add("revisions", "blocked" if stale else "ok", "stale_revision" if stale else "ok", "Base and head commits were resolved and compared with any explicitly supplied pins.")
        result, raw = _probe(["ls-tree", "-r", "--name-only", "-z", head_sha], cwd=root, deadline=deadline, git=True)
        if result:
            raise ProbeUnavailable()
        detected = {detect_language(path.decode("utf-8", errors="replace")) for path in raw.split(b"\0") if path}
        languages = tuple(sorted(language.value for language in detected if language != Language.UNKNOWN))
        add("language", "ok" if languages else "blocked", "ok" if languages else "unsupported_language", "Supported source languages were detected from committed filenames only.")
        tools_ok = _test_tools(root, languages)
        add("test_tool", "limitation" if tools_ok else "unavailable", "validation_not_run" if tools_ok else "unavailable_test_tool", "Test tools were discovered without running tests, wrappers, repository code, or dependency installation.")
    except (OSError, UnicodeError, ValueError, ProbeUnavailable):
        if not any(check.name == "repository" for check in checks):
            add("repository", "unavailable", "invalid_repository", "Repository checks could not be completed within their bounds.")
        else:
            add("language" if not any(check.name == "language" for check in checks) else "test_tool", "unavailable", "check_unavailable", "A local probe was unavailable or exceeded its bounds.")
        return finish()
    _service_checks(config, request, deadline, add, checks)
    try:
        smoke_ok = _offline_smoke() if time.monotonic() < deadline else False
        add("offline_benchmark", "ok" if smoke_ok else "unavailable", "ok" if smoke_ok else "offline_benchmark_failed", "The embedded offline deterministic benchmark was evaluated without network or repository execution.")
    except (OSError, TypeError, ValueError, RuntimeError):
        add("offline_benchmark", "unavailable", "offline_benchmark_failed", "The bounded offline benchmark could not be completed.")
    _github_check(request, root, base_sha, head_sha, deadline, add)
    try:
        cleanup_ok = _cleanup_smoke() if time.monotonic() < deadline else False
    except OSError:
        cleanup_ok = False
    add("cleanup", "ok" if cleanup_ok else "unavailable", "ok" if cleanup_ok else "cleanup_unavailable", "Only a newly created owned temporary directory was tested for cleanup.")
    return finish()


def _runtime_checks(config: ControlledBetaConfig, request: OnboardingRequest, add: AddCheck) -> None:
    report = check_beta(config)
    python = next(check for check in report.checks if check.name == "python")
    add("python", "ok" if python.status == "ok" else "blocked", "ok" if python.status == "ok" else "unsupported_python", "Python version compatibility was checked.")
    try:
        installed = importlib.metadata.version("codeatlas") == __version__
    except importlib.metadata.PackageNotFoundError:
        installed = False
    add("cli", "ok" if installed else "unavailable", "ok" if installed else "cli_unavailable", "The installed CLI distribution version was compared with the running package.")
    provider = next(check for check in report.checks if check.name == "provider")
    if provider.status in {"ok", "not_applicable"}:
        add("provider", "ok", "ok", "The explicitly selected provider mode is available; no live request was made.")
    else:
        code: FailureCode = "missing_provider" if provider.code == "credential_unavailable" else "invalid_provider_configuration"
        add("provider", "blocked", code, "The explicitly selected provider or its configuration is unavailable.")
    authorized = config.provider_mode == "offline_deterministic" or request.allow_live_provider
    add("live_authorization", "ok" if authorized else "blocked", "ok" if authorized else "live_provider_not_authorized", "Live-provider authorization is checked for this invocation only; onboarding never contacts a provider.")


def _test_tools(root: Path, languages: tuple[SupportedLanguage, ...]) -> bool:
    for language in languages:
        if language == "python":
            if not Path(sys.executable).is_file():
                return False
        elif language in {"javascript", "typescript"}:
            if find_js_runner(root) is None:
                return False
        elif (
            (language == "go" and shutil.which("go") is None)
            or (language == "rust" and shutil.which("cargo") is None)
            or (language == "java" and shutil.which("mvn") is None and shutil.which("gradle") is None)
        ):
            return False
    return bool(languages)


def _service_checks(config: ControlledBetaConfig, request: OnboardingRequest, deadline: float, add: AddCheck, checks: list[OnboardingCheck]) -> None:
    if request.service_url is None:
        required = config.workflow == "vscode"
        add("service", "unavailable" if required else "limitation", "unavailable_service" if required else "not_requested", "A loopback service must be selected explicitly to check service and storage readiness.")
        dependent: tuple[CheckName, ...] = ("health", "database", "migration", "extension_api")
        for name in dependent:
            add(name, "limitation", "not_requested", "This service-dependent check was not requested.")
        return
    try:
        remaining = min(10.0, deadline - time.monotonic())
        client = CodeAtlasClient(request.service_url, timeout=remaining)

        def fetch(endpoint: str) -> dict:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise ClientError("timeout", "Diagnostic deadline exceeded")
            return client.request("GET", endpoint, timeout=min(budget, 10.0))

        health = HealthResponse.model_validate(fetch("/health"))
        if health.status != "ok" or health.service != "codeatlas-service":
            raise ClientError("unreachable", "Service health unavailable")
        add("service", "ok", "ok", "The selected loopback service is available.")
        add("health", "ok", "ok", "The service health endpoint reports healthy.")
        expected_providers = {"live"} if config.provider_mode == "live_provider" else {"mock", "deterministic"}
        if health.provider not in expected_providers:
            index = next(index for index, check in enumerate(checks) if check.name == "provider")
            checks[index] = OnboardingCheck(
                name="provider", status="blocked", code="invalid_provider_configuration",
                detail="The service provider mode does not match the explicitly selected beta mode.",
            )
        capabilities = CapabilitiesResponse.model_validate(fetch("/capabilities"))
        mismatch = health.version != __version__ or (request.extension_version is not None and request.extension_version != health.version)
        add("extension_api", "blocked" if mismatch else "limitation", "extension_service_mismatch" if mismatch else "extension_connection_unverified", "API and package compatibility were checked; an actual extension connection remains unverified.")
        try:
            storage = StorageHealthResponse.model_validate(fetch("/diagnostics/storage"), strict=True)
            if storage.state_lifetime != capabilities.state_lifetime:
                raise ValueError("service storage lifetime changed during onboarding")
            _project_storage(storage, add)
        except ClientError as error:
            absent = error.status == 404
            storage_checks: tuple[CheckName, ...] = ("database", "migration")
            for name in storage_checks:
                add(name, "limitation" if absent else "unavailable", "storage_health_not_exposed" if absent else "unavailable_service", "The service could not expose bounded owned-storage health; database health is not inferred from HTTP health.")
        if capabilities.api_version != "1":
            raise ClientError("incompatible_api", "API mismatch")
    except ClientError as error:
        present = {check.name for check in checks}
        mismatch = error.code == "incompatible_api"
        service_checks: tuple[CheckName, ...] = ("service", "health", "database", "migration", "extension_api")
        for name in service_checks:
            if name not in present:
                add(name, "blocked" if mismatch and name == "extension_api" else "unavailable", "extension_service_mismatch" if mismatch and name == "extension_api" else "unavailable_service", "The selected service is unavailable, incompatible, or returned an invalid bounded response.")
    except (OSError, TypeError, ValueError):
        present = {check.name for check in checks}
        service_checks = ("service", "health", "database", "migration", "extension_api")
        for name in service_checks:
            if name not in present:
                add(name, "unavailable", "unavailable_service", "The selected service returned an invalid bounded response.")


def _project_storage(storage: StorageHealthResponse, add: AddCheck) -> None:
    coherent = (
        (storage.state_lifetime == "process" and storage.status == "not_applicable" and storage.code == "not_applicable")
        or (storage.state_lifetime == "persistent" and storage.status == "ok" and storage.code == "ok")
        or (storage.state_lifetime == "persistent" and storage.status == "error" and storage.code in {"corrupted_database", "migration_invalid", "database_unavailable"})
    )
    if not coherent:
        raise ValueError("storage health observations contradict one another")
    names: tuple[CheckName, ...] = ("database", "migration")
    if storage.code == "not_applicable":
        for name in names:
            add(name, "not_applicable", "not_applicable", "The service uses process-local storage; persistent database checks do not apply.")
    elif storage.code == "ok":
        for name in names:
            add(name, "ok", "ok", "Bounded health checks on the service-owned database connection passed.")
    else:
        code: FailureCode = "corrupted_database" if storage.code == "corrupted_database" else "migration_invalid" if storage.code == "migration_invalid" else "unavailable_service"
        status: CheckStatus = "unavailable" if storage.code == "database_unavailable" else "blocked"
        for name in names:
            add(name, status, code, "The service-owned storage health check failed; no database errors or contents are exported.")


def _github_check(
    request: OnboardingRequest,
    root: Path,
    base: str,
    head: str,
    deadline: float,
    add: AddCheck,
) -> None:
    if request.github_repository is None:
        add("github", "limitation", "not_requested", "GitHub read access and dry-run preflight were not requested; no GitHub request was made.")
        return
    executable = shutil.which("gh")
    if executable is None:
        add("github", "unavailable", "github_unavailable", "The existing authenticated GitHub CLI is unavailable.")
        return
    try:
        result, raw = _probe([executable, "pr", "view", str(request.github_pr), "--repo", request.github_repository, "--json", "number,baseRefOid,headRefOid"], cwd=root, deadline=deadline)
        if result:
            add("github", "blocked", "github_permission_failure", "The explicitly selected pull request could not be read with the existing GitHub CLI authentication.")
            return
        payload = json.loads(raw)
        valid = isinstance(payload, dict) and payload.get("number") == request.github_pr and payload.get("baseRefOid") == base and payload.get("headRefOid") == head
        add("github", "limitation" if valid else "blocked", "github_review_not_run" if valid else "stale_revision", "Read-only GitHub dry-run preflight checked PR pins and read access; full review execution and write permissions remain unverified.")
    except (OSError, ValueError, ProbeUnavailable):
        add("github", "unavailable", "github_unavailable", "GitHub preflight exceeded its bounds or returned invalid data; no remote write was attempted.")
