import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("eval_runner_dataflow", ROOT / "eval/run_eval.py")
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

def test_dataflow_expanded_contract_and_mix():
    cases = runner.discover_cases(ROOT / "eval/cases/dataflow-expanded", recursive=True)
    assert len(cases) >= 25
    for case in cases:
        metadata = runner.load_case(case)
        assert {"expected_behavior", "flow_kind", "rule", "severity", "redaction"} <= metadata.keys()
        assert metadata["expected_behavior"] in {"detect", "abstain", "review-only"}

def test_dataflow_reporting_has_flow_metrics():
    results = list(runner.run(ROOT / "eval/cases/dataflow-expanded", recursive=True))
    summary = runner.summarize_results(results)
    assert len(results) == 25
    assert {"direct_flow", "alias_flow", "overall", "mean_duration", "p95_duration", "mean_propagation_steps", "unsupported_flow_abstentions"} <= summary.keys()
    assert summary["unsupported_flow_abstentions"] == 0

def test_dataflow_fixtures_are_never_executed():
    assert all("dataflow fixture" in (case / "before/example.py").read_text() for case in runner.discover_cases(ROOT / "eval/cases/dataflow-expanded", recursive=True))
