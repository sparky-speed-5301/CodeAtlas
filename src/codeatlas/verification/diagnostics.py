"""Structured failure diagnostics parsing and normalization for test executions."""

from __future__ import annotations

import re
from typing import Any, Sequence

from .models import OutputRedactionAudit
from .redaction import redact_test_output

MAX_FAILED_TESTS = 10
MAX_STACK_FRAMES = 3
MAX_SUMMARY_LENGTH = 500
MAX_OUTPUT_BYTES = 4000

# Regex patterns for pytest
RE_PYTEST_FAILED_LINE = re.compile(r"FAILED\s+([^\s:]+::\S+)(?:\s+-\s+(.+))?")
RE_PYTEST_SUMMARY = re.compile(
    r"(?:(?:(\d+)\s+failed)?[,\s]*(?:(\d+)\s+passed)?[,\s]*(?:(\d+)\s+skipped)?).*in\s+[\d\.]+s",
    re.IGNORECASE,
)
RE_PYTEST_ALT_SUMMARY = re.compile(r"(\d+)\s+(failed|passed|skipped|error)")

# Regex patterns for unittest
RE_UNITTEST_FAIL_LINE = re.compile(r"^(?:FAIL|ERROR):\s+([^\s(]+)\s+\(([^)]+)\)", re.MULTILINE)
RE_UNITTEST_SUMMARY = re.compile(r"FAILED\s+\((?:failures=(\d+))?[,\s]*(?:errors=(\d+))?[,\s]*(?:skipped=(\d+))?\)")
RE_UNITTEST_RAN = re.compile(r"Ran\s+(\d+)\s+tests?\s+in")

# Regex patterns for vitest
RE_VITEST_FAIL_LINE = re.compile(r"(?:FAIL|✕)\s+(.+?)(?:\s*>\s*(.+))?$")
RE_VITEST_SUMMARY = re.compile(r"Tests\s+(?:(\d+)\s+failed)?[,\s|]*(?:(\d+)\s+passed)?")

# Regex patterns for jest
RE_JEST_FAIL_LINE = re.compile(r"(?:✕|FAIL)\s+(.+?)(?:\s*\([\d\.]+\s*m?s\))?$")
RE_JEST_SUMMARY = re.compile(r"Tests:\s+(?:(\d+)\s+failed)?[,\s]*(?:(\d+)\s+passed)?[,\s]*(?:(\d+)\s+total)?")

# Common exception pattern
RE_EXCEPTION = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure|Interrupt)):?\s*(.*)")


