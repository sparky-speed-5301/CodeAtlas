"""Explicit offline and pinned public-repository benchmark collection."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .repository import (
    RepositoryBenchmarkCase,
    RepositoryBenchmarkSuite,
    load_benchmark_suite,
)

CollectionMode = Literal["offline", "public"]
CollectionStatus = Literal["available", "unavailable"]

UNAVAILABLE_REASONS = (
    "case_not_found",
    "fixture_unavailable",
    "metadata_unavailable",
    "invalid_url",
    "missing_pin",
    "license_missing",
    "fetch_failed",
    "sha_verification_failed",
    "timeout",
    "cleanup_failed",
)


class CollectionRequest(BaseModel):
    """Inputs for one explicitly selected benchmark collection operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: CollectionMode
    suite_path: str = Field(min_length=1)
    case_id: str = Field(min_length=1, max_length=128)
    fixture_root: str | None = None
    repository_url: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    license_spdx: str | None = Field(default=None, max_length=128)
    provenance: str | None = Field(default=None, max_length=512)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @model_validator(mode="after")
    def validate_request(self) -> CollectionRequest:
        if self.mode == "offline" and any((self.repository_url, self.base_sha, self.head_sha)):
            raise ValueError("offline collection cannot accept public repository inputs")
        return self


class CollectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: CollectionMode
    case_id: str
    status: CollectionStatus
    reason: str | None = None
    repository_identity: str | None = None
    repository_url: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    license_spdx: str | None = None
    provenance: str | None = None
    benchmark_version: str | None = None
    fixture_digest: str | None = None

    @model_validator(mode="after")
    def validate_status(self) -> CollectionResult:
        if self.status == "unavailable":
            if self.reason not in UNAVAILABLE_REASONS:
                raise ValueError("unavailable results require a stable reason code")
            if any((self.fixture_digest,)):
                raise ValueError("unavailable results cannot contain fixture content metadata")
        elif self.reason is not None:
            raise ValueError("available results cannot contain an unavailable reason")
        return self


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _unavailable(request: CollectionRequest, reason: str) -> CollectionResult:
    return CollectionResult(mode=request.mode, case_id=request.case_id, status="unavailable", reason=reason)


def _case(suite: RepositoryBenchmarkSuite, case_id: str) -> RepositoryBenchmarkCase | None:
    return next((candidate for candidate in suite.cases if candidate.case_id == case_id), None)


def _read_metadata(fixture: Path) -> tuple[str, str] | None:
    path = fixture / "metadata.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("license_spdx"), str) or not isinstance(value.get("provenance"), str):
        return None
    if not value["license_spdx"] or not value["provenance"]:
        return None
    return value["license_spdx"], value["provenance"]


def _fixture_digest(fixture: Path, case: RepositoryBenchmarkCase) -> str | None:
    digest = hashlib.sha256()
    for snapshot in ("before", "after"):
        for relative in sorted(case.changed_files):
            path = fixture / snapshot / relative
            try:
                digest.update(snapshot.encode("ascii"))
                digest.update(relative.encode("utf-8"))
                digest.update(path.read_bytes())
            except OSError:
                return None
    return digest.hexdigest()


def collect_offline(request: CollectionRequest) -> CollectionResult:
    """Validate a local fixture and return metadata only; never uses the network."""
    try:
        suite = load_benchmark_suite(request.suite_path, fixture_root=request.fixture_root)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _unavailable(request, "fixture_unavailable")
    case = _case(suite, request.case_id)
    if case is None or not case.fixture_ref:
        return _unavailable(request, "case_not_found")
    root = Path(request.fixture_root).resolve() if request.fixture_root else Path(request.suite_path).resolve().parent
    fixture = (root / case.fixture_ref).resolve()
    metadata = _read_metadata(fixture)
    digest = _fixture_digest(fixture, case)
    if not metadata or digest is None:
        return _unavailable(request, "metadata_unavailable" if metadata is None else "fixture_unavailable")
    return CollectionResult(
        mode="offline", case_id=case.case_id, status="available", repository_identity=case.repository_identity,
        repository_url=case.repository_url, base_sha=case.base_sha, head_sha=case.head_sha,
        license_spdx=metadata[0], provenance=metadata[1], benchmark_version=suite.benchmark_version,
        fixture_digest=digest,
    )


def _git(directory: Path, args: list[str], timeout: float) -> bool:
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ASKPASS": os.devnull,
    }
    try:
        subprocess.run(
            ["git", *args], cwd=directory, env=environment, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    return True


def collect_public(request: CollectionRequest) -> CollectionResult:
    """Fetch two exact commits into a temporary repository and discard it."""
    if request.mode != "public":
        raise ValueError("collect_public requires public mode")
    if not request.repository_url or not request.base_sha or not request.head_sha:
        return _unavailable(request, "missing_pin")
    if not _is_https_url(request.repository_url):
        return _unavailable(request, "invalid_url")
    if not _is_sha(request.base_sha) or not _is_sha(request.head_sha) or request.base_sha == request.head_sha:
        return _unavailable(request, "sha_verification_failed")
    if not request.license_spdx or not request.provenance:
        return _unavailable(request, "license_missing")
    try:
        suite = load_benchmark_suite(request.suite_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _unavailable(request, "case_not_found")
    case = _case(suite, request.case_id)
    if case is None:
        return _unavailable(request, "case_not_found")
    if case.repository_url and case.repository_url != request.repository_url:
        return _unavailable(request, "case_not_found")
    if case.base_sha != request.base_sha or case.head_sha != request.head_sha:
        return _unavailable(request, "sha_verification_failed")
    try:
        directory = Path(tempfile.mkdtemp(prefix="codeatlas-benchmark-"))
    except OSError:
        return _unavailable(request, "fetch_failed")
    failure: str | None = None
    try:
        if not _git(directory, ["init", "--quiet"], request.timeout_seconds) or not _git(directory, ["fetch", "--no-tags", "--depth=1", request.repository_url, request.base_sha, request.head_sha], request.timeout_seconds):
            failure = "fetch_failed"
        elif any(
            not _git(directory, ["rev-parse", "--verify", f"{sha}^{{commit}}"], request.timeout_seconds)
            for sha in (request.base_sha, request.head_sha)
        ):
            failure = "sha_verification_failed"
    except TimeoutError:
        failure = "timeout"
    except (OSError, RuntimeError):
        failure = "fetch_failed"
    try:
        shutil.rmtree(directory)
    except OSError:
        return _unavailable(request, "cleanup_failed")
    if failure:
        return _unavailable(request, failure)
    return CollectionResult(
        mode="public", case_id=request.case_id, status="available", repository_identity=case.repository_identity,
        repository_url=request.repository_url, base_sha=request.base_sha, head_sha=request.head_sha,
        license_spdx=request.license_spdx, provenance=request.provenance, benchmark_version=suite.benchmark_version,
    )


def collect_benchmark(request: CollectionRequest) -> CollectionResult:
    return collect_offline(request) if request.mode == "offline" else collect_public(request)


__all__ = ["UNAVAILABLE_REASONS", "CollectionMode", "CollectionRequest", "CollectionResult", "CollectionStatus", "collect_benchmark", "collect_offline", "collect_public"]
