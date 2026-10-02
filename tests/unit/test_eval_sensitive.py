import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("eval_runner_sensitive", ROOT / "eval" / "run_eval.py")
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_sensitive_expanded_has_complete_mix_and_artifacts():
    cases = runner.discover_cases(ROOT / "eval/cases/sensitive-expanded", recursive=True)
    assert len(cases) >= 25
    assert {runner.load_case(c)["category"] for c in cases} == {"buggy", "clean", "ambiguous"}
    assert {tag for c in cases for tag in runner.load_case(c).get("tags", [])} >= {
        "true-positive", "deleted-line", "unchanged-context", "renamed", "ambiguous"
    }


def test_sensitive_results_have_confusion_and_metric_summary():
    cases_dir = ROOT / "eval/cases/sensitive-expanded"
    results = list(runner.run(cases_dir, runner.predict_secret_status, recursive=True, analyzer_name="static-secret-baseline"))
    assert len(results) == 25
    assert all({"case_id", "analyzer", "expected_status", "predicted_status", "category", "severity", "rule", "tp", "tn", "fp", "fn", "abstention", "redaction_result", "duration", "errors"} <= set(r) for r in results)
    summary = runner.summarize_results(results)
    assert set(summary) == {"cases", "precision", "recall", "false_positive_rate", "false_negative_rate", "abstention_rate", "coverage", "redaction_safety_rate"}
    assert "accuracy" not in summary

def row(expected: str, predicted: str) -> dict:
    label = ("true_positive" if expected == predicted == "detected" else
             "true_negative" if expected == predicted == "review_only" else
             "false_positive" if expected == "review_only" and predicted == "detected" else
             "false_negative" if expected == "detected" else "ambiguous")
    return {"expected_status": expected, "predicted_status": predicted,
            "tp": label == "true_positive", "tn": label == "true_negative",
            "fp": label == "false_positive", "fn": label == "false_negative",
            "abstention": predicted == "abstained", "redaction_result": "not_applicable"}

def test_metric_perfect_classifier():
    metrics = runner.summarize_results([row("detected", "detected"), row("review_only", "review_only")])
    assert metrics["precision"] == metrics["recall"] == 1.0
    assert metrics["false_positive_rate"] == metrics["false_negative_rate"] == 0.0

def test_metric_one_false_positive():
    metrics = runner.summarize_results([row("detected", "detected"), row("review_only", "detected")])
    assert metrics["precision"] == 0.5 and metrics["false_positive_rate"] == 1.0

def test_metric_one_false_negative_identity():
    metrics = runner.summarize_results([row("detected", "review_only"), row("review_only", "review_only")])
    assert metrics["false_negative_rate"] == 1 - metrics["recall"] == 1.0

def test_metric_abstention_reduces_coverage():
    metrics = runner.summarize_results([row("detected", "abstained"), row("review_only", "review_only")])
    assert metrics["abstention_rate"] == 0.5
    assert metrics["false_negative_rate"] == 1.0
    assert metrics["coverage"] == 0.5

def test_metric_ambiguous_is_excluded_from_confusion():
    metrics = runner.summarize_results([row("ambiguous", "abstained"), row("detected", "detected")])
    assert metrics["precision"] == metrics["recall"] == 1.0

def test_metric_empty_set():
    metrics = runner.summarize_results([])
    assert metrics["cases"] == 0 and all(metrics[key] == 0.0 for key in metrics if key != "cases")
