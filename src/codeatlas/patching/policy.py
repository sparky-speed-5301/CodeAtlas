"""Allowed-path, change-budget, and security policy rules for patch proposals."""

from __future__ import annotations

import re
from typing import Any, Sequence
from pydantic import BaseModel, ConfigDict, Field

from codeatlas.repository.files import is_configuration_file, is_test_file
from .models import PatchFile, PatchProposal, PatchStatus

DEFAULT_PATCH_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_files": 5,
    "max_changed_lines": 150,
    "allow_new_files": False,
    "allow_delete_files": False,
    "allow_rename_files": False,
    "allow_test_changes": False,
    "allow_workflow_changes": False,
    "allow_dependency_changes": False,
    "allow_lockfile_changes": False,
    "allow_configuration_changes": False,
    "require_human_approval": True,
    "require_validation": True,
    "allow_apply": False,
}

_WORKFLOW_PATTERNS = [
    re.compile(r"(?i)^\.github/(?:workflows|actions)/"),
    re.compile(r"(?i)^\.circleci/"),
    re.compile(r"(?i)^\.gitlab-ci\.ya?ml$"),
    re.compile(r"(?i)^azure-pipelines(?:\.[^/]+)?\.ya?ml$"),
    re.compile(r"(?i)^\.travis\.ya?ml$"),
    re.compile(r"(?i)^jenkinsfile$"),
]

_LOCKFILE_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pipfile.lock",
    "cargo.lock",
    "go.sum",
}

_DEPENDENCY_PATTERNS = [
    re.compile(r"(?i)^package\.json$"),
    re.compile(r"(?i)^requirements(?:-[^/]+)?\.txt$"),
    re.compile(r"(?i)^pipfile$"),
    re.compile(r"(?i)^poetry\.toml$"),
    re.compile(r"(?i)^pyproject\.toml$"),
    re.compile(r"(?i)^setup\.(?:py|cfg)$"),
    re.compile(r"(?i)^cargo\.toml$"),
    re.compile(r"(?i)^go\.mod$"),
    re.compile(r"(?i)^pom\.xml$"),
    re.compile(r"(?i)^build\.gradle(?:\.kts)?$"),
]


def is_workflow_path(rel_path: str) -> bool:
    """Detect if a path belongs to CI/CD workflows or permission actions."""
    norm = rel_path.replace("\\", "/").strip("/")
    filename = norm.rsplit("/", 1)[-1]
    return any(p.search(norm) or p.search(filename) for p in _WORKFLOW_PATTERNS)


def is_lockfile_path(rel_path: str) -> bool:
    """Detect if a path is a package manager lockfile."""
    norm = rel_path.replace("\\", "/").strip("/").lower()
    filename = norm.rsplit("/", 1)[-1]
    return filename in _LOCKFILE_FILENAMES


def is_dependency_manifest_path(rel_path: str) -> bool:
    """Detect if a path is a package or dependency declaration manifest."""
    norm = rel_path.replace("\\", "/").strip("/").lower()
    filename = norm.rsplit("/", 1)[-1]
    if is_lockfile_path(rel_path):
        return True
    return any(p.search(filename) for p in _DEPENDENCY_PATTERNS)


class PatchPolicyDecision(BaseModel):
    """Result of policy evaluation against a proposed patch."""

    model_config = ConfigDict(extra="ignore")

    allowed: bool
    decision: str = Field(description="approved, requires_human_approval, rejected, or blocked")
    reasons: list[str] = Field(default_factory=list)
    rules_evaluated: list[str] = Field(default_factory=list)