def parse_test_diagnostics(
    *,
    runner: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool = False,
    proc_failed: bool = False,
    repo_mutated: bool = False,
    output_truncated: bool = False,
    extra_tokens: Sequence[str] = (),
    error_message: str | None = None,
) -> dict[str, Any]:
    """Extract structured, bounded failure diagnostics from test runner output."""
    failed_test_names: list[str] = []
    tests_passed = 0
    tests_failed = 0
    tests_skipped = 0
    failure_summary = ""
    stack_trace_summary = ""
    limitations: list[str] = []

    combined_text = f"{stdout}\n{stderr}".strip()

    if output_truncated:
        limitations.append("output_truncated_during_diagnostics")

    if timed_out:
        failure_summary = error_message or "Test execution timed out"
        return {
            "tests_passed": 0,
            "tests_failed": 1,
            "tests_skipped": 0,
            "failed_test_names": [],
            "failure_summary": failure_summary[:MAX_SUMMARY_LENGTH],
            "stack_trace_summary": "",
            "diagnostics_limitations": limitations,
        }

    if proc_failed:
        failure_summary = error_message or "Process launch failed"
        return {
            "tests_passed": 0,
            "tests_failed": 1,
            "tests_skipped": 0,
            "failed_test_names": [],
            "failure_summary": failure_summary[:MAX_SUMMARY_LENGTH],
            "stack_trace_summary": "",
            "diagnostics_limitations": limitations,
        }

    if repo_mutated:
        failure_summary = "Test execution modified repository worktree (containment violation)"
        return {
            "tests_passed": 0,
            "tests_failed": 1,
            "tests_skipped": 0,
            "failed_test_names": [],
            "failure_summary": failure_summary[:MAX_SUMMARY_LENGTH],
            "stack_trace_summary": "",
            "diagnostics_limitations": limitations,
        }

    # If exit code was 0, it passed
    if exit_code == 0:
        # Extract pass counts if available
        if runner in {"pytest", "python"}:
            for m in RE_PYTEST_ALT_SUMMARY.finditer(combined_text):
                count, kind = int(m.group(1)), m.group(2).lower()
                if kind == "passed":
                    tests_passed = max(tests_passed, count)
                elif kind == "skipped":
                    tests_skipped = max(tests_skipped, count)
        elif runner == "unittest":
            ran_m = RE_UNITTEST_RAN.search(combined_text)
            if ran_m:
                tests_passed = int(ran_m.group(1))

        if tests_passed == 0 and not combined_text:
            tests_passed = 1

        return {
            "tests_passed": tests_passed,
            "tests_failed": 0,
            "tests_skipped": tests_skipped,
            "failed_test_names": [],
            "failure_summary": "",
            "stack_trace_summary": "",
            "diagnostics_limitations": limitations,
        }

    # Runner-specific failure parsing
    if runner in {"pytest", "python"}:
        _parse_pytest(
            combined_text,
            failed_test_names,
            limitations,
            results_out := {},
        )
        tests_passed = results_out.get("passed", 0)
        tests_failed = results_out.get("failed", len(failed_test_names) or 1)
        tests_skipped = results_out.get("skipped", 0)
        failure_summary = results_out.get("summary", "")
        stack_trace_summary = results_out.get("stack", "")
    elif runner == "unittest":
        _parse_unittest(
            combined_text,
            failed_test_names,
            limitations,
            results_out := {},
        )
        tests_passed = results_out.get("passed", 0)
        tests_failed = results_out.get("failed", len(failed_test_names) or 1)
        tests_skipped = results_out.get("skipped", 0)
        failure_summary = results_out.get("summary", "")
        stack_trace_summary = results_out.get("stack", "")
    elif runner in {"vitest", "jest", "typescript", "javascript"}:
        _parse_js(
            combined_text,
            failed_test_names,
            limitations,
            results_out := {},
        )
        tests_passed = results_out.get("passed", 0)
        tests_failed = results_out.get("failed", len(failed_test_names) or 1)
        tests_skipped = results_out.get("skipped", 0)
        failure_summary = results_out.get("summary", "")
        stack_trace_summary = results_out.get("stack", "")
    else:
        limitations.append("unsupported_runner_diagnostics")
        failure_summary = f"Process exited with code {exit_code}"

    if not failure_summary and combined_text:
        # Fallback to last non-empty line
        lines = [ln.strip() for ln in combined_text.splitlines() if ln.strip()]
        if lines:
            failure_summary = lines[-1]
        else:
            failure_summary = f"Process exited with code {exit_code}"

    # Bounded limits
    bounded_failed_names = failed_test_names[:MAX_FAILED_TESTS]
    bounded_summary = failure_summary[:MAX_SUMMARY_LENGTH]

    # Ensure stack trace summary is bounded
    stack_lines = stack_trace_summary.splitlines()[:MAX_STACK_FRAMES * 2]
    bounded_stack = "\n".join(stack_lines)

    # Redact diagnostics fields
    redacted_summary, _ = redact_test_output(bounded_summary, extra_tokens=extra_tokens)
    redacted_stack, _ = redact_test_output(bounded_stack, extra_tokens=extra_tokens)

    return {
        "tests_passed": tests_passed,
        "tests_failed": max(tests_failed, len(bounded_failed_names) or 1),
        "tests_skipped": tests_skipped,
        "failed_test_names": bounded_failed_names,
        "failure_summary": redacted_summary,
        "stack_trace_summary": redacted_stack,
        "diagnostics_limitations": sorted(set(limitations)),
    }


