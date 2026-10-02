"""Unit tests for Phase 8B-1: structured test-failure diagnostics and review packet integration."""

from __future__ import annotations

from pathlib import Path
import pytest

from codeatlas.review.packet import ReviewPacket, attach_observed_test_evidence
from codeatlas.verification.diagnostics import parse_test_diagnostics
from codeatlas.verification.models import TestResult


def test_parse_pytest_failure_diagnostics() -> None:
    stdout = """
============================= test session starts =============================
collected 2 items

tests/test_calc.py .F                                                    [100%]

================================== FAILURES ===================================
__________________________________ test_add ___________________________________

    def test_add():
>       assert add(1, 2) == 0
E       AssertionError: assert 3 == 0

tests/test_calc.py:4: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_calc.py::test_add - AssertionError: assert 3 == 0
========================= 1 failed, 1 passed in 0.05s =========================
"""
    diag = parse_test_diagnostics(
        runner="pytest",
        exit_code=1,
        stdout=stdout,
        stderr="",
    )

    assert diag["tests_failed"] == 1
    assert diag["tests_passed"] == 1
    assert "tests/test_calc.py::test_add" in diag["failed_test_names"]
    assert "AssertionError" in diag["failure_summary"]
    assert len(diag["failed_test_names"]) <= 10


def test_parse_unittest_failure_diagnostics() -> None:
    stderr = """
FAIL: test_sub (tests.test_math.TestMath.test_sub)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\\repo\\tests\\test_math.py", line 5, in test_sub
    self.assertEqual(sub(5, 2), 0)
AssertionError: 3 != 0

----------------------------------------------------------------------
Ran 3 tests in 0.002s

FAILED (failures=1)
"""
    diag = parse_test_diagnostics(
        runner="unittest",
        exit_code=1,
        stdout="",
        stderr=stderr,
    )

    assert diag["tests_failed"] == 1
    assert diag["tests_passed"] == 2
    assert any("test_sub" in name for name in diag["failed_test_names"])
    assert "AssertionError" in diag["failure_summary"]


def test_parse_js_failure_diagnostics() -> None:
    stdout = """
FAIL tests/calc.test.ts > multiply > should multiply numbers
AssertionError: expected 0 to be 6
    at tests/calc.test.ts:5:10

Tests  1 failed | 1 passed (2)
"""
    diag = parse_test_diagnostics(
        runner="vitest",
        exit_code=1,
        stdout=stdout,
        stderr="",
    )

    assert diag["tests_failed"] == 1
    assert diag["tests_passed"] == 1
    assert any("multiply" in name for name in diag["failed_test_names"])
    assert "AssertionError" in diag["failure_summary"]


def test_diagnostics_redaction() -> None:
    stdout = """
FAILED tests/test_auth.py::test_secret - AssertionError: key AKIAIOSFODNN7EXAMPLE != ghp_111122223333444455556666777788889999
"""
    diag = parse_test_diagnostics(
        runner="pytest",
        exit_code=1,
        stdout=stdout,
        stderr="",
        extra_tokens=["CAT-APP-token1234567890"],
    )

    assert "AKIAIOSFODNN7EXAMPLE" not in diag["failure_summary"]
    assert "ghp_111122223333444455556666777788889999" not in diag["failure_summary"]
    assert "[REDACTED]" in diag["failure_summary"]


def test_diagnostics_timeout_and_truncation() -> None:
    diag = parse_test_diagnostics(
        runner="pytest",
        exit_code=-1,
        stdout="partial output...",
        stderr="",
        timed_out=True,
        output_truncated=True,
        error_message="Test execution timed out after 10.0 seconds",
    )

    assert diag["tests_failed"] == 1
    assert "timed out" in diag["failure_summary"].lower()
    assert "output_truncated_during_diagnostics" in diag["diagnostics_limitations"]


def test_review_packet_attachment_bounds() -> None:
    packet = ReviewPacket(
        packet_id="pkt-1",
        repository="repo",
        changed_files=["src/calc.py"],
    )

    class DummyProposal:
        proposal_id = "prop-123"

    # Should not attach if test was not run
    not_run_res = TestResult(status="not_run", execution_allowed=False)
    pkt_not_run = attach_observed_test_evidence(packet.model_copy(), DummyProposal(), not_run_res)
    assert pkt_not_run.observed_test_evidence is None

    # Should attach bounded evidence if executed
    many_failed = [f"tests/test_{i}.py::test_{i}" for i in range(25)]
    executed_res = TestResult(
        status="failed",
        execution_allowed=True,
        runner="pytest",
        command=["pytest", "tests/"],
        tests_run=["tests/test_calc.py"],
        tests_failed=25,
        failed_test_names=many_failed,
        failure_summary="X" * 1000,
        stack_trace_summary="line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8",
        duration_ms=123.4,
        sandbox_id="sandbox-test-1",
        redaction_audit={"safe": True},
    )

    pkt_attached = attach_observed_test_evidence(packet.model_copy(), DummyProposal(), executed_res)
    assert pkt_attached.observed_test_evidence is not None
    ev = pkt_attached.observed_test_evidence

    assert ev.proposal_id == "prop-123"
    assert ev.sandbox_id == "sandbox-test-1"
    assert ev.result == "failed"
    assert len(ev.failed_test_names) <= 10
    assert len(ev.failure_summary) <= 500
    assert len(ev.stack_trace_summary.splitlines()) <= 6
    assert ev.observed_evidence_only is True
    assert any("does not prove patch correctness" in lim for lim in ev.limitations)