def evaluate_patch_policy(
    proposal: PatchProposal,
    files: Sequence[PatchFile],
    config: dict[str, Any] | None = None,
) -> PatchPolicyDecision:
    """Evaluate patch gating policy against a patch proposal and its target files."""
    cfg = dict(DEFAULT_PATCH_CONFIG)
    if config:
        cfg.update(config.get("patch", config))

    rules_evaluated: list[str] = []
    reasons: list[str] = []

    # Rule 1: Patch feature toggle
    rules_evaluated.append("patch_enabled")
    if not cfg.get("enabled", True):
        return PatchPolicyDecision(
            allowed=False,
            decision=PatchStatus.REJECTED,
            reasons=["Patching is disabled by configuration"],
            rules_evaluated=rules_evaluated,
        )

    # Rule 2: Redaction check
    rules_evaluated.append("patch_redaction_safe")
    if not proposal.redaction_audit.safe:
        return PatchPolicyDecision(
            allowed=False,
            decision=PatchStatus.REJECTED,
            reasons=[
                f"Patch contains potentially sensitive tokens or credentials: {', '.join(proposal.redaction_audit.failed_checks)}"
            ],
            rules_evaluated=rules_evaluated,
        )

    # Rule 3: File count limit
    rules_evaluated.append("max_files_limit")
    max_files = int(cfg.get("max_files", 5))
    if len(files) > max_files:
        reasons.append(f"Patch modifies {len(files)} files, exceeding allowed maximum of {max_files}")

    # Rule 4: Total changed lines limit
    rules_evaluated.append("max_changed_lines_limit")
    max_changed_lines = int(cfg.get("max_changed_lines", 150))
    total_changed_lines = sum(
        sum(1 for line in hunk.lines if line.startswith(("+", "-")))
        for f in files
        for hunk in f.hunks
    )
    if total_changed_lines > max_changed_lines:
        reasons.append(
            f"Patch changes {total_changed_lines} lines, exceeding allowed maximum of {max_changed_lines}"
        )

    # Rule 5: File operations and protected path classifications
    rules_evaluated.append("file_operations_and_path_protections")
    for f in files:
        path = f.path.replace("\\", "/").strip("/")

        # Operations
        if f.operation == "add" and not cfg.get("allow_new_files", False):
            reasons.append(f"Creating new files is prohibited: {path}")
        if f.operation == "delete" and not cfg.get("allow_delete_files", False):
            reasons.append(f"Deleting files is prohibited: {path}")
        if f.operation == "rename" and not cfg.get("allow_rename_files", False):
            reasons.append(f"Renaming files is prohibited: {path}")

        # Path protection policies
        if is_test_file(path) and not cfg.get("allow_test_changes", False):
            reasons.append(f"Modifying test files is prohibited by policy: {path}")

        if is_workflow_path(path) and not cfg.get("allow_workflow_changes", False):
            reasons.append(f"Modifying CI/CD or workflow files is prohibited by policy: {path}")

        if is_lockfile_path(path) and not cfg.get("allow_lockfile_changes", False):
            reasons.append(f"Modifying dependency lockfiles is prohibited by policy: {path}")

        if is_dependency_manifest_path(path) and not cfg.get("allow_dependency_changes", False):
            reasons.append(f"Modifying dependency manifest files is prohibited by policy: {path}")

        if is_configuration_file(path) and not cfg.get("allow_configuration_changes", False):
            reasons.append(f"Modifying configuration files is prohibited by policy: {path}")

    if reasons:
        return PatchPolicyDecision(
            allowed=False,
            decision=PatchStatus.REJECTED,
            reasons=reasons,
            rules_evaluated=rules_evaluated,
        )

    # Rule 6: Human approval requirement
    rules_evaluated.append("require_human_approval")
    if cfg.get("require_human_approval", True):
        return PatchPolicyDecision(
            allowed=True,
            decision=PatchStatus.REQUIRES_HUMAN_APPROVAL,
            reasons=["Patch policy satisfied; human approval required prior to application"],
            rules_evaluated=rules_evaluated,
        )

    return PatchPolicyDecision(
        allowed=True,
        decision=PatchStatus.APPROVED,
        reasons=["Patch policy satisfied"],
        rules_evaluated=rules_evaluated,
    )


__all__ = [
    "DEFAULT_PATCH_CONFIG",
    "is_workflow_path",
    "is_lockfile_path",
    "is_dependency_manifest_path",
    "PatchPolicyDecision",
    "evaluate_patch_policy",
]