def _parse_pytest(
    text: str,
    failed_names: list[str],
    limitations: list[str],
    out: dict[str, Any],
) -> None:
    """Parse pytest-specific output format."""
    summaries: list[str] = []
    stack_frames: list[str] = []

    for line in text.splitlines():
        line_clean = line.strip()
        m = RE_PYTEST_FAILED_LINE.search(line_clean)
        if m:
            test_target = m.group(1).strip()
            if test_target not in failed_names:
                failed_names.append(test_target)
            if m.group(2):
                summaries.append(m.group(2).strip())

        # Check for assertion or exception lines
        exc_m = RE_EXCEPTION.search(line_clean)
        if exc_m and not summaries:
            summaries.append(line_clean)

        # Collect stack frame candidate lines
        if line.startswith("    ") and "assert " in line:
            stack_frames.append(line.strip())
        elif " in test_" in line or "::test_" in line:
            stack_frames.append(line.strip())

    # Parse counts
    for m in RE_PYTEST_ALT_SUMMARY.finditer(text):
        count, kind = int(m.group(1)), m.group(2).lower()
        if kind == "passed":
            out["passed"] = count
        elif kind in {"failed", "error"}:
            out["failed"] = max(out.get("failed", 0), count)
        elif kind == "skipped":
            out["skipped"] = count

    if not failed_names and not summaries:
        limitations.append("unrecognized_pytest_failure_format")

    out["summary"] = "; ".join(summaries[:2]) if summaries else ""
    out["stack"] = "\n".join(stack_frames[:MAX_STACK_FRAMES])


def _parse_unittest(
    text: str,
    failed_names: list[str],
    limitations: list[str],
    out: dict[str, Any],
) -> None:
    """Parse Python unittest-specific output format."""
    summaries: list[str] = []
    stack_frames: list[str] = []

    for m in RE_UNITTEST_FAIL_LINE.finditer(text):
        test_method = m.group(1).strip()
        test_module = m.group(2).strip()
        target = f"{test_module}.{test_method}"
        if target not in failed_names:
            failed_names.append(target)

    for line in text.splitlines():
        line_s = line.strip()
        exc_m = RE_EXCEPTION.search(line_s)
        if exc_m:
            summaries.append(line_s)
        if line_s.startswith("File ") and ", line " in line_s:
            stack_frames.append(line_s)

    sum_m = RE_UNITTEST_SUMMARY.search(text)
    if sum_m:
        fails = int(sum_m.group(1) or 0)
        errs = int(sum_m.group(2) or 0)
        out["failed"] = fails + errs
        out["skipped"] = int(sum_m.group(3) or 0)

    ran_m = RE_UNITTEST_RAN.search(text)
    if ran_m:
        total = int(ran_m.group(1))
        out["passed"] = max(0, total - out.get("failed", 0) - out.get("skipped", 0))

    if not failed_names and not summaries:
        limitations.append("unrecognized_unittest_failure_format")

    out["summary"] = "; ".join(summaries[:2]) if summaries else ""
    out["stack"] = "\n".join(stack_frames[:MAX_STACK_FRAMES])


def _parse_js(
    text: str,
    failed_names: list[str],
    limitations: list[str],
    out: dict[str, Any],
) -> None:
    """Parse Vitest or Jest output format."""
    summaries: list[str] = []
    stack_frames: list[str] = []

    for line in text.splitlines():
        line_clean = line.strip()
        m_vitest = RE_VITEST_FAIL_LINE.search(line_clean)
        if m_vitest:
            file_part = m_vitest.group(1).strip()
            name_part = (m_vitest.group(2) or "").strip()
            target = f"{file_part} > {name_part}" if name_part else file_part
            if target not in failed_names:
                failed_names.append(target)

        exc_m = RE_EXCEPTION.search(line_clean)
        if exc_m:
            summaries.append(line_clean)
        elif "AssertionError" in line_clean or "Error:" in line_clean:
            summaries.append(line_clean)

        if line_clean.startswith("at ") and "(" in line_clean:
            stack_frames.append(line_clean)

    # Vitest counts
    v_m = RE_VITEST_SUMMARY.search(text)
    if v_m:
        if v_m.group(1):
            out["failed"] = int(v_m.group(1))
        if v_m.group(2):
            out["passed"] = int(v_m.group(2))

    # Jest counts
    j_m = RE_JEST_SUMMARY.search(text)
    if j_m:
        if j_m.group(1):
            out["failed"] = int(j_m.group(1))
        if j_m.group(2):
            out["passed"] = int(j_m.group(2))

    if not failed_names and not summaries:
        limitations.append("unrecognized_js_failure_format")

    out["summary"] = "; ".join(summaries[:2]) if summaries else ""
    out["stack"] = "\n".join(stack_frames[:MAX_STACK_FRAMES])


__all__ = [
    "parse_test_diagnostics",
]
