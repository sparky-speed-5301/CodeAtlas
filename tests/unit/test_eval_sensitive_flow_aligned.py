import importlib.util
from pathlib import Path
import tempfile
import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("eval_runner_aligned", ROOT / "eval" / "run_eval.py")
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

ALIGNED_DIR = ROOT / "eval" / "cases" / "sensitive-flow-aligned"


def test_metadata_validation():
    counts = runner.validate_benchmark_suite(ALIGNED_DIR)
    assert counts["total"] >= 30
    assert counts["positives"] >= 15
    assert counts["negatives"] >= 8
    assert counts["ambiguous"] >= 4

    # Negative test: invalid positive case missing anchor
    with tempfile.TemporaryDirectory() as tmp:
        case = Path(tmp) / "bad-case"
        case.mkdir()
        (case / "before").mkdir()
        (case / "visible_tests").mkdir()
        (case / "before" / "app.py").write_text("print(1)\n")
        (case / "visible_tests" / "test.txt").write_text("test\n")
        (case / "metadata.json").write_text(
            '{"case_id": "bad-case", "category": "SENSITIVE_DATA_EXPOSURE", "expected_status": "detected", "abstention_allowed": false}'
        )
        with pytest.raises(ValueError, match="must specify expected_file and expected_line"):
            runner.validate_benchmark_suite(Path(tmp))


def test_aligned_direct_flow_case():
    case_dir = ALIGNED_DIR / "py-print-direct-token"
    res = runner.real_analyzer_status(case_dir)
    assert res["predicted_status"] == "detected"
    assert res["tp"] is True
    assert res["predicted_flow_type"] == "direct"
    assert res["matched_finding"] is True
    assert res["anchor_match"] is True


def test_aligned_alias_flow_case():
    case_dir = ALIGNED_DIR / "py-auth-alias-print"
    res = runner.real_analyzer_status(case_dir)
    assert res["predicted_status"] == "detected"
    assert res["tp"] is True
    assert res["predicted_flow_type"] == "alias"
    assert res["matched_finding"] is True
    assert res["anchor_match"] is True


def test_aligned_safe_case():
    case_dir = ALIGNED_DIR / "ts-token-literal-safe"
    res = runner.real_analyzer_status(case_dir)
    assert res["predicted_status"] == "review_only"
    assert res["tn"] is True
    assert res["fp"] is False
    assert res["actual_finding_count"] == 0


def test_aligned_ambiguous_case():
    case_dir = ALIGNED_DIR / "py-interprocedural-call"
    res = runner.real_analyzer_status(case_dir)
    assert res["predicted_status"] == "abstained"
    assert res["abstention"] is True
    assert res["actual_finding_count"] == 0


def test_expected_rule_matching():
    case_dir = ALIGNED_DIR / "py-logger-direct-password"
    res = runner.real_analyzer_status(case_dir)
    assert res["rule_match"] is True
    assert res["rule"] == "sensitive-to-sink"


def test_expected_flow_matching():
    for case_id in ("py-print-direct-token", "py-auth-alias-print", "ts-template-alias-token", "py-object-header-auth-sink", "ts-dict-body-credit-card-sink"):
        case_dir = ALIGNED_DIR / case_id
        res = runner.real_analyzer_status(case_dir)
        assert res["flow_match"] is True, f"Flow mismatch for {case_id}: expected {res['expected_flow_type']}, actual {res['predicted_flow_type']}"


def test_expected_anchor_matching():
    case_dir = ALIGNED_DIR / "py-password-alias-exception"
    res = runner.real_analyzer_status(case_dir)
    assert res["anchor_match"] is True
    meta = runner.load_case(case_dir)
    finding = res["findings"][0]
    assert finding["file"] == meta["expected_file"]
    assert finding["start_line"] == meta["expected_line"]


def test_regression_phase3a():
    cases_dir = ROOT / "eval/cases/secrets-expanded"
    results = list(runner.run(cases_dir, runner.predict_secret_status, recursive=True, analyzer_name="static-secret-baseline"))
    assert len(results) == 23
    assert all(r["errors"] == [] for r in results)
    summary = runner.summarize_results(results)
    assert summary["cases"] == 23


def test_regression_phase3b():
    cases_dir = ROOT / "eval/cases/sensitive-expanded"
    results = list(runner.run(cases_dir, runner.predict_secret_status, recursive=True, analyzer_name="static-secret-baseline"))
    assert len(results) == 25
    summary = runner.summarize_results(results)
    assert summary["cases"] == 25


def test_redaction_safety():
    case_dir = ALIGNED_DIR / "py-logger-direct-password"
    res = runner.real_analyzer_status(case_dir)
    assert res["redaction_safe"] is True
    for f in res["findings"]:
        prov = f.get("provenance", {})
        assert prov.get("source_expression") == "<redacted>"
        assert prov.get("sink_expression") == "<redacted>"
        for evidence_item in f.get("evidence", []):
            assert "<redacted>" in evidence_item


def test_fixture_cleanup():
    tmp_root = Path(tempfile.gettempdir())
    before_eval_temps = set(tmp_root.glob("codeatlas-eval-*"))
    case_dir = ALIGNED_DIR / "ts-console-direct-token"
    runner.real_analyzer_status(case_dir)
    after_eval_temps = set(tmp_root.glob("codeatlas-eval-*"))
    # No temporary directories leaked
    leaked = after_eval_temps - before_eval_temps
    assert not leaked, f"Temporary directories leaked: {leaked}"
