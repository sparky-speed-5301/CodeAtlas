import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
RUNNER_PATH = ROOT / "eval" / "run_eval.py"
SPEC = importlib.util.spec_from_file_location("eval_runner_phase3a", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_phase3a_has_requested_static_fixture_mix():
    cases = runner.discover_cases(ROOT / "eval" / "cases" / "secrets-expanded", recursive=True)
    assert len(cases) == 23
    tags = [tag for case in cases for tag in runner.load_case(case).get("tags", [])]
    assert tags.count("true-positive") == 5
    assert tags.count("clean") == 5
    assert tags.count("placeholder") == 3
    assert tags.count("environment-reference") == 2
    assert tags.count("deleted-line") == 2
    assert tags.count("unchanged-context") == 2
    assert tags.count("false-positive-challenge") == 2
    assert tags.count("private-key") == 2


def test_phase3a_results_are_static_and_analyzer_oriented():
    cases_dir = ROOT / "eval" / "cases" / "secrets-expanded"
    results = list(runner.run(cases_dir, runner.predict_secret_status, recursive=True, analyzer_name="static-secret-baseline"))
    assert len(results) == 23
    assert all(result["duration"] == 0.0 for result in results)
    assert all(result["errors"] == [] for result in results)
    assert all(result["analyzer"] == "static-secret-baseline" for result in results)
    assert all(result["expected_finding_status"] == result["expected_status"] for result in results)
    assert all("redaction_result" in result and "precision_related_result" in result for result in results)
    assert {result["precision_related_result"] for result in results} == {"true_positive", "true_negative", "false_positive"}
    assert json.loads(json.dumps(results[0]))["case_id"] == results[0]["case_id"]
