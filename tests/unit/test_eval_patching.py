"""Unit tests for Phase 6 patching evaluation suite."""

from __future__ import annotations

from pathlib import Path
import pytest

from eval.eval_patching import (
    evaluate_patching_case,
    run_patching_eval,
    summarize_patching,
)

CASES_DIR = Path("eval/cases/patching")


def test_patching_eval_suite_has_at_least_25_cases():
    cases = [d for d in CASES_DIR.iterdir() if d.is_dir() and (d / "metadata.json").is_file()]
    assert len(cases) >= 25


def test_eval_case_one_line_python_fix():
    case_dir = CASES_DIR / "case-01-one-line-python-fix"
    res = evaluate_patching_case(case_dir)
    assert res["valid_match"] is True
    assert res["applies_match"] is True
    assert res["worktree_unchanged"] is True
    assert res["redaction_safe"] is True


def test_eval_case_secret_introducing_patch():
    case_dir = CASES_DIR / "case-16-secret-introducing-patch"
    res = evaluate_patching_case(case_dir)
    assert res["valid_match"] is True
    assert res["rejection_correct"] is True
    assert res["redaction_safe"] is True


def test_eval_case_path_traversal():
    case_dir = CASES_DIR / "case-08-path-traversal"
    res = evaluate_patching_case(case_dir)
    assert res["valid_match"] is True
    assert res["rejection_correct"] is True
    assert res["worktree_unchanged"] is True


def test_eval_summarize_metrics():
    mock_results = [
        {
            "case_id": "c1",
            "title": "t1",
            "duration_ms": 10.0,
            "worktree_unchanged": True,
            "redaction_safe": True,
            "policy_correct": True,
            "valid_match": True,
            "applies_match": True,
            "syntax_match": True,
            "rejection_correct": True,
            "is_expected_invalid": False,
            "validation_result": {"valid": True},
            "proposal_status": "validated",
            "commands_run": [],
        },
        {
            "case_id": "c2",
            "title": "t2",
            "duration_ms": 20.0,
            "worktree_unchanged": True,
            "redaction_safe": True,
            "policy_correct": True,
            "valid_match": True,
            "applies_match": False,
            "syntax_match": True,
            "rejection_correct": True,
            "is_expected_invalid": True,
            "validation_result": {"valid": False},
            "proposal_status": "rejected",
            "commands_run": [],
        },
    ]
    summary = summarize_patching(mock_results)
    assert summary["total_cases"] == 2
    assert summary["proposal_construction_success_rate"] == 1.0
    assert summary["redaction_safety"] == 1.0
    assert summary["policy_decision_accuracy"] == 1.0
    assert summary["invalid_proposal_rejection_rate"] == 1.0
    assert summary["clean_isolated_application_rate"] == 1.0
    assert summary["original_worktree_safety_rate"] == 1.0
    assert summary["syntax_validation_accuracy"] == 1.0
