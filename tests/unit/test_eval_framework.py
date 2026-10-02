import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[2]
RUNNER_PATH = ROOT / "eval" / "run_eval.py"
SPEC = importlib.util.spec_from_file_location("eval_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_discovers_the_ten_static_cases():
    cases = runner.discover_cases(ROOT / "eval" / "cases")
    assert len(cases) == 10
    assert [case.name for case in cases] == sorted(case.name for case in cases)
    categories = [runner.load_case(case)["category"] for case in cases]
    assert {category: categories.count(category) for category in set(categories)} == {
        "buggy": 5,
        "clean": 3,
        "ambiguous": 2,
    }


def test_case_metadata_has_expected_behavior_and_artifacts():
    for case_dir in runner.discover_cases(ROOT / "eval" / "cases"):
        metadata = runner.load_case(case_dir)
        assert metadata["case_id"] == case_dir.name
        assert metadata["expected_behavior"]
        assert isinstance(metadata["abstention_allowed"], bool)
        assert (case_dir / "before").is_dir()
        assert (case_dir / "visible_tests").is_dir()


def test_baseline_results_are_stable_and_jsonl_ready():
    results = list(runner.run(ROOT / "eval" / "cases"))
    assert sum(result["pass"] for result in results) == 3
    assert all(result["duration"] == 0.0 for result in results)
    encoded = "\n".join(json.dumps(result) for result in results)
    assert len(encoded.splitlines()) == 10
    assert all("error_details" in result for result in results)
