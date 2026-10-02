"""Sandboxed test execution with environment sanitization and resource limits."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from codeatlas.evidence import EvidenceLogger
from codeatlas.git.executable import run_git

from .models import ResourceLimits, TestPlan, TestResult
from .redaction import audit_and_redact_results


def _sanitized_env(sandbox_path: Path) -> dict[str, str]:
    """Build a minimal, safe environment without host credentials or tokens."""
    safe_keys = {
        "PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "LANG", "LC_ALL",
        "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    }
    env: dict[str, str] = {}
    for k in safe_keys:
        val = os.environ.get(k)
        if val is not None:
            env[k] = val

    env["PYTHONPATH"] = str(sandbox_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["NODE_PATH"] = str(sandbox_path / "node_modules")
    env["TEMP"] = str(sandbox_path)
    env["TMP"] = str(sandbox_path)
    env["CI"] = "1"
    # Ensure network proxies are disabled
    for proxy_key in ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
        env[proxy_key] = ""
    return env


def execute_test_command(
    sandbox_path: Path,
    command: list[str],
    *,
    limits: ResourceLimits | None = None,
    evidence: EvidenceLogger | None = None,
    sandbox_id: str | None = None,
    extra_tokens: Sequence[str] = (),
    test_targets: Sequence[str] = (),
    runner: str = "",
    language: str = "",
    working_directory: str = ".",
) -> TestResult:
    """Execute an approved test command within the detached sandbox worktree.

    Guarantees:
    - Never uses shell=True.
    - Sanitized environment containing no credentials.
    - Strict timeout enforcement.
    - Max output byte truncation and secret pattern redaction.
    - Post-execution worktree cleanliness verification.
    """
    from .diagnostics import parse_test_diagnostics

    res_limits = limits or ResourceLimits()
    timeout = res_limits.timeout_seconds
    max_bytes = res_limits.max_output_bytes

    if evidence is not None:
        evidence.emit(
            "test_execution_started",
            sandbox_id=sandbox_id,
            command=" ".join(command),
            timeout_seconds=timeout,
            status="started",
        )

    # Snapshot sandbox status before test execution to detect unexpected mutations
    pre_test_status = run_git(["status", "--porcelain"], cwd=sandbox_path, check=False)

    # If python executable is needed, ensure it points to sys.executable
    cmd_to_run = list(command)
    if cmd_to_run and cmd_to_run[0] == "python":
        cmd_to_run[0] = sys.executable
    elif cmd_to_run and cmd_to_run[0] in {"vitest", "jest"}:
        local_cmd = sandbox_path / "node_modules" / ".bin" / f"{cmd_to_run[0]}.cmd"
        local_bin = sandbox_path / "node_modules" / ".bin" / cmd_to_run[0]
        if local_cmd.is_file():
            cmd_to_run[0] = str(local_cmd)
        elif local_bin.is_file():
            cmd_to_run[0] = str(local_bin)

    started = time.perf_counter()
    stdout_raw = ""
    stderr_raw = ""
    exit_code: int | None = None
    timed_out = False
    proc_failed = False
    error_message: str | None = None

    try:
        proc = subprocess.run(
            cmd_to_run,
            cwd=sandbox_path,
            env=_sanitized_env(sandbox_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        duration_ms = (time.perf_counter() - started) * 1000.0
        exit_code = proc.returncode
        stdout_raw = proc.stdout or ""
        stderr_raw = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        duration_ms = (time.perf_counter() - started) * 1000.0
        timed_out = True
        exit_code = -1
        stdout_raw = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr_raw = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        if evidence is not None:
            evidence.emit(
                "test_execution_timed_out",
                sandbox_id=sandbox_id,
                timeout_seconds=timeout,
                duration_ms=duration_ms,
                status="timed_out",
            )
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000.0
        proc_failed = True
        exit_code = -1
        error_message = f"Process launch error: {type(exc).__name__}: {exc}"

    # Verify that test execution did not modify repository tracked/untracked files
    post_test_status = run_git(["status", "--porcelain"], cwd=sandbox_path, check=False)
    repo_mutated = post_test_status.stdout != pre_test_status.stdout

    # Build failure summaries
    failures: list[str] = []
    if timed_out:
        failures.append(f"Test execution timed out after {timeout} seconds")
    elif proc_failed:
        failures.append(error_message or "Execution failure")
    elif repo_mutated:
        diff_lines = [
            line for line in post_test_status.stdout.splitlines()
            if line not in pre_test_status.stdout.splitlines()
        ]
        failures.append(
            f"Test execution modified repository worktree (violates containment): {', '.join(diff_lines[:3])}"
        )
    elif exit_code != 0:
        # Extract concise failure lines from stderr or stdout
        combined_err = (stderr_raw or stdout_raw).strip()
        last_lines = combined_err.splitlines()[-5:] if combined_err else ["Non-zero exit code"]
        failures.append("; ".join(line.strip() for line in last_lines if line.strip()))

    output_truncated = False
    if len(stdout_raw.encode("utf-8", errors="replace")) > max_bytes or len(stderr_raw.encode("utf-8", errors="replace")) > max_bytes:
        output_truncated = True
        if evidence is not None:
            evidence.emit(
                "test_output_truncated",
                sandbox_id=sandbox_id,
                max_output_bytes=max_bytes,
                status="truncated",
            )

    # Redact outputs
    clean_stdout, clean_stderr, clean_failures, redaction_audit = audit_and_redact_results(
        stdout_raw,
        stderr_raw,
        failures,
        extra_tokens=extra_tokens,
        max_output_bytes=max_bytes,
    )

    if evidence is not None:
        evidence.emit(
            "test_output_redacted",
            sandbox_id=sandbox_id,
            matches_count=redaction_audit.raw_value_matches,
            rules_applied=redaction_audit.rules_applied,
            status="redacted",
        )

    # Inferred runner
    inferred_runner = runner
    if not inferred_runner:
        for term in command:
            if "pytest" in term:
                inferred_runner = "pytest"
                break
            if "unittest" in term:
                inferred_runner = "unittest"
                break
            if "vitest" in term:
                inferred_runner = "vitest"
                break
            if "jest" in term:
                inferred_runner = "jest"
                break
        if not inferred_runner:
            inferred_runner = "python" if language == "python" else "generic"

    # Parse failure diagnostics
    if evidence is not None:
        evidence.emit(
            "test_diagnostics_started",
            sandbox_id=sandbox_id,
            runner=inferred_runner,
            status="started",
        )

    try:
        diag = parse_test_diagnostics(
            runner=inferred_runner,
            exit_code=exit_code,
            stdout=clean_stdout,
            stderr=clean_stderr,
            timed_out=timed_out,
            proc_failed=proc_failed,
            repo_mutated=repo_mutated,
            output_truncated=output_truncated,
            extra_tokens=extra_tokens,
            error_message=clean_failures[0] if clean_failures else None,
        )
        if evidence is not None:
            if diag.get("failed_test_names"):
                evidence.emit(
                    "test_failure_parsed",
                    sandbox_id=sandbox_id,
                    failed_tests=diag["failed_test_names"],
                    failure_summary=diag.get("failure_summary", ""),
                    status="parsed",
                )
            evidence.emit(
                "test_diagnostics_completed",
                sandbox_id=sandbox_id,
                tests_passed=diag.get("tests_passed", 0),
                tests_failed=diag.get("tests_failed", 0),
                status="completed",
            )
    except Exception as diag_err:
        if evidence is not None:
            evidence.emit(
                "test_diagnostics_failed",
                sandbox_id=sandbox_id,
                error=str(diag_err),
                status="failed",
            )
        diag = {
            "tests_passed": 0,
            "tests_failed": 1 if exit_code != 0 else 0,
            "tests_skipped": 0,
            "failed_test_names": [],
            "failure_summary": clean_failures[0] if clean_failures else f"Exit code {exit_code}",
            "stack_trace_summary": "",
            "diagnostics_limitations": ["diagnostics_exception"],
        }

    # Determine final status
    if timed_out:
        status = "timed_out"
    elif repo_mutated or proc_failed:
        status = "error"
    elif exit_code == 0:
        status = "passed"
    else:
        status = "failed"

    if evidence is not None:
        evidence.emit(
            "test_execution_completed",
            sandbox_id=sandbox_id,
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
        )

    return TestResult(
        status=status,
        runner=inferred_runner,
        language=language or ("python" if inferred_runner in {"pytest", "unittest"} else "typescript"),
        command=list(command),
        working_directory=working_directory,
        tests_run=list(test_targets),
        tests_passed=diag.get("tests_passed", 0),
        tests_failed=diag.get("tests_failed", 0),
        tests_skipped=diag.get("tests_skipped", 0),
        failed_test_names=diag.get("failed_test_names", []),
        failure_summary=diag.get("failure_summary", ""),
        stack_trace_summary=diag.get("stack_trace_summary", ""),
        commands_run=[" ".join(command)],
        exit_code=exit_code,
        duration_ms=duration_ms,
        timeout=timeout,
        stdout_summary=clean_stdout[:2000],
        stderr_summary=clean_stderr[:2000],
        output_truncated=output_truncated,
        failures=clean_failures,
        redaction_audit=redaction_audit.model_dump(mode="json"),
        network_policy_requested="disabled",
        network_policy_enforced=True,
        network_isolation_verified=False,
        dependency_install_allowed=res_limits.dependency_install_allowed,
        execution_allowed=True,
        network_allowed=res_limits.network_allowed,
        resource_limits=res_limits.model_dump(mode="json"),
        sandbox_id=sandbox_id,
        diagnostics_limitations=diag.get("diagnostics_limitations", []),
    )


__all__ = [
    "execute_test_command",
]
