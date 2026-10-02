"""Test runner protocol and concrete language implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from codeatlas.evidence import EvidenceLogger

from .command_policy import validate_test_command
from .discovery import discover_test_plan
from .executor import execute_test_command
from .models import CommandValidation, ResourceLimits, TestPlan, TestResult


@runtime_checkable
class TestRunner(Protocol):
    """Protocol defining test discovery, command validation, and sandboxed execution."""

    name: str
    supported_languages: set[str]

    def discover(
        self,
        sandbox: Any,
        changed_files: Sequence[str],
        index: Any = None,
    ) -> TestPlan:
        """Discover targeted tests and formulate a test plan."""
        ...

    def validate_command(
        self,
        command: Sequence[str] | str,
        policy: dict[str, Any] | None = None,
    ) -> CommandValidation:
        """Validate a proposed test command against strict allowlists."""
        ...

    def run(
        self,
        sandbox: Any,
        test_plan: TestPlan,
        limits: ResourceLimits | None = None,
    ) -> TestResult:
        """Execute the test plan in the provided sandbox worktree."""
        ...


TestRunner.__test__ = False


class BaseTestRunner:
    """Base test runner providing standard validation and execution."""

    __test__ = False
    name: str = "base"
    supported_languages: set[str] = set()

    def __init__(self, evidence: EvidenceLogger | None = None, extra_tokens: Sequence[str] = ()):
        self.evidence = evidence
        self.extra_tokens = list(extra_tokens)

    def _sandbox_path(self, sandbox: Any) -> Path:
        if hasattr(sandbox, "path"):
            return Path(sandbox.path)
        return Path(sandbox)

    def _sandbox_id(self, sandbox: Any) -> str | None:
        if hasattr(sandbox, "sandbox_id"):
            return str(sandbox.sandbox_id)
        return None

    def validate_command(
        self,
        command: Sequence[str] | str,
        policy: dict[str, Any] | None = None,
    ) -> CommandValidation:
        return validate_test_command(command, policy=policy)

    def run(
        self,
        sandbox: Any,
        test_plan: TestPlan,
        limits: ResourceLimits | None = None,
    ) -> TestResult:
        sandbox_path = self._sandbox_path(sandbox)
        sandbox_id = self._sandbox_id(sandbox)
        res_limits = limits or ResourceLimits(
            timeout_seconds=test_plan.timeout,
            network_allowed=(test_plan.network_policy == "allowed"),
            dependency_install_allowed=(test_plan.dependency_install_policy == "allowed"),
        )

        # Enforce command validation before execution
        val = self.validate_command(test_plan.exact_command)
        if not val.allowed:
            if self.evidence is not None:
                self.evidence.emit(
                    "test_execution_blocked",
                    sandbox_id=sandbox_id,
                    reason=val.rejection_reason or "command_validation_failed",
                    status="blocked",
                )
            return TestResult(
                status="blocked",
                tests_run=[],
                commands_run=[" ".join(test_plan.exact_command)] if test_plan.exact_command else [],
                exit_code=None,
                duration_ms=0.0,
                failures=[val.rejection_reason or "Command validation rejected test command"],
                execution_allowed=False,
                network_allowed=res_limits.network_allowed,
                dependency_install_allowed=res_limits.dependency_install_allowed,
                resource_limits=res_limits.model_dump(mode="json"),
                sandbox_id=sandbox_id,
            )

        # Check working directory safety
        target_cwd = sandbox_path / test_plan.working_directory
        try:
            target_cwd.resolve().relative_to(sandbox_path.resolve())
        except ValueError:
            return TestResult(
                status="blocked",
                tests_run=[],
                commands_run=[" ".join(test_plan.exact_command)] if test_plan.exact_command else [],
                exit_code=None,
                duration_ms=0.0,
                failures=[f"Working directory '{test_plan.working_directory}' resolves outside sandbox"],
                execution_allowed=False,
                resource_limits=res_limits.model_dump(mode="json"),
                sandbox_id=sandbox_id,
            )

        return execute_test_command(
            sandbox_path,
            test_plan.exact_command,
            limits=res_limits,
            evidence=self.evidence,
            sandbox_id=sandbox_id,
            extra_tokens=self.extra_tokens,
            test_targets=test_plan.test_targets,
        )


class PytestTestRunner(BaseTestRunner):
    """Targeted pytest runner for Python."""

    name: str = "pytest"
    supported_languages: set[str] = {"python"}

    def discover(
        self,
        sandbox: Any,
        changed_files: Sequence[str],
        index: Any = None,
    ) -> TestPlan:
        sandbox_path = self._sandbox_path(sandbox)
        plan, blocked = discover_test_plan(
            sandbox_path,
            changed_files,
            index=index,
            runner_preference="pytest",
        )
        if blocked and not plan.limitations:
            plan.limitations.append(blocked)
        return plan


class UnittestTestRunner(BaseTestRunner):
    """Targeted unittest runner for Python standard library tests."""

    name: str = "unittest"
    supported_languages: set[str] = {"python"}

    def discover(
        self,
        sandbox: Any,
        changed_files: Sequence[str],
        index: Any = None,
    ) -> TestPlan:
        sandbox_path = self._sandbox_path(sandbox)
        plan, blocked = discover_test_plan(
            sandbox_path,
            changed_files,
            index=index,
            runner_preference="unittest",
        )
        if blocked and not plan.limitations:
            plan.limitations.append(blocked)
        return plan


class VitestTestRunner(BaseTestRunner):
    """Vitest runner for TypeScript and JavaScript."""

    name: str = "vitest"
    supported_languages: set[str] = {"typescript", "javascript"}

    def discover(
        self,
        sandbox: Any,
        changed_files: Sequence[str],
        index: Any = None,
    ) -> TestPlan:
        sandbox_path = self._sandbox_path(sandbox)
        plan, blocked = discover_test_plan(
            sandbox_path,
            changed_files,
            index=index,
            runner_preference="vitest",
        )
        if blocked and not plan.limitations:
            plan.limitations.append(blocked)
        return plan


class JestTestRunner(BaseTestRunner):
    """Jest runner for TypeScript and JavaScript."""

    name: str = "jest"
    supported_languages: set[str] = {"typescript", "javascript"}

    def discover(
        self,
        sandbox: Any,
        changed_files: Sequence[str],
        index: Any = None,
    ) -> TestPlan:
        sandbox_path = self._sandbox_path(sandbox)
        plan, blocked = discover_test_plan(
            sandbox_path,
            changed_files,
            index=index,
            runner_preference="jest",
        )
        if blocked and not plan.limitations:
            plan.limitations.append(blocked)
        return plan


def get_test_runner(
    language: str,
    preference: str | None = None,
    evidence: EvidenceLogger | None = None,
    extra_tokens: Sequence[str] = (),
) -> TestRunner | None:
    """Retrieve an approved test runner for the given language and preference."""
    lang = language.lower()
    pref = (preference or "").lower()

    if lang == "python":
        if pref == "unittest":
            return UnittestTestRunner(evidence=evidence, extra_tokens=extra_tokens)
        return PytestTestRunner(evidence=evidence, extra_tokens=extra_tokens)

    if lang in {"typescript", "javascript"}:
        if pref == "jest":
            return JestTestRunner(evidence=evidence, extra_tokens=extra_tokens)
        return VitestTestRunner(evidence=evidence, extra_tokens=extra_tokens)

    return None


__all__ = [
    "TestRunner",
    "BaseTestRunner",
    "PytestTestRunner",
    "UnittestTestRunner",
    "VitestTestRunner",
    "JestTestRunner",
    "get_test_runner",
]
