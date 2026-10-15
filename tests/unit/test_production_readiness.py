from pathlib import Path

from eval.eval_production_readiness import evaluate


def test_readiness_reports_measured_metrics_and_unavailable_live() -> None:
    result = evaluate(Path("eval/cases/real-repositories/v1/suite.json"))
    assert result["production_ready"] is False
    sections = result["sections"]
    assert sections["benchmark_integrity"]["status"] == "passed"
    assert sections["live_provider_review_metrics"]["provider_mode_used"] == "unavailable"
    assert sections["deterministic_review_metrics"]
    assert sections["process_interruption_recovery"]["status"] == "available"
    assert sections["process_interruption_recovery"]["scenarios"] == [
        "queued", "provider_generation", "test_execution", "revert",
    ]
    assert sections["release_artifact_correctness"]["production_ready"] is False
