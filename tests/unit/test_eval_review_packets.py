"""Unit tests for Phase 5 review-packet evaluation runner."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from eval.eval_review_packets import (
    evaluate_review_packet_case,
    run_review_packets_eval,
    summarize_review_packets,
)


CASES_DIR = Path("eval/cases/review-packets")


def test_eval_suite_has_at_least_20_cases():
    cases = [d for d in CASES_DIR.iterdir() if d.is_dir() and (d / "metadata.json").is_file()]
    assert len(cases) >= 20


def test_eval_case_small_changed_func():
    case_dir = CASES_DIR / "case-01-small-changed-func"
    res = evaluate_review_packet_case(case_dir)
    assert res["pass"] is True
    assert res["packet_success"] is True
    assert res["redaction_safe"] is True
    assert res["actual_policy"] == "allowed"
    assert res["provider_output_valid"] is True


def test_eval_case_deterministic_secret():
    case_dir = CASES_DIR / "case-05-deterministic-secret"
    res = evaluate_review_packet_case(case_dir)
    assert res["pass"] is True
    assert res["actual_policy"] == "requires_human_approval"
    assert res["redaction_safe"] is True


def test_eval_case_truncation():
    case_dir = CASES_DIR / "case-10-packet-truncation"
    res = evaluate_review_packet_case(case_dir)
    assert res["pass"] is True
    assert res["actual_policy"] == "abstain"
    assert res["truncation_match"] is True


def test_eval_case_duplicate_merge():
    case_dir = CASES_DIR / "case-16-duplicate-findings"
    res = evaluate_review_packet_case(case_dir)
    assert res["pass"] is True
    assert res["merge_match"] is True


def test_eval_summarize_metrics():
    mock_results = [
        {
            "case_id": "c1",
            "scenario": "s1",
            "packet_success": True,
            "redaction_safe": True,
            "policy_match": True,
            "provider_valid_match": True,
            "truncation_match": True,
            "merge_match": True,
            "prio_order_valid": True,
            "expected_provider_valid": True,
            "packet_bytes": 1000,
            "duration_ms": 50.0,
            "pass": True,
        },
        {
            "case_id": "c2",
            "scenario": "duplicate_s2",
            "packet_success": True,
            "redaction_safe": True,
            "policy_match": True,
            "provider_valid_match": True,
            "truncation_match": True,
            "merge_match": True,
            "prio_order_valid": True,
            "expected_provider_valid": False,
            "packet_bytes": 2000,
            "duration_ms": 70.0,
            "pass": True,
        },
    ]
    summary = summarize_review_packets(mock_results)
    assert summary["total_cases"] == 2
    assert summary["packet_construction_success_rate"] == 1.0
    assert summary["redaction_safety"] == 1.0
    assert summary["policy_decision_accuracy"] == 1.0
    assert summary["provider_validation_rejection_rate"] == 1.0
    assert summary["duplicate_merge_rate"] == 1.0
