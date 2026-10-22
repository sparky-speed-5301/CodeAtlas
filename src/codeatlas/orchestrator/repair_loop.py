"""Bounded repair generation and verification loop.

The loop is deliberately separate from explicit FixProposal application.  It
can generate and verify a candidate in an isolated validation path, but it
never approves, applies, commits, or changes the caller's workspace.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.adapters import select_language_adapter
from codeatlas.evidence import EvidenceLogger
from codeatlas.findings.models import Finding
from codeatlas.git.snapshot import Snapshot
from codeatlas.patching.validator import validate_patch_proposal
from codeatlas.providers import (
    DeterministicRepairProvider,
    RepairFinding,
    RepairLanguageCapabilities,
    RepairOutput,
    RepairPolicy,
    RepairProviderConfig,
    RepairProviderFailure,
    RepairRequest,
    StructuredRepairProvider,
    safe_test_paths,
)
from codeatlas.review.packet import ReviewPacket

from .manifest import RunManifest
from .repair import RepairOrchestrator
from .repair_models import RepairContext, RepairLimits, RepairRepositoryState, RepairResult


class RepairVerificationEvidence(BaseModel):
    """Bounded evidence collected for one candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["static_only", "verified", "failed", "blocked"]
    syntax: str = Field(default="not_run", max_length=32)
    formatter: str = Field(default="not_run", max_length=32)
    linter: str = Field(default="not_run", max_length=32)
    type_check: str = Field(default="not_run", max_length=32)
    tests: str = Field(default="not_run", max_length=32)
    diagnostics: tuple[str, ...] = Field(default=(), max_length=20)
    limitations: tuple[str, ...] = Field(default=(), max_length=20)
    commands_run: tuple[str, ...] = Field(default=(), max_length=8)


class RepairAttempt(BaseModel):
    """Stable attempt history without provider output or exception text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int = Field(ge=1, le=5)
    status: Literal["generated", "rejected", "retry", "accepted", "abstained"]
    reason: str = Field(max_length=64)
    provider_reason: str | None = Field(default=None, max_length=64)
    verification_status: str = Field(default="not_run", max_length=32)


class RepairLifecycleEvent(BaseModel):
    """Stable, bounded lifecycle evidence for one repair-loop operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int = Field(ge=1, le=5)
    stage: Literal[
        "proposal_requested",
        "proposal_rejected",
        "proposal_generated",
        "verification_blocked",
        "verification_started",
        "verification_completed",
        "retry_scheduled",
        "finished",
    ]
    status: Literal["observed", "blocked", "passed", "failed", "scheduled", "terminal"]
    reason: str = Field(default="", max_length=64)


