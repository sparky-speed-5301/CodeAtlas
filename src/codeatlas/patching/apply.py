"""Isolated patch application and validation within ephemeral worktree sandboxes.

Phase 7C: this is the only path that may apply a patch, and only after a
valid scoped approval token.  The sandbox is a fresh detached worktree that is
always cleaned up (unless retention was explicitly requested for debugging).
Repository tests, builds, package managers, and arbitrary commands are never
executed here; the only operations are narrowly scoped git plumbing and
standard-library Python syntax parsing.
"""

from __future__ import annotations

import ast
import hashlib
import time
from pathlib import Path
from typing import Any

from codeatlas.evidence import EvidenceLogger
from codeatlas.git.errors import SnapshotCleanupError
from codeatlas.git.executable import run_git
from codeatlas.git.refs import resolve_ref
from codeatlas.verification.command_policy import validate_test_command
from codeatlas.verification.discovery import discover_test_plan
from codeatlas.verification.executor import execute_test_command
from codeatlas.verification.models import ResourceLimits, TestPlan, TestResult

from .models import PatchProposal, PatchStatus, PatchValidationResult
from .parser import parse_unified_diff
from .policy import evaluate_patch_policy
from .proposal import audit_patch_redaction, compute_patch_hash, verify_approval_token
from .sandbox import temporary_patch_sandbox


def _emit(evidence: EvidenceLogger | None, event: str, **details: Any) -> None:
    if evidence is not None:
        evidence.emit(event, **details)


def _resulting_diff_stats(sandbox_path: Path) -> tuple[list[tuple[str, str, str]], str]:
    """Return numstat entries and a hash of the sandbox's unstaged diff."""
    diff_res = run_git(["diff"], cwd=sandbox_path, check=False)
    diff_text = diff_res.stdout
    diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    numstat = run_git(["diff", "--numstat"], cwd=sandbox_path, check=False)
    entries: list[tuple[str, str, str]] = []
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            entries.append((parts[0], parts[1], parts[2].replace("\\", "/").strip()))
    return entries, diff_hash


