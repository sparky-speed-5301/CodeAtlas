"""Explicit capability contracts for repository validation sandboxes.

Runtime discovery is deliberately weaker than isolation verification.  Finding
an executable such as Docker, Podman, ``unshare``, or ``bwrap`` only records
that a possible backend is present; it never claims that a sandbox was started
or that its network, filesystem, process, or resource boundaries were enforced.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SandboxBackend = Literal["worktree", "container", "namespace"]
SandboxStatus = Literal["available", "not_run", "unavailable", "unsupported", "blocked"]
SandboxEvidenceBasis = Literal["measured", "contract_only", "unavailable", "not_run", "blocked"]


class SandboxCapability(BaseModel):
    """Bounded evidence about one sandbox backend.

    ``runtime_available`` is a discovery fact.  The isolation and enforcement
    fields are intentionally false until an authorized execution path records
    direct evidence for them.
    """

    model_config = ConfigDict(extra="ignore")

    backend: SandboxBackend
    status: SandboxStatus
    runtime: str | None = None
    runtime_available: bool = False
    execution_attempted: bool = False
    execution_observed: bool = False
    filesystem_isolation_verified: bool = False
    process_isolation_verified: bool = False
    network_isolation_verified: bool = False
    resource_limits_verified: bool = False
    evidence_basis: SandboxEvidenceBasis
    reason: str = Field(default="", max_length=256)
    limitations: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_claims(self) -> "SandboxCapability":
        verified = (
            self.filesystem_isolation_verified,
            self.process_isolation_verified,
            self.network_isolation_verified,
            self.resource_limits_verified,
        )
        if any(verified) and not self.execution_observed:
            raise ValueError("isolation verification requires observed sandbox execution")
        if self.execution_observed and not self.execution_attempted:
            raise ValueError("observed sandbox execution requires an execution attempt")
        if self.status in {"not_run", "unavailable", "unsupported", "blocked"} and self.execution_observed:
            raise ValueError("unavailable or not-run sandbox cannot claim observed execution")
        if self.status in {"not_run", "unavailable", "unsupported", "blocked"} and self.evidence_basis == "measured":
            raise ValueError("unavailable or not-run sandbox cannot use measured evidence")
        return self


class SandboxCapabilities(BaseModel):
    """Discovery report for all supported backend contracts."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "12.1"
    capabilities: list[SandboxCapability] = Field(default_factory=list, max_length=3)
    limitations: list[str] = Field(default_factory=list, max_length=12)

    def for_backend(self, backend: SandboxBackend) -> SandboxCapability:
        """Return the capability for ``backend`` or an explicit unavailable result."""
        for capability in self.capabilities:
            if capability.backend == backend:
                return capability
        return SandboxCapability(
            backend=backend,
            status="unavailable",
            evidence_basis="unavailable",
            reason="backend_not_reported",
            limitations=["No capability report was supplied for this backend."],
        )


RuntimeLookup = Callable[[str], str | None]


def _runtime_capability(
    backend: Literal["container", "namespace"],
    candidates: Sequence[str],
    *,
    which: RuntimeLookup,
) -> SandboxCapability:
    runtime: str | None = None
    runtime_path: str | None = None
    for candidate in candidates:
        path = which(candidate)
        if path:
            runtime = candidate
            runtime_path = path
            break

    if runtime is None:
        return SandboxCapability(
            backend=backend,
            status="unavailable",
            runtime_available=False,
            evidence_basis="unavailable",
            reason="runtime_not_found",
            limitations=[
                "No supported runtime executable was discovered.",
                "Sandbox execution was not attempted.",
            ],
        )

    return SandboxCapability(
        backend=backend,
        status="not_run",
        runtime=runtime,
        runtime_available=True,
        evidence_basis="not_run",
        reason="runtime_discovered_execution_not_attempted",
        limitations=[
            f"Runtime executable discovered at {runtime_path}; no sandbox was started.",
            "Runtime presence does not verify filesystem, process, network, or resource isolation.",
        ],
    )


def discover_sandbox_capabilities(*, which: RuntimeLookup = shutil.which) -> SandboxCapabilities:
    """Discover backend prerequisites without executing any sandbox runtime."""

    worktree = SandboxCapability(
        backend="worktree",
        status="available",
        runtime="git-worktree",
        runtime_available=True,
        evidence_basis="contract_only",
        reason="existing_worktree_backend",
        limitations=[
            "Worktree validation is not container or namespace isolation.",
            "Process, network, and resource isolation are not verified by discovery.",
        ],
    )
    container = _runtime_capability("container", ("docker", "podman", "nerdctl"), which=which)
    namespace = _runtime_capability("namespace", ("bwrap", "unshare"), which=which)
    return SandboxCapabilities(
        capabilities=[worktree, container, namespace],
        limitations=[
            "Discovery did not execute repository code or invoke a sandbox runtime.",
            "Only an explicitly observed run may produce measured isolation claims.",
        ],
    )


__all__ = [
    "SandboxBackend",
    "SandboxStatus",
    "SandboxEvidenceBasis",
    "SandboxCapability",
    "SandboxCapabilities",
    "discover_sandbox_capabilities",
]