class RepairLoopResult(BaseModel):
    """Safe result envelope for the bounded generation/verification loop."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted", "rejected", "retry_exhausted", "abstained"]
    reason: str = Field(default="", max_length=64)
    proposal_result: RepairResult | None = None
    proposal: Any = None
    validation: Any = None
    context: RepairContext | None = None
    verification: RepairVerificationEvidence | None = None
    attempts: tuple[RepairAttempt, ...] = Field(default=(), max_length=5)
    lifecycle: tuple[RepairLifecycleEvent, ...] = Field(default=(), max_length=24)
    attempt_count: int = Field(default=0, ge=0, le=5)
    final_stage: str = Field(default="finished", max_length=32)
    confidence: float = Field(default=0.0, ge=0, le=1)
    limitations: tuple[str, ...] = Field(default=(), max_length=24)
    approval_required: Literal[True] = True
    automatic_application_blocked: Literal[True] = True


def _request_for(finding: Finding, context: RepairContext, snapshot: Snapshot) -> RepairRequest:
    adapter = select_language_adapter(context.changed_file)
    if adapter is None:
        raise RepairProviderFailure("schema_failure")
    tools = adapter.report_tool_availability(snapshot.path)
    capability_limitations = (
        ("JavaScript/TypeScript syntax and type support is conservative when local toolchains are unavailable.",)
        if context.language in {"javascript", "typescript"} else (
            "Compiled-language syntax support is conservative when local toolchains are unavailable.",
        ) if context.language in {"go", "java", "rust"} else ()
    )
    test_runner_names = ("pytest", "vitest", "jest", "go_test", "maven", "gradle", "cargo")
    capabilities = RepairLanguageCapabilities(
        language=context.language,
        syntax_validation=True,
        formatter=bool(tools.get("formatter")),
        linter=bool(tools.get("ruff") or tools.get("eslint") or tools.get("linter")),
        type_checker=bool(tools.get("compiler") or tools.get("type_checker")),
        targeted_tests=bool(any(tools.get(name) for name in test_runner_names)),
        test_runners=tuple(name for name in test_runner_names if tools.get(name)),
        limitations=capability_limitations,
    )
    policy = RepairPolicy(
        policy_version=context.policy_version,
        max_target_files=context.limits.max_target_files,
        max_patch_lines=context.limits.max_patch_lines,
        max_patch_bytes=context.limits.max_patch_bytes,
        prohibited_paths=context.limits.prohibited_paths,
    )
    projection = RepairFinding(
        id=finding.id,
        file=finding.file,
        start_line=finding.start_line,
        end_line=finding.end_line,
        category=finding.category,
        claim=finding.claim,
        impact=finding.impact,
        quality_decision=finding.quality_decision,
        confidence=min(finding.confidence, context.confidence),
        evidence_strength=finding.evidence_strength,
        evidence=tuple(finding.evidence),
        limitations=tuple(finding.limitations[:10]),
    )
    return RepairRequest(
        finding=projection,
        context=context,
        policy=policy,
        capabilities=capabilities,
        target_file_limits=1,
        patch_size_limits=(context.limits.max_patch_lines, context.limits.max_patch_bytes),
    )


class _LoopProvider:
    """Adapt the structured provider to the existing static proposal pipeline."""

    network_access = False

    def __init__(self, primary: StructuredRepairProvider, fallback: StructuredRepairProvider | None, finding: Finding, snapshot: Snapshot) -> None:
        self.primary = primary
        self.fallback = fallback
        self.finding = finding
        self.snapshot = snapshot
        self.used_fallback = False
        self.last_failure: str | None = None
        self.last_output: RepairOutput | None = None
        self.name = str(getattr(primary, "name", "repair-provider"))[:128]
        self.version = str(getattr(primary, "version", "12.0.0"))[:128]

    def propose(self, context: RepairContext) -> dict[str, Any]:
        request = _request_for(self.finding, context, self.snapshot)
        provider: Any = self.primary
        try:
            output = provider.generate(request)
        except RepairProviderFailure as failure:
            self.last_failure = failure.code
            if self.fallback is None or self.used_fallback:
                raise
            self.used_fallback = True
            provider = self.fallback
            try:
                output = provider.generate(request)
            except RepairProviderFailure as fallback_failure:
                self.last_failure = fallback_failure.code
                raise
        except Exception:
            self.last_failure = "provider_unavailable"
            raise
        try:
            output = RepairOutput.model_validate(output.model_dump(mode="json") if isinstance(output, RepairOutput) else output)
        except Exception:
            self.last_failure = "schema_failure"
            raise RepairProviderFailure("schema_failure")
        self.last_output = output
        assumptions = list(output.assumptions)
        limitations = list(output.limitations)
        if assumptions:
            limitations.append("Provider assumptions require independent human review: " + "; ".join(assumptions[:4]))
        return {
            "suggestion_id": f"repair-{context.finding_id}",
            "finding_id": context.finding_id,
            "unified_diff": output.patch,
            "rationale": f"{output.diagnosis}\n{output.explanation}"[:2_000],
            "expected_behavior": output.expected_behavior_change,
            "target_files": list(output.target_files),
            "risk_level": output.risk_level,
            "limitations": limitations[:10],
            "provider_provenance": {
                "origin": "repair_provider",
                "provider": self.name,
                "model": str(getattr(getattr(provider, "config", None), "model_name", "unknown"))[:128],
            },
        }

    def generate(self, request: object) -> object:
        """Satisfy the existing provider protocol without bypassing ``propose``."""
        if not isinstance(request, RepairRequest):
            raise RepairProviderFailure("schema_failure")
        return self.primary.generate(request)


class RepairVerificationLoop:
    """Generate at most N candidates and verify each through existing policy."""

    def __init__(
        self,
        provider: StructuredRepairProvider,
        *,
        config: RepairProviderConfig | None = None,
        fallback_provider: StructuredRepairProvider | None = None,
        limits: RepairLimits | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or RepairProviderConfig(
            provider_name=str(getattr(provider, "name", "offline")),
            mode="offline" if not getattr(provider, "network_access", False) else "online",
            enabled=bool(getattr(provider, "network_access", False)),
        )
        self.fallback_provider = fallback_provider
        self.limits = limits or RepairLimits()

    def run(
        self,
        finding: Finding,
        *,
        packet: ReviewPacket,
        manifest: RunManifest,
        state: RepairRepositoryState,
        snapshot: Snapshot,
        fix_eligible: bool = False,
        evidence: EvidenceLogger | None = None,
        approval_token: str | None = None,
        execute_checks: bool = False,
        run_tests: bool = False,
    ) -> RepairLoopResult:
        attempts: list[RepairAttempt] = []
        lifecycle: list[RepairLifecycleEvent] = []
        limitations: list[str] = [
            "Provider output is untrusted and has no authority over policy, approval, or execution.",
            "Automatic application is permanently blocked by this operation.",
        ]
        max_attempts = min(self.config.max_repair_attempts, 5)
        fallback: StructuredRepairProvider | None = self.fallback_provider
        if fallback is None and self.config.deterministic_fallback and getattr(self.provider, "network_access", False):
            # The built-in fallback abstains; it never fabricates a patch.  A
            # caller can replace it with an explicitly deterministic fixture.
            fallback = DeterministicRepairProvider()
        last_result: RepairResult | None = None
        last_provider_reason: str | None = None
        last_verification: RepairVerificationEvidence | None = None

        for number in range(1, max_attempts + 1):
            lifecycle.append(RepairLifecycleEvent(
                attempt=number, stage="proposal_requested", status="observed",
            ))
            adapter = _LoopProvider(self.provider, fallback, finding, snapshot)
            result = RepairOrchestrator(adapter, limits=self.limits).plan(
                finding,
                packet=packet,
                manifest=manifest,
                state=state,
                snapshot=snapshot,
                fix_eligible=fix_eligible,
                evidence=evidence,
            )
            last_result = result
            last_provider_reason = adapter.last_failure
            if result.status in {"rejected", "review_only"} and result.proposal is None:
                provider_reason = adapter.last_failure
                reason = provider_reason or result.reason
                lifecycle.append(RepairLifecycleEvent(
                    attempt=number, stage="proposal_rejected",
                    status="blocked" if result.status == "review_only" else "failed",
                    reason=reason,
                ))
                if provider_reason and provider_reason in {"network_disabled", "secret_detected", "budget_exceeded"}:
                    attempts.append(RepairAttempt(attempt=number, status="abstained", reason=reason, provider_reason=provider_reason))
                    return self._finish("abstained", reason, result, attempts, last_verification, limitations, lifecycle=lifecycle)
                if provider_reason and provider_reason in {
                    "provider_unavailable", "timeout", "rate_limit", "malformed_response", "schema_failure", "retry_exhausted",
                }:
                    retry = number < max_attempts
                    attempts.append(RepairAttempt(attempt=number, status="retry" if retry else "rejected", reason=reason, provider_reason=provider_reason))
                    if retry:
                        lifecycle.append(RepairLifecycleEvent(
                            attempt=number, stage="retry_scheduled", status="scheduled", reason=reason,
                        ))
                        continue
                    return self._finish("retry_exhausted", "retry_exhausted", result, attempts, last_verification, limitations, lifecycle=lifecycle)
                attempts.append(RepairAttempt(attempt=number, status="abstained" if result.status == "review_only" else "rejected", reason=reason))
                return self._finish("abstained" if result.status == "review_only" else "rejected", reason, result, attempts, last_verification, limitations, lifecycle=lifecycle)

            if result.proposal is None or result.validation is None:
                lifecycle.append(RepairLifecycleEvent(
                    attempt=number, stage="proposal_rejected", status="failed",
                    reason=result.reason or "proposal_missing",
                ))
                attempts.append(RepairAttempt(attempt=number, status="rejected", reason=result.reason or "proposal_missing"))
                return self._finish("rejected", result.reason or "proposal_missing", result, attempts, last_verification, limitations, lifecycle=lifecycle)

            output = adapter.last_output
            if output is None:
                lifecycle.append(RepairLifecycleEvent(
                    attempt=number, stage="proposal_rejected", status="failed",
                    reason="provider_output_missing",
                ))
                attempts.append(RepairAttempt(attempt=number, status="rejected", reason="provider_output_missing"))
                return self._finish("rejected", "provider_output_missing", result, attempts, last_verification, limitations, lifecycle=lifecycle)
            # Trusted CodeAtlas confidence is the upper bound.  Provider claims
            # can only lower it and can never upgrade finding quality/severity.
            trusted_confidence = min(finding.confidence, result.context.confidence if result.context else finding.confidence)
            confidence = min(output.confidence, trusted_confidence)
            result.proposal.provenance.update({
                "context_hash": result.context.context_hash if result.context else "",
                "provider_confidence": output.confidence,
                "trusted_confidence": trusted_confidence,
                "final_confidence": confidence,
                "assumptions": list(output.assumptions)[:8],
                "confidence_clamped": output.confidence > trusted_confidence,
                "tests_to_run_are_untrusted_paths": True,
            })
            selected_tests = safe_test_paths(output.tests_to_run)
            if any(path not in (result.context.related_tests if result.context else ()) for path in selected_tests):
                limitations.append("Provider-selected tests outside deterministic context were ignored.")

            validation = result.validation
            if execute_checks:
                if not approval_token:
                    lifecycle.append(RepairLifecycleEvent(
                        attempt=number, stage="verification_blocked", status="blocked",
                        reason="approval_required",
                    ))
                    last_verification = RepairVerificationEvidence(
                        status="blocked",
                        syntax="passed" if validation.syntax_valid else "not_verified",
                        limitations=("Explicit validation approval is required before isolated checks.",),
                    )
                    attempts.append(RepairAttempt(attempt=number, status="abstained", reason="approval_required", verification_status="blocked"))
                    return self._finish("abstained", "approval_required", result, attempts, last_verification, limitations, lifecycle=lifecycle)
                lifecycle.append(RepairLifecycleEvent(
                    attempt=number, stage="verification_started", status="observed",
                ))
                validation = validate_patch_proposal(
                    result.proposal,
                    snapshot.path,
                    config={"patch": {"max_files": 1, "max_changed_lines": self.limits.max_patch_lines, "max_patch_bytes": self.limits.max_patch_bytes}},
                    approval_token=approval_token,
                    allow_isolated_apply=True,
                    evidence=evidence,
                    run_id=state.run_id,
                    run_tests=run_tests,
                )
                result.validation = validation
                lifecycle.append(RepairLifecycleEvent(
                    attempt=number, stage="verification_completed",
                    status="passed" if validation.valid else "failed",
                    reason="validated" if validation.valid else "verification_failed",
                ))
            else:
                lifecycle.append(RepairLifecycleEvent(
                    attempt=number, stage="proposal_generated", status="passed",
                    reason="static_validation_passed",
                ))
            last_verification = self._verification(validation, result, execute_checks, run_tests)
            if validation.valid and (not execute_checks or validation.tests_status in {"not_run", "passed"}):
                attempts.append(RepairAttempt(attempt=number, status="accepted", reason="verified" if execute_checks else "static_validation_passed", verification_status=last_verification.status))
                return self._finish("accepted", "verified" if execute_checks else "static_validation_passed", result, attempts, last_verification, limitations, confidence=confidence, lifecycle=lifecycle)
            retry = number < max_attempts
            attempts.append(RepairAttempt(attempt=number, status="retry" if retry else "rejected", reason="verification_failed", verification_status=last_verification.status))
            if retry:
                lifecycle.append(RepairLifecycleEvent(
                    attempt=number, stage="retry_scheduled", status="scheduled", reason="verification_failed",
                ))
                continue
            return self._finish("retry_exhausted", "verification_failed", result, attempts, last_verification, limitations, confidence=confidence, lifecycle=lifecycle)

        return self._finish("retry_exhausted", last_provider_reason or "retry_exhausted", last_result, attempts, last_verification, limitations, lifecycle=lifecycle)

    @staticmethod
    def _verification(validation: Any, result: RepairResult, executed: bool, run_tests: bool) -> RepairVerificationEvidence:
        diagnostics = tuple(str(item)[:512] for item in (validation.errors or validation.warnings)[:20])
        planned = tuple(" ".join(command)[:512] for command in result.checks_planned[:8])
        formatter = "not_available"
        linter = "not_available"
        type_check = "not_available"
        for command in result.checks_planned:
            executable = command[0] if command else ""
            if executable in {"black", "prettier"} or (executable == "ruff" and "format" in command):
                formatter = "planned_not_run"
            if executable in {"ruff", "eslint"}:
                linter = "planned_not_run"
            if executable in {"tsc", "mypy", "pyright"}:
                type_check = "planned_not_run"
        check_limitation = (
            "Available formatter, linter, and type checks are planned through the existing command policy; "
            "they require an explicit isolated-validation execution request."
        ) if planned else ""
        limitations = list(result.limitations[:20])
        if check_limitation:
            limitations.append(check_limitation)
        if not executed:
            return RepairVerificationEvidence(
                status="static_only",
                syntax="passed" if validation.syntax_valid is True else "not_verified",
                formatter=formatter,
                linter=linter,
                type_check=type_check,
                limitations=tuple(dict.fromkeys(limitations))[:20],
                diagnostics=diagnostics,
                commands_run=planned,
            )
        failed = bool(validation.errors) or (run_tests and validation.tests_status not in {"not_run", "passed"})
        return RepairVerificationEvidence(
            status="failed" if failed else "verified",
            syntax="passed" if validation.syntax_valid is True else "not_verified",
            formatter=formatter,
            linter=linter,
            type_check=type_check,
            tests=validation.tests_status,
            diagnostics=diagnostics,
            limitations=tuple(dict.fromkeys(limitations))[:20],
            commands_run=tuple(validation.commands_run[:8]),
        )

    @staticmethod
    def _finish(
        status: Literal["accepted", "rejected", "retry_exhausted", "abstained"],
        reason: str,
        result: RepairResult | None,
        attempts: list[RepairAttempt],
        verification: RepairVerificationEvidence | None,
        limitations: list[str],
        *,
        confidence: float = 0.0,
        lifecycle: list[RepairLifecycleEvent] | None = None,
    ) -> RepairLoopResult:
        history = list(lifecycle or ())
        attempt = attempts[-1].attempt if attempts else 1
        history.append(RepairLifecycleEvent(
            attempt=attempt,
            stage="finished",
            status="terminal",
            reason=reason,
        ))
        return RepairLoopResult(
            status=status,
            reason=reason,
            proposal_result=result,
            proposal=result.proposal if result else None,
            validation=result.validation if result else None,
            context=result.context if result else None,
            verification=verification,
            attempts=tuple(attempts),
            lifecycle=tuple(history[-24:]),
            attempt_count=len(attempts),
            final_stage="finished",
            confidence=confidence,
            limitations=tuple(dict.fromkeys(limitations))[:24],
        )


# Short aliases for integrations and documentation.
RepairLoop = RepairVerificationLoop


__all__ = [
    "RepairAttempt",
    "RepairLifecycleEvent",
    "RepairLoop",
    "RepairLoopResult",
    "RepairVerificationEvidence",
    "RepairVerificationLoop",
]