def apply_patch_in_isolated_sandbox(
    proposal: PatchProposal,
    repository_root: Path,
    *,
    approval_token: str | None = None,
    config: dict[str, Any] | None = None,
    skip_approval_check: bool = False,
    evidence: EvidenceLogger | None = None,
    run_id: str = "default",
    retain_sandbox_on_failure: bool = False,
    run_tests: bool = False,
    run_full_suite: bool = False,
    test_timeout: float = 30.0,
    max_output_bytes: int = 100_000,
    test_config: dict[str, Any] | None = None,
    validation_scope: dict[str, Any] | None = None,
) -> PatchValidationResult:
    """Apply a patch proposal inside an ephemeral detached worktree sandbox.

    Guarantees:
    - Requires a valid scoped approval token (proposal id, base commit, patch
      hash, target files, run scope) unless the proposal is already approved.
    - Never modifies the original working tree.
    - Cleans up the sandbox in all paths; retention only on explicit opt-in
      after a failure.
    - When run_tests and run_full_suite are False (default): Never executes repository
      tests, builds, package managers, or arbitrary commands; records
      tests_status/build_status as not_run and execution_allowed as False.
    - When run_tests is True: Executes approved targeted tests inside the detached
      sandbox only after patch application and syntax validation succeed.
    - When run_full_suite is True: Executes approved full repository test suite
      inside detached sandbox only after policy opt-in, approval, patch application,
      and syntax validation succeed. Note: passing full suite does not prove patch correctness.
    """
    started = time.perf_counter()
    errors: list[str] = []
    warnings: list[str] = []
    commands_run: list[str] = []
    tests_status = "not_run"
    tests_run_list: list[str] = []
    test_plan_dict: dict[str, Any] | None = None
    test_runner_name: str | None = None
    test_discovery_reason: str | None = None
    test_exit_code: int | None = None
    test_duration_ms: float | None = None
    test_failures: list[str] = []
    test_output_redaction_audit: dict[str, Any] | None = None
    network_allowed = False
    dependency_install_allowed = False
    resource_limits: dict[str, Any] | None = None
    test_execution_attempted = False
    test_execution_blocked_reason: str | None = None
    test_stdout_summary: str | None = None
    test_stderr_summary: str | None = None
    diagnostic_summary: str | None = None
    failed_test_names: list[str] = []
    test_failure_count: int | None = None
    test_pass_count: int | None = None
    test_skip_count: int | None = None
    test_output_truncated: bool | None = None
    network_policy_requested: str | None = "disabled" if (run_tests or run_full_suite) else None
    network_policy_enforced: bool | None = True if (run_tests or run_full_suite) else None
    network_isolation_verified: bool | None = False if (run_tests or run_full_suite) else None
    diagnostic_limitations: list[str] = []
    stack_trace_summary: str | None = None
    execution_allowed = False

    # Phase 8B-2 full-suite fields
    t_cfg = test_config or {}
    full_suite_requested = run_full_suite
    full_suite_policy_opted_in = bool(
        t_cfg.get("allow_full_suite")
        or t_cfg.get("full_suite_opt_in")
        or (config.get("allow_full_suite") if isinstance(config, dict) else False)
        or (config.get("full_suite_opt_in") if isinstance(config, dict) else False)
        or (config.get("test", {}).get("allow_full_suite") if isinstance(config, dict) and isinstance(config.get("test"), dict) else False)
        or (config.get("test", {}).get("full_suite_opt_in") if isinstance(config, dict) and isinstance(config.get("test"), dict) else False)
    )
    full_suite_status: str | None = "blocked" if run_full_suite else "not_run"
    full_suite_blocked_reason: str | None = None
    full_suite_command: list[str] | None = None
    full_suite_result: dict[str, Any] | None = None
    test_result: TestResult | None = None
    full_suite_test_plan: TestPlan | None = None

    def finish(**overrides: Any) -> PatchValidationResult:
        nonlocal full_suite_blocked_reason
        if run_full_suite and full_suite_status == "blocked" and not full_suite_blocked_reason:
            if errors:
                full_suite_blocked_reason = errors[0]
        payload: dict[str, Any] = {
            "commands_run": commands_run,
            "execution_allowed": execution_allowed,
            "proposal_id": proposal.proposal_id,
            "base_commit": proposal.base_commit,
            "patch_hash": proposal.patch_hash or None,
            "policy_decision": proposal.policy_decision,
            "redaction_audit": proposal.redaction_audit.model_dump(mode="json"),
            "duration_ms": (time.perf_counter() - started) * 1000.0,
            "cleanup_status": "not_applicable",
            "sandbox_retained": False,
            "tests_status": tests_status,
            "tests_run": tests_run_list,
            "test_plan": test_plan_dict,
            "test_runner": test_runner_name,
            "test_discovery_reason": test_discovery_reason,
            "test_exit_code": test_exit_code,
            "test_duration_ms": test_duration_ms,
            "test_failures": test_failures,
            "test_timeout": test_timeout if (run_tests or run_full_suite) else None,
            "test_output_redaction_audit": test_output_redaction_audit,
            "network_allowed": network_allowed,
            "dependency_install_allowed": dependency_install_allowed,
            "resource_limits": resource_limits,
            "test_execution_attempted": test_execution_attempted,
            "test_execution_blocked_reason": test_execution_blocked_reason,
            "test_stdout_summary": test_stdout_summary,
            "test_stderr_summary": test_stderr_summary,
            "diagnostic_summary": diagnostic_summary,
            "failed_test_names": failed_test_names,
            "test_failure_count": test_failure_count,
            "test_pass_count": test_pass_count,
            "test_skip_count": test_skip_count,
            "test_output_truncated": test_output_truncated,
            "network_policy_requested": network_policy_requested,
            "network_policy_enforced": network_policy_enforced,
            "network_isolation_verified": network_isolation_verified,
            "diagnostic_limitations": diagnostic_limitations,
            "stack_trace_summary": stack_trace_summary,
            "full_suite_requested": full_suite_requested,
            "full_suite_policy_opted_in": full_suite_policy_opted_in,
            "full_suite_status": full_suite_status,
            "full_suite_blocked_reason": full_suite_blocked_reason,
            "full_suite_command": full_suite_command,
            "full_suite_result": full_suite_result,
            "test_result": test_result,
            "full_suite_test_plan": full_suite_test_plan,
            "approval_scope": run_id,
        }
        payload.update(overrides)
        result = PatchValidationResult(**payload)
        _emit(
            evidence,
            "patch_validation_completed",
            proposal_id=proposal.proposal_id,
            status="ok" if result.valid else "failed",
            valid=result.valid,
            duration_ms=result.duration_ms,
        )
        return result

    _emit(
        evidence,
        "patch_validation_requested",
        proposal_id=proposal.proposal_id,
        status="requested",
    )

    # 0. Lifecycle state guards: rejected or already-applied/validated
    #    proposals may not re-enter isolated validation by default.
    if proposal.status == PatchStatus.REJECTED:
        _emit(
            evidence,
            "patch_validation_rejected",
            proposal_id=proposal.proposal_id,
            reason="proposal has been rejected",
            status="rejected",
        )
        return finish(
            valid=False,
            approval_verified=False,
            errors=["Proposal has been rejected; no further transitions are allowed"],
        )
    if proposal.status in {PatchStatus.APPLIED_IN_ISOLATED_WORKTREE, PatchStatus.VALIDATED}:
        _emit(
            evidence,
            "patch_validation_rejected",
            proposal_id=proposal.proposal_id,
            reason=f"proposal already in terminal-applied state '{proposal.status}'",
            status="rejected",
        )
        return finish(
            valid=False,
            approval_verified=False,
            errors=[
                f"Proposal status is '{proposal.status}'; isolated validation cannot run again "
                "unless policy explicitly allows revalidation"
            ],
        )

    # 1. Re-run the patch redaction audit (never trust a stored audit alone).
    fresh_redaction = audit_patch_redaction(proposal.unified_diff)
    proposal.redaction_audit = fresh_redaction
    if not fresh_redaction.safe:
        _emit(
            evidence,
            "patch_validation_rejected",
            proposal_id=proposal.proposal_id,
            reason="patch contains unredacted credential material",
            status="redaction_failed",
        )
        return finish(
            valid=False,
            applies_cleanly=False,
            errors=["Patch contains unredacted credentials or sensitive tokens; isolated application blocked"],
            security_check_status="failed",
            approval_verified=False,
        )

    # 2. Verify patch hash integrity
    computed_hash = compute_patch_hash(proposal.unified_diff)
    if proposal.patch_hash and computed_hash != proposal.patch_hash:
        return finish(
            valid=False,
            applies_cleanly=False,
            approval_verified=False,
            errors=[
                f"Patch hash mismatch: proposal hash '{proposal.patch_hash}' != computed hash '{computed_hash}'"
            ],
        )

    # 3. Best-effort structural parse for policy and target verification.
    patch_cfg = config.get("patch", config) if isinstance(config, dict) and isinstance(config.get("patch", config), dict) else {}
    parsed_files, _parse_errors = parse_unified_diff(
        proposal.unified_diff,
        max_patch_bytes=int(patch_cfg.get("max_patch_bytes", 500_000)) if isinstance(patch_cfg, dict) else 500_000,
        max_files=int(patch_cfg.get("max_files", 5)) if isinstance(patch_cfg, dict) else 5,
        max_changed_lines=int(patch_cfg.get("max_changed_lines", 150)) if isinstance(patch_cfg, dict) else 150,
    )

    # 4. Re-run patch policy (protected paths, operations, budgets).
    policy_dec = evaluate_patch_policy(proposal, parsed_files, config=config)
    proposal.policy_decision = policy_dec.model_dump()
    if policy_dec.decision == PatchStatus.REJECTED:
        _emit(
            evidence,
            "patch_validation_rejected",
            proposal_id=proposal.proposal_id,
            reason="; ".join(policy_dec.reasons[:1]) or "patch policy rejected",
            status="rejected",
        )
        return finish(
            valid=False,
            applies_cleanly=False,
            approval_verified=False,
            errors=policy_dec.reasons or ["Patch policy rejected the proposal"],
        )

    # 5. Approval token: the only authorization for isolated validation.
    _emit(
        evidence,
        "approval_verification_started",
        proposal_id=proposal.proposal_id,
        status="started",
    )
    approval_verified = False
    if validation_scope is None and (skip_approval_check or proposal.status == PatchStatus.APPROVED):
        approval_verified = True
        warnings.append("Approval check skipped: proposal is already approved by policy state")
    else:
        if not approval_token:
            _emit(
                evidence,
                "approval_verification_failed",
                proposal_id=proposal.proposal_id,
                reason="missing approval token",
                status="failed",
            )
            return finish(
                valid=False,
                applies_cleanly=False,
                approval_verified=False,
                errors=[
                    "Patch application requires explicit human approval token; status is currently "
                    f"'{proposal.status}'"
                ],
            )
        is_valid_token, token_err = verify_approval_token(
            approval_token, proposal, proposal.base_commit, run_id=run_id,
            validation_scope=validation_scope,
        )
        if not is_valid_token:
            _emit(
                evidence,
                "approval_verification_failed",
                proposal_id=proposal.proposal_id,
                reason=token_err or "invalid approval token",
                status="failed",
            )
            return finish(
                valid=False,
                applies_cleanly=False,
                approval_verified=False,
                errors=[token_err or "Invalid approval token"],
            )
        approval_verified = True

    _emit(
        evidence,
        "approval_verified",
        proposal_id=proposal.proposal_id,
        status="verified",
    )

    # 5b. Phase 8B-2: Enforce repository policy opt-in gate for full-suite execution
    if run_full_suite and not full_suite_policy_opted_in:
        block_msg = "Full-suite execution is disabled by repository policy; explicit repository policy opt-in is required"
        _emit(
            evidence,
            "full_suite_blocked",
            proposal_id=proposal.proposal_id,
            reason=block_msg,
            status="blocked",
        )
        return finish(
            valid=False,
            applies_cleanly=False,
            approval_verified=approval_verified,
            full_suite_requested=True,
            full_suite_policy_opted_in=False,
            full_suite_status="blocked",
            full_suite_blocked_reason=block_msg,
            errors=[f"Full-suite execution blocked: {block_msg}"],
        )

    # Operator approval authorizes the proposal for isolated validation.
    if proposal.status in {PatchStatus.PROPOSED, PatchStatus.REQUIRES_HUMAN_APPROVAL}:
        proposal.transition_to(PatchStatus.APPROVED)

    # 6. Verify base commit exists
    try:
        resolved = resolve_ref(repository_root, proposal.base_commit)
    except Exception as err:
        return finish(
            valid=False,
            applies_cleanly=False,
            approval_verified=approval_verified,
            errors=[f"Base commit '{proposal.base_commit}' could not be resolved: {err}"],
        )

    # 7. Record original worktree status to verify zero mutation
    initial_status = run_git(["status", "--porcelain"], cwd=repository_root, check=False)
    commands_run.append("git status (pre-check)")

    sandbox_id: str | None = None
    cleanup_status = "not_applicable"
    sandbox_retained = False
    applied_cleanly = False
    syntax_valid: bool | None = None
    changed_files: list[str] = []
    resulting_diff_hash: str | None = None
    failure = False

    try:
        sandbox_ctx = temporary_patch_sandbox(
            repository_root, proposal.base_commit, retain_on_failure=retain_sandbox_on_failure
        )
        with sandbox_ctx as sandbox:
            sandbox_id = sandbox.sandbox_id
            commands_run.append("git worktree add --detach")
            _emit(evidence, "sandbox_created", sandbox_id=sandbox_id, status="created")

            # 8. The sandbox must start from the requested base commit and be clean.
            if sandbox.base_commit != resolved.commit:
                errors.append(
                    f"Sandbox base commit mismatch: expected '{resolved.commit}', got '{sandbox.base_commit}'"
                )
                failure = True
            clean_res = run_git(["status", "--porcelain"], cwd=sandbox.path, check=False)
            commands_run.append("git status (sandbox pre-check)")
            if clean_res.stdout.strip():
                errors.append("Sandbox worktree is not clean at start")
                failure = True

            diff_input = (
                proposal.unified_diff
                if proposal.unified_diff.endswith("\n")
                else proposal.unified_diff + "\n"
            )

            if not failure:
                # 9. Check patch applicability without modifying files first
                _emit(evidence, "patch_check_started", sandbox_id=sandbox_id, status="started")
                check_res = run_git(
                    ["apply", "--check", "--whitespace=nowarn", "-"],
                    cwd=sandbox.path,
                    input=diff_input,
                    check=False,
                )
                commands_run.append("git apply --check")
                _emit(
                    evidence,
                    "patch_check_completed",
                    sandbox_id=sandbox_id,
                    status="ok" if check_res.returncode == 0 else "failed",
                )

                if check_res.returncode != 0:
                    err_msg = check_res.stderr.strip() or check_res.stdout.strip() or "Patch does not apply cleanly"
                    errors.append(f"Patch conflict / apply error: {err_msg}")
                    failure = True
                else:
                    # 10. Apply patch to detached sandbox
                    apply_res = run_git(
                        ["apply", "--whitespace=nowarn", "-"],
                        cwd=sandbox.path,
                        input=diff_input,
                        check=False,
                    )
                    commands_run.append("git apply")

                    if apply_res.returncode != 0:
                        err_msg = apply_res.stderr.strip() or "git apply failed in sandbox"
                        errors.append(err_msg)
                        failure = True
                    else:
                        applied_cleanly = True
                        proposal.transition_to(PatchStatus.APPLIED_IN_ISOLATED_WORKTREE)
                        _emit(
                            evidence,
                            "patch_applied_isolated",
                            sandbox_id=sandbox_id,
                            status="applied",
                        )

            if applied_cleanly:
                # 11. Inspect modified files and verify they match the proposal.
                status_res = run_git(["status", "--porcelain"], cwd=sandbox.path, check=False)
                commands_run.append("git status (sandbox)")
                for line in status_res.stdout.splitlines():
                    if len(line) >= 4:
                        cf = line[3:].strip().replace("\\", "/")
                        changed_files.append(cf)

                expected_paths = {f.path for f in parsed_files} or set(proposal.target_files)
                declared_targets = set(proposal.target_files)
                if not expected_paths <= declared_targets:
                    errors.append(
                        "Patch modifies files beyond the declared targets: "
                        f"diff={sorted(expected_paths - declared_targets)}"
                    )
                    failure = True
                if set(changed_files) != expected_paths:
                    errors.append(
                        "Resulting changed files do not match the proposal: "
                        f"sandbox={sorted(set(changed_files))} proposal={sorted(expected_paths)}"
                    )
                    failure = True

                # 12. Resulting diff must stay within policy budgets.
                numstat, resulting_diff_hash = _resulting_diff_stats(sandbox.path)
                max_files = int(patch_cfg.get("max_files", 5)) if isinstance(patch_cfg, dict) else 5
                max_changed_lines = int(patch_cfg.get("max_changed_lines", 150)) if isinstance(patch_cfg, dict) else 150
                total_changed = 0
                for added, deleted, path in numstat:
                    if added == "-" or deleted == "-":
                        total_changed += max_changed_lines + 1  # binary content: force overflow
                        continue
                    total_changed += int(added) + int(deleted)
                if len(numstat) > max_files:
                    errors.append(
                        f"Resulting diff modifies {len(numstat)} files, exceeding limit of {max_files}"
                    )
                    failure = True
                if total_changed > max_changed_lines:
                    errors.append(
                        f"Resulting diff changes {total_changed} lines, exceeding limit of {max_changed_lines}"
                    )
                    failure = True

                # 13. Python AST syntax validation (no execution).
                syntax_errors: list[str] = []
                for cf in changed_files:
                    if cf.endswith(".py"):
                        full_p = sandbox.path / cf
                        if full_p.is_file():
                            try:
                                source = full_p.read_text(encoding="utf-8", errors="replace")
                                ast.parse(source, filename=cf)
                            except SyntaxError as syn_err:
                                syntax_errors.append(
                                    f"Syntax error in modified file '{cf}' at line {syn_err.lineno}: {syn_err.msg}"
                                )

                if syntax_errors:
                    syntax_valid = False
                    errors.extend(syntax_errors)
                    failure = True
                else:
                    syntax_valid = True
                _emit(
                    evidence,
                    "syntax_validation_completed",
                    sandbox_id=sandbox_id,
                    status="ok" if syntax_valid else "failed",
                    count=len(syntax_errors),
                )

                # 14. Phase 8A: Opt-in sandboxed test execution
                if applied_cleanly and not failure and (syntax_valid is not False) and run_tests:
                    test_execution_attempted = True
                    t_cfg = test_config or {}
                    max_proc = t_cfg.get("max_processes")
                    max_mem = t_cfg.get("max_memory_bytes")
                    limits = ResourceLimits(
                        timeout_seconds=test_timeout,
                        max_output_bytes=max_output_bytes,
                        max_processes=max_proc,
                        max_memory_bytes=max_mem,
                        network_allowed=False,
                        dependency_install_allowed=False,
                    )
                    resource_limits = limits.model_dump(mode="json")

                    if t_cfg.get("target_original_worktree"):
                        block_reason = "Test execution in original repository is strictly forbidden"
                        plan = TestPlan(language="python", discovery_reason=block_reason, timeout=test_timeout)
                    elif max_proc == 0:
                        block_reason = "Process limit violation: 0 processes allowed"
                        plan = TestPlan(language="python", discovery_reason=block_reason, timeout=test_timeout)
                    elif max_mem is not None and max_mem < 100:
                        block_reason = "Memory limit violation: insufficient memory allocated"
                        plan = TestPlan(language="python", discovery_reason=block_reason, timeout=test_timeout)
                    else:
                        plan, block_reason = discover_test_plan(
                            sandbox.path,
                            changed_files,
                            limits=limits,
                            policy=t_cfg,
                        )
                    runner_name = (
                        "pytest" if any("pytest" in a for a in plan.exact_command)
                        else "unittest" if any("unittest" in a for a in plan.exact_command)
                        else "vitest" if any("vitest" in a for a in plan.exact_command)
                        else "jest" if any("jest" in a for a in plan.exact_command)
                        else plan.language
                    )
                    test_plan_dict = plan.model_dump(mode="json")
                    test_runner_name = runner_name
                    test_discovery_reason = plan.discovery_reason

                    if block_reason is None and plan.working_directory:
                        try:
                            resolved_cwd = (sandbox.path / plan.working_directory).resolve()
                            if not (resolved_cwd == sandbox.path.resolve() or resolved_cwd.is_relative_to(sandbox.path.resolve())):
                                block_reason = f"Working directory escapes sandbox: {plan.working_directory}"
                        except Exception:
                            block_reason = f"Invalid working directory: {plan.working_directory}"

                    if block_reason is not None:
                        _emit(
                            evidence,
                            "test_execution_blocked",
                            sandbox_id=sandbox_id,
                            reason=block_reason,
                            status="blocked",
                        )
                        tests_status = "blocked"
                        test_execution_blocked_reason = block_reason
                        diagnostic_summary = block_reason
                        test_failure_count = 1
                        diagnostic_limitations = list(plan.limitations)
                        failure = True
                        errors.append(f"Test discovery blocked: {block_reason}")
                        proposal.transition_to(PatchStatus.TEST_BLOCKED)
                    else:
                        _emit(
                            evidence,
                            "test_plan_created",
                            sandbox_id=sandbox_id,
                            runner=plan.language,
                            targets=plan.test_targets,
                            command=" ".join(plan.exact_command),
                            status="created",
                        )
                        # Validate the discovered command against strict allowlist
                        val = validate_test_command(
                            plan.exact_command,
                            sandbox_path=sandbox.path,
                            policy=test_config,
                        )
                        _emit(
                            evidence,
                            "test_command_validated",
                            sandbox_id=sandbox_id,
                            allowed=val.allowed,
                            command=" ".join(plan.exact_command),
                            status="validated" if val.allowed else "rejected",
                        )
                        if not val.allowed:
                            _emit(
                                evidence,
                                "test_execution_blocked",
                                sandbox_id=sandbox_id,
                                reason=val.rejection_reason or "command_validation_failed",
                                status="blocked",
                            )
                            tests_status = "blocked"
                            test_execution_blocked_reason = val.rejection_reason
                            diagnostic_summary = val.rejection_reason
                            test_failure_count = 1
                            diagnostic_limitations = ["command_validation_rejected"]
                            failure = True
                            errors.append(f"Test command blocked: {val.rejection_reason}")
                            proposal.transition_to(PatchStatus.TEST_BLOCKED)
                        else:
                            proposal.transition_to(PatchStatus.TEST_EXECUTION_STARTED)
                            tokens_to_redact = [approval_token] if approval_token else []
                            test_res = execute_test_command(
                                sandbox.path,
                                plan.exact_command,
                                limits=limits,
                                evidence=evidence,
                                sandbox_id=sandbox_id,
                                proposal_id=proposal.proposal_id,
                                extra_tokens=tokens_to_redact,
                                test_targets=plan.test_targets,
                                runner=runner_name,
                                language=plan.language,
                                working_directory=plan.working_directory,
                            )
                            commands_run.extend(test_res.commands_run)
                            test_result = test_res
                            tests_status = test_res.status
                            tests_run_list = list(plan.test_targets)
                            test_exit_code = test_res.exit_code
                            test_duration_ms = test_res.duration_ms
                            test_failures = test_res.failures
                            test_output_redaction_audit = test_res.redaction_audit
                            test_stdout_summary = test_res.stdout_summary
                            test_stderr_summary = test_res.stderr_summary
                            execution_allowed = test_res.execution_allowed
                            diagnostic_summary = test_res.failure_summary or (test_res.failures[0] if test_res.failures else None)
                            failed_test_names = test_res.failed_test_names
                            test_failure_count = test_res.tests_failed
                            test_pass_count = test_res.tests_passed
                            test_skip_count = test_res.tests_skipped
                            test_output_truncated = test_res.output_truncated
                            network_policy_requested = test_res.network_policy_requested
                            network_policy_enforced = test_res.network_policy_enforced
                            network_isolation_verified = test_res.network_isolation_verified
                            diagnostic_limitations = test_res.diagnostics_limitations
                            stack_trace_summary = test_res.stack_trace_summary or None

                            if test_res.status == "passed":
                                proposal.transition_to(PatchStatus.TESTS_PASSED)
                            elif test_res.status == "timed_out":
                                proposal.transition_to(PatchStatus.TEST_TIMEOUT)
                                failure = True
                                errors.extend(test_res.failures)
                            elif test_res.status == "failed":
                                proposal.transition_to(PatchStatus.TESTS_FAILED)
                                failure = True
                                errors.extend(test_res.failures)
                            elif test_res.status == "blocked":
                                proposal.transition_to(PatchStatus.TEST_BLOCKED)
                                failure = True
                                errors.extend(test_res.failures)
                            else:  # error
                                proposal.transition_to(PatchStatus.TEST_ERROR)
                                failure = True
                                errors.extend(test_res.failures)

                # 15. Phase 8B-2: Opt-in sandboxed full-suite execution
                if applied_cleanly and not failure and (syntax_valid is not False) and run_full_suite:
                    test_execution_attempted = True
                    t_cfg = test_config or {}
                    max_proc = t_cfg.get("max_processes")
                    max_mem = t_cfg.get("max_memory_bytes")
                    fs_limits = ResourceLimits(
                        timeout_seconds=test_timeout,
                        max_output_bytes=max_output_bytes,
                        max_processes=max_proc,
                        max_memory_bytes=max_mem,
                        network_allowed=False,
                        dependency_install_allowed=False,
                    )
                    resource_limits = fs_limits.model_dump(mode="json")

                    if t_cfg.get("target_original_worktree"):
                        fs_block_reason = "Test execution in original repository is strictly forbidden"
                        fs_plan = TestPlan(language="python", discovery_reason=fs_block_reason, timeout=test_timeout, full_suite=True)
                    elif max_proc == 0:
                        fs_block_reason = "Process limit violation: 0 processes allowed"
                        fs_plan = TestPlan(language="python", discovery_reason=fs_block_reason, timeout=test_timeout, full_suite=True)
                    elif max_mem is not None and max_mem < 100:
                        fs_block_reason = "Memory limit violation: insufficient memory allocated"
                        fs_plan = TestPlan(language="python", discovery_reason=fs_block_reason, timeout=test_timeout, full_suite=True)
                    else:
                        fs_plan, fs_block_reason = discover_test_plan(
                            sandbox.path,
                            changed_files,
                            full_suite=True,
                            limits=fs_limits,
                            policy=t_cfg,
                        )

                    full_suite_test_plan = fs_plan
                    if fs_block_reason is not None:
                        _emit(evidence, "full_suite_blocked", sandbox_id=sandbox_id, reason=fs_block_reason)
                        full_suite_status = "blocked"
                        full_suite_blocked_reason = fs_block_reason
                        if not run_tests:
                            diagnostic_summary = fs_block_reason
                            test_failure_count = 1
                            diagnostic_limitations = list(fs_plan.limitations)
                        failure = True
                        errors.append(f"Full-suite discovery blocked: {fs_block_reason}")
                        proposal.transition_to(PatchStatus.TEST_BLOCKED)
                    else:
                        _emit(
                            evidence,
                            "full_suite_plan_created",
                            sandbox_id=sandbox_id,
                            runner=fs_plan.language,
                            command=" ".join(fs_plan.exact_command),
                            status="created",
                        )
                        val = validate_test_command(
                            fs_plan.exact_command,
                            sandbox_path=sandbox.path,
                            policy=test_config,
                        )
                        _emit(
                            evidence,
                            "full_suite_command_validated",
                            sandbox_id=sandbox_id,
                            allowed=val.allowed,
                            command=" ".join(fs_plan.exact_command),
                            status="validated" if val.allowed else "rejected",
                        )
                        if not val.allowed:
                            rej = val.rejection_reason or "command_validation_failed"
                            _emit(evidence, "full_suite_blocked", sandbox_id=sandbox_id, reason=rej)
                            full_suite_status = "blocked"
                            full_suite_blocked_reason = rej
                            if not run_tests:
                                diagnostic_summary = rej
                                test_failure_count = 1
                                diagnostic_limitations = ["command_validation_rejected"]
                            failure = True
                            errors.append(f"Full-suite command blocked: {rej}")
                            proposal.transition_to(PatchStatus.TEST_BLOCKED)
                        else:
                            proposal.transition_to(PatchStatus.TEST_EXECUTION_STARTED)
                            tokens_to_redact = [approval_token] if approval_token else []
                            fs_runner_name = (
                                "pytest" if any("pytest" in a for a in fs_plan.exact_command)
                                else "unittest" if any("unittest" in a for a in fs_plan.exact_command)
                                else "vitest" if any("vitest" in a for a in fs_plan.exact_command)
                                else "jest" if any("jest" in a for a in fs_plan.exact_command)
                                else fs_plan.language
                            )
                            fs_res = execute_test_command(
                                sandbox.path,
                                fs_plan.exact_command,
                                limits=fs_limits,
                                evidence=evidence,
                                sandbox_id=sandbox_id,
                                proposal_id=proposal.proposal_id,
                                extra_tokens=tokens_to_redact,
                                test_targets=fs_plan.test_targets,
                                runner=fs_runner_name,
                                language=fs_plan.language,
                                working_directory=fs_plan.working_directory,
                            )
                            fs_res.full_suite = True
                            commands_run.extend(fs_res.commands_run)
                            full_suite_status = fs_res.status
                            full_suite_command = fs_res.command
                            full_suite_result = fs_res.model_dump(mode="json")
                            execution_allowed = fs_res.execution_allowed

                            if not run_tests:
                                test_plan_dict = fs_plan.model_dump(mode="json")
                                test_runner_name = fs_runner_name
                                test_discovery_reason = fs_plan.discovery_reason
                                tests_run_list = list(fs_plan.test_targets)
                                test_exit_code = fs_res.exit_code
                                test_duration_ms = fs_res.duration_ms
                                test_failures = fs_res.failures
                                test_output_redaction_audit = fs_res.redaction_audit
                                test_stdout_summary = fs_res.stdout_summary
                                test_stderr_summary = fs_res.stderr_summary
                                diagnostic_summary = fs_res.failure_summary or (fs_res.failures[0] if fs_res.failures else None)
                                failed_test_names = fs_res.failed_test_names
                                test_failure_count = fs_res.tests_failed
                                test_pass_count = fs_res.tests_passed
                                test_skip_count = fs_res.tests_skipped
                                test_output_truncated = fs_res.output_truncated
                                network_policy_requested = fs_res.network_policy_requested
                                network_policy_enforced = fs_res.network_policy_enforced
                                network_isolation_verified = fs_res.network_isolation_verified
                                diagnostic_limitations = fs_res.diagnostics_limitations
                                stack_trace_summary = fs_res.stack_trace_summary or None

                            if fs_res.status == "passed":
                                # Full suite passed empirically; note that passing full suite is not proof of correctness
                                proposal.transition_to(PatchStatus.TESTS_PASSED)
                            elif fs_res.status == "timed_out":
                                proposal.transition_to(PatchStatus.TEST_TIMEOUT)
                                full_suite_blocked_reason = "Test execution timed out"
                                failure = True
                                errors.extend(fs_res.failures)
                            elif fs_res.status == "failed":
                                proposal.transition_to(PatchStatus.TESTS_FAILED)
                                full_suite_blocked_reason = fs_res.failure_summary or "Full suite tests failed"
                                failure = True
                                errors.extend(fs_res.failures)
                            elif fs_res.status == "blocked":
                                proposal.transition_to(PatchStatus.TEST_BLOCKED)
                                full_suite_blocked_reason = fs_res.failure_summary or "Full suite test execution blocked"
                                failure = True
                                errors.extend(fs_res.failures)
                            else:
                                proposal.transition_to(PatchStatus.TEST_ERROR)
                                full_suite_blocked_reason = fs_res.failure_summary or "Full suite execution error"
                                failure = True
                                errors.extend(fs_res.failures)

            if test_config and test_config.get("simulate_cleanup_failure"):
                raise SnapshotCleanupError("Simulated sandbox cleanup failure")

            # Retention must be requested before the context manager exits.
            if failure and retain_sandbox_on_failure:
                sandbox_ctx.retain()
            sandbox_retained = sandbox_ctx.retained

    except SnapshotCleanupError as err:
        cleanup_status = "failed"
        failure = True
        errors.append(f"Sandbox cleanup failed: {err}")
        _emit(
            evidence,
            "sandbox_cleanup_failed",
            sandbox_id=sandbox_id,
            reason=str(err),
            status="failed",
        )
    except Exception as err:
        if isinstance(err, RuntimeError) and "CRITICAL INVARIANT" in str(err):
            raise
        errors.append(f"Sandbox failure: {type(err).__name__}: {err}")
        failure = True
    finally:
        if sandbox_id is not None and cleanup_status == "not_applicable":
            if sandbox_retained:
                cleanup_status = "retained"
                _emit(
                    evidence,
                    "sandbox_cleanup_completed",
                    sandbox_id=sandbox_id,
                    status="retained",
                )
            else:
                cleanup_status = "completed"
                _emit(
                    evidence,
                    "sandbox_cleanup_completed",
                    sandbox_id=sandbox_id,
                    status="completed",
                )

        # Verify original working tree was never modified
        post_status = run_git(["status", "--porcelain"], cwd=repository_root, check=False)
        commands_run.append("git status (post-check)")
        if post_status.stdout != initial_status.stdout:
            raise RuntimeError(
                "CRITICAL INVARIANT VIOLATION: Original repository working tree was modified during patch sandbox execution!"
            )

    if run_tests or run_full_suite:
        if proposal.status == PatchStatus.TESTS_PASSED:
            proposal.transition_to(PatchStatus.FAILED_VALIDATION if failure else PatchStatus.VALIDATED)
        elif proposal.status in {
            PatchStatus.TESTS_FAILED,
            PatchStatus.TEST_TIMEOUT,
            PatchStatus.TEST_BLOCKED,
            PatchStatus.TEST_ERROR,
        }:
            proposal.transition_to(PatchStatus.FAILED_VALIDATION)
        elif proposal.status == PatchStatus.APPLIED_IN_ISOLATED_WORKTREE and failure:
            proposal.transition_to(PatchStatus.FAILED_VALIDATION)
    else:
        if syntax_valid is False or failure:
            if proposal.status == PatchStatus.APPLIED_IN_ISOLATED_WORKTREE:
                proposal.transition_to(PatchStatus.FAILED_VALIDATION)
        elif applied_cleanly and proposal.status == PatchStatus.APPLIED_IN_ISOLATED_WORKTREE:
            proposal.transition_to(PatchStatus.VALIDATED)

    is_valid = (
        applied_cleanly
        and approval_verified
        and (syntax_valid is not False)
        and (len(errors) == 0)
        and cleanup_status in {"completed", "retained"}
        and (tests_status == "passed" if run_tests else True)
        and (full_suite_status == "passed" if run_full_suite else True)
    )

    return finish(
        valid=is_valid,
        applies_cleanly=applied_cleanly,
        patch_applied_in_isolated_sandbox=applied_cleanly,
        syntax_valid=syntax_valid,
        type_check_status="not_run",
        tests_status=tests_status,
        build_status="not_run",
        security_check_status="passed" if proposal.redaction_audit.safe else "failed",
        changed_files=changed_files,
        rejected_files=[],
        errors=errors,
        warnings=warnings,
        approval_verified=approval_verified,
        sandbox_id=sandbox_id,
        resulting_diff_hash=resulting_diff_hash,
        cleanup_status=cleanup_status,
        sandbox_retained=sandbox_retained,
    )


__all__ = [
    "apply_patch_in_isolated_sandbox",
]
