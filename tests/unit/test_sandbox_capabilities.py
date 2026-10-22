"""Tests for explicit validation sandbox capability contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from codeatlas.verification import (
    SandboxCapability,
    SandboxCapabilities,
    discover_sandbox_capabilities,
)
from codeatlas.verification.models import TestPlan, TestResult


def test_discovery_distinguishes_worktree_runtime_and_execution() -> None:
    paths = {
        "docker": "/usr/bin/docker",
        "unshare": "/usr/bin/unshare",
    }

    report = discover_sandbox_capabilities(which=lambda name: paths.get(name))

    worktree = report.for_backend("worktree")
    container = report.for_backend("container")
    namespace = report.for_backend("namespace")

    assert report.schema_version == "12.1"
    assert worktree.status == "available"
    assert worktree.evidence_basis == "contract_only"
    assert container.status == "not_run"
    assert container.runtime == "docker"
    assert container.runtime_available is True
    assert container.execution_observed is False
    assert container.network_isolation_verified is False
    assert namespace.status == "not_run"
    assert namespace.runtime == "unshare"


def test_missing_runtimes_are_unavailable_not_unsupported_success() -> None:
    report = discover_sandbox_capabilities(which=lambda _name: None)

    assert report.for_backend("container").status == "unavailable"
    assert report.for_backend("container").evidence_basis == "unavailable"
    assert report.for_backend("namespace").status == "unavailable"
    assert all(not item.execution_observed for item in report.capabilities)


def test_capability_model_rejects_unobserved_isolation_claims() -> None:
    with pytest.raises(ValidationError, match="observed sandbox execution"):
        SandboxCapability(
            backend="container",
            status="not_run",
            evidence_basis="not_run",
            network_isolation_verified=True,
        )

    with pytest.raises(ValidationError, match="measured evidence"):
        SandboxCapability(
            backend="namespace",
            status="not_run",
            evidence_basis="measured",
        )


def test_execution_evidence_requires_explicit_attempt_and_available_status() -> None:
    with pytest.raises(ValidationError, match="execution attempt"):
        SandboxCapability(
            backend="container",
            status="available",
            evidence_basis="measured",
            execution_observed=True,
        )

    with pytest.raises(ValidationError, match="not-run sandbox"):
        SandboxCapability(
            backend="container",
            status="blocked",
            evidence_basis="blocked",
            execution_attempted=True,
            execution_observed=True,
        )


def test_verification_models_record_backend_without_changing_defaults() -> None:
    plan = TestPlan(language="python")
    result = TestResult()
    explicit = TestResult(sandbox_backend="container")

    assert plan.sandbox_backend == "worktree"
    assert result.sandbox_backend == "worktree"
    assert explicit.sandbox_backend == "container"


def test_missing_backend_report_returns_explicit_unavailable_contract() -> None:
    report = SandboxCapabilities(capabilities=[])

    capability = report.for_backend("namespace")

    assert capability.status == "unavailable"
    assert capability.reason == "backend_not_reported"
