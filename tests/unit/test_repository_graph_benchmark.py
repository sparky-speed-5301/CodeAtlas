from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from codeatlas.benchmark import (
    GraphBenchmarkCase,
    GraphEdgeExpectation,
    load_graph_cases,
    score_graph_cases,
)
from codeatlas.repository import GraphEdge, build_repository_index


def _edge(*, source: str = "src/app.py", target: str | None = "src/helper.py", state: str = "resolved") -> GraphEdge:
    edge_type = "import"
    return GraphEdge(
        edge_id=GraphEdge.make_id(
            edge_type=edge_type,
            source_file=source,
            target_file=target,
            source_symbol_id=None,
            target_symbol_id=None,
            source_line=1,
        ),
        edge_type=edge_type,
        source_file=source,
        target_file=target,
        source_line=1,
        evidence_kind="import_statement",
        resolution_state=state,
    )


def test_graph_benchmark_reports_precision_recall_and_unresolved_states() -> None:
    case = GraphBenchmarkCase(
        case_id="case",
        fixture_ref="case",
        expected_edges=(
            GraphEdgeExpectation(edge_type="import", source_file="src/app.py", target_file="src/helper.py"),
        ),
        benchmark_version="test-v1",
    )
    report = score_graph_cases((case,), {"case": [_edge()]})
    assert report.true_positive_count == 1
    assert report.false_positive_count == 0
    assert report.false_negative_count == 0
    precision = next(metric for metric in report.metrics if metric.name == "precision" and metric.edge_type == "overall")
    recall = next(metric for metric in report.metrics if metric.name == "recall" and metric.edge_type == "overall")
    assert precision.value == 1.0
    assert recall.value == 1.0

    unresolved_report = score_graph_cases((case,), {"case": [_edge(state="ambiguous")]})
    assert unresolved_report.unresolved_observed_count == 1
    assert unresolved_report.false_positive_count == 1
    assert unresolved_report.false_negative_count == 1


def test_graph_benchmark_does_not_merge_same_signature_across_cases() -> None:
    expected = GraphEdgeExpectation(edge_type="import", source_file="src/app.py", target_file="src/helper.py")
    cases = (
        GraphBenchmarkCase(case_id="one", fixture_ref="one", expected_edges=(expected,), benchmark_version="test-v1"),
        GraphBenchmarkCase(case_id="two", fixture_ref="two", expected_edges=(expected,), benchmark_version="test-v1"),
    )
    report = score_graph_cases(cases, {"one": [_edge()], "two": []})
    assert report.expected_edge_count == 2
    assert report.false_negative_count == 1
    assert report.metrics[1].value == 0.5


def test_graph_benchmark_rejects_unsafe_fixture_and_duplicate_expected_edges() -> None:
    with pytest.raises(ValidationError, match="safe relative path"):
        GraphBenchmarkCase(case_id="bad", fixture_ref="../outside", benchmark_version="test-v1")

    edge = GraphEdgeExpectation(edge_type="import", source_file="src/app.py")
    with pytest.raises(ValidationError, match="unique"):
        GraphBenchmarkCase(
            case_id="duplicate",
            fixture_ref="duplicate",
            expected_edges=(edge, edge),
            benchmark_version="test-v1",
        )


def test_checked_in_graph_fixtures_score_without_executing_source() -> None:
    cases_dir = Path("eval/cases/graph-intel")
    cases = load_graph_cases(cases_dir)
    observations = {
        case.case_id: build_repository_index(cases_dir / case.fixture_ref / "before").graph_edges
        for case in cases
    }
    report = score_graph_cases(cases, observations)
    assert report.case_count == 2
    assert report.false_positive_count == 0
    assert report.false_negative_count == 0


def test_graph_evaluation_entrypoint_returns_report() -> None:
    path = Path("eval/eval_repo_graph.py")
    spec = importlib.util.spec_from_file_location("eval_repo_graph", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.run_graph_eval(Path("eval/cases/graph-intel"))
    assert report.true_positive_count == report.expected_edge_count == report.observed_edge_count
