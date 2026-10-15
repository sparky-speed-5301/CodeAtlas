"""Evidence-based readiness report; passing contracts never imply production readiness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeatlas.benchmark.collection import CollectionRequest, collect_benchmark
from codeatlas.benchmark.quality_collection import (
    collect_deterministic_reviews,
    quality_report,
)
from codeatlas.benchmark.repository import load_benchmark_suite
from codeatlas.benchmark.runner import run_benchmark_files, write_benchmark_report


def _load_evidence(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _aggregate_checks(evidence: dict[str, object] | None, key: str) -> dict[str, object]:
    if evidence is None:
        return {"status": "unavailable", "reason": "evidence_file_unavailable"}
    values = evidence.get(key)
    if not isinstance(values, dict):
        return {"status": "unavailable", "reason": "evidence_shape_invalid"}
    statuses = [row.get("status") for row in values.values() if isinstance(row, dict)]
    return {
        "status": "passed" if statuses and all(status == "passed" for status in statuses) else "failed",
        "checks": values,
        "production_ready": False,
    }


def evaluate(suite_path: Path) -> dict[str, object]:
    suite = load_benchmark_suite(suite_path)
    collected = [collect_benchmark(CollectionRequest(mode="offline", suite_path=str(suite_path), case_id=case.case_id)) for case in suite.cases]
    review = collect_deterministic_reviews(suite_path)
    metrics = quality_report(suite, review)
    comparison = run_benchmark_files(suite_path, provider_comparisons_path=suite_path.parent / "example-provider-comparisons.json")
    sections: dict[str, object] = {
        "benchmark_integrity": {"status": "passed" if all(item.status == "available" for item in collected) else "failed", "case_count": len(collected)},
        "deterministic_review_metrics": metrics["review_reports"],
        "live_provider_review_metrics": {"status": "unavailable", "reason": "live_not_selected", "provider_mode_used": "unavailable"},
        "deterministic_repair_metrics": {"status": "not_run", "reason": "candidate_collection_required"},
        "live_provider_repair_metrics": {"status": "unavailable", "reason": "live_not_selected", "provider_mode_used": "unavailable"},
        "provider_comparison": {
            "status": "contract_only",
            "basis": "example-provider-comparisons.json",
            "live_quality_claimed": False,
            "report": comparison["provider_comparison_report"],
        },
        "fallback_behavior": {"status": "not_run", "reason": "live_not_selected", "automatic_fallback": False},
    }
    results = Path("eval/results")
    release = _load_evidence(results / "release/release-evidence.json")
    platform = _load_evidence(results / "release/platform-matrix.json")
    security = _load_evidence(results / "security-audit.json")
    sections.update(
        {
            "github_lifecycle_correctness": {
                "status": "available",
                "evidence": "tests/integration/test_github_lifecycle_harness.py",
                "mode": "deterministic_mock_transport",
                "limitations": ["No webhook receiver, hosted GitHub Actions run, or artifact upload was exercised."],
            },
            "budget_enforcement": {
                "status": "available",
                "evidence": "tests/unit/test_reliability_budgets.py",
                "limitations": ["Deadlines are cooperative; all service/provider/database paths are not centrally instrumented."],
            },
            "persistence_recovery": {
                "status": "available",
                "evidence": ["tests/unit/test_service_persistence.py", "tests/integration/test_process_recovery.py"],
            },
            "process_interruption_recovery": {
                "status": "available",
                "evidence": "tests/integration/test_process_recovery.py",
                "scenarios": ["queued", "provider_generation", "test_execution", "revert"],
                "limitations": [
                    "Provider interruption uses an offline mock.",
                    "Crash-created test sandboxes are observed but not automatically cleaned.",
                    "Cleanup uncertainty is retained across restarts; recovery never deletes a persisted workspace path.",
                ],
            },
            "service_aware_diagnostics": {
                "status": "available",
                "evidence": "tests/unit/test_service_diagnostics.py",
                "limitations": ["A running service is required for a healthy service-aware result."],
            },
            "platform_availability": platform or {"status": "unavailable", "reason": "platform_matrix_unavailable"},
            "security_findings": security or {"status": "unavailable", "reason": "security_evidence_unavailable"},
            "release_artifact_correctness": _aggregate_checks(release, "checks"),
        }
    )
    return {
        "benchmark_version": suite.benchmark_version, "report_version": "production-readiness-v1",
        "production_ready": False, "sections": sections,
        "environment": metrics["environment"],
        "limitations": ["Deterministic sanitized fixtures measure contracts and installed analyzer behavior, not live quality.",
                         "Available test contracts do not substitute for live providers, hosted GitHub, or native cross-platform evidence.",
                         "Repository-wide historical Ruff and mypy findings remain baseline debt."],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path("eval/cases/real-repositories/v1/suite.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.suite)
        if args.output:
            write_benchmark_report(report, args.output, protected_inputs=(args.suite,))
        else:
            print(json.dumps(report, sort_keys=True, indent=2))
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": "unavailable", "reason": "readiness_evidence_unavailable"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
