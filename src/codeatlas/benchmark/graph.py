"""Offline graph-edge benchmark contracts and precision/recall scoring."""

from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.repository.models import GraphEdge

GraphEdgeType = Literal[
    "import",
    "reference",
    "call",
    "containment",
    "export",
    "inheritance",
    "route_handler",
    "configuration_key",
]
GraphResolutionState = Literal["resolved", "unresolved", "ambiguous", "unsupported"]


class GraphEdgeExpectation(BaseModel):
    """Secret-free identity label for one expected graph relationship."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    edge_type: GraphEdgeType
    source_file: str = Field(min_length=1, max_length=512)
    target_file: str | None = Field(default=None, max_length=512)
    source_symbol_id: str | None = Field(default=None, max_length=256)
    target_symbol_id: str | None = Field(default=None, max_length=256)
    resolution_state: GraphResolutionState = "resolved"

    @model_validator(mode="after")
    def validate_paths(self) -> GraphEdgeExpectation:
        for value in (self.source_file, self.target_file):
            if value is None:
                continue
            path = PurePosixPath(value.replace("\\", "/"))
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError("graph edge paths must be safe repository-relative paths")
        return self

    def signature(self) -> tuple[str, str, str | None, str | None, str | None, str]:
        return (
            self.edge_type,
            self.source_file,
            self.target_file,
            self.source_symbol_id,
            self.target_symbol_id,
            self.resolution_state,
        )

    @classmethod
    def from_edge(cls, edge: GraphEdge) -> GraphEdgeExpectation:
        return cls(
            edge_type=edge.edge_type,
            source_file=edge.source_file,
            target_file=edge.target_file,
            source_symbol_id=edge.source_symbol_id,
            target_symbol_id=edge.target_symbol_id,
            resolution_state=edge.resolution_state,
        )


class GraphBenchmarkCase(BaseModel):
    """One offline repository graph fixture and its expected edge identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=128)
    fixture_ref: str = Field(min_length=1, max_length=512)
    expected_edges: tuple[GraphEdgeExpectation, ...] = ()
    expected_unresolved_edge_count: int = Field(default=0, ge=0)
    known_limitations: tuple[str, ...] = ()
    benchmark_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_case(self) -> GraphBenchmarkCase:
        path = PurePosixPath(self.fixture_ref.replace("\\", "/"))
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("fixture_ref must be a safe relative path")
        signatures = [item.signature() for item in self.expected_edges]
        if len(signatures) != len(set(signatures)):
            raise ValueError("expected graph edge identities must be unique")
        return self


class GraphMetric(BaseModel):
    """Precision/recall metric with explicit counts and population scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["precision", "recall", "f1"]
    edge_type: GraphEdgeType | Literal["overall"]
    value: float | None
    numerator: int
    denominator: int
    benchmark_version: str
    population: str


class GraphEvaluationReport(BaseModel):
    """Scored graph evaluation without collapsing unresolved states into success."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_version: str
    case_count: int = Field(ge=0)
    expected_edge_count: int = Field(ge=0)
    observed_edge_count: int = Field(ge=0)
    true_positive_count: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    false_negative_count: int = Field(ge=0)
    unresolved_observed_count: int = Field(ge=0)
    unresolved_expectation_mismatch_count: int = Field(ge=0)
    metrics: tuple[GraphMetric, ...]
    case_results: tuple[dict[str, object], ...]


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _make_metrics(
    *,
    edge_type: GraphEdgeType | Literal["overall"],
    tp: int,
    fp: int,
    fn: int,
    version: str,
) -> list[GraphMetric]:
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    precision = _rate(tp, precision_denominator)
    recall = _rate(tp, recall_denominator)
    f1 = None if precision is None or recall is None or precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return [
        GraphMetric(
            name="precision",
            edge_type=edge_type,
            value=precision,
            numerator=tp,
            denominator=precision_denominator,
            benchmark_version=version,
            population="observed edge identities including explicit unresolved states",
        ),
        GraphMetric(
            name="recall",
            edge_type=edge_type,
            value=recall,
            numerator=tp,
            denominator=recall_denominator,
            benchmark_version=version,
            population="expected edge identities",
        ),
        GraphMetric(
            name="f1",
            edge_type=edge_type,
            value=f1,
            numerator=2 * tp,
            denominator=2 * tp + fp + fn,
            benchmark_version=version,
            population="expected and observed edge identities",
        ),
    ]


def score_graph_cases(
    cases: tuple[GraphBenchmarkCase, ...] | list[GraphBenchmarkCase],
    observations: dict[str, list[GraphEdge]] | dict[str, tuple[GraphEdge, ...]],
) -> GraphEvaluationReport:
    """Score exact edge identities and report overall plus per-type metrics."""
    if not cases:
        raise ValueError("at least one graph benchmark case is required")
    versions = {case.benchmark_version for case in cases}
    if len(versions) != 1:
        raise ValueError("all graph benchmark cases must use one benchmark version")
    version = next(iter(versions))

    metrics: list[GraphMetric] = []
    case_results: list[dict[str, object]] = []
    all_expected: set[tuple[str, tuple[str, str, str | None, str | None, str | None, str]]] = set()
    all_observed: set[tuple[str, tuple[str, str, str | None, str | None, str | None, str]]] = set()
    expected_by_type: Counter[str] = Counter()
    observed_by_type: Counter[str] = Counter()
    tp_by_type: Counter[str] = Counter()
    unresolved_observed = 0
    unresolved_expectation_mismatches = 0

    for case in cases:
        expected = {edge.signature() for edge in case.expected_edges}
        observed_edges = list(observations.get(case.case_id, ()))
        observed = {GraphEdgeExpectation.from_edge(edge).signature() for edge in observed_edges}
        expected_by_type.update(item[0] for item in expected)
        observed_by_type.update(item[0] for item in observed)
        unresolved_observed += sum(edge.resolution_state != "resolved" for edge in observed_edges)
        unresolved_count = sum(edge.resolution_state != "resolved" for edge in observed_edges)
        unresolved_expectation_matches = unresolved_count == case.expected_unresolved_edge_count
        unresolved_expectation_mismatches += int(not unresolved_expectation_matches)
        true_positive = expected & observed
        false_positive = observed - expected
        false_negative = expected - observed
        tp_by_type.update(item[0] for item in true_positive)
        all_expected.update((case.case_id, item) for item in expected)
        all_observed.update((case.case_id, item) for item in observed)
        case_results.append(
            {
                "case_id": case.case_id,
                "expected_edge_count": len(expected),
                "observed_edge_count": len(observed),
                "true_positive_count": len(true_positive),
                "false_positive_count": len(false_positive),
                "false_negative_count": len(false_negative),
                "unresolved_observed_count": sum(edge.resolution_state != "resolved" for edge in observed_edges),
                "expected_unresolved_edge_count": case.expected_unresolved_edge_count,
                "unresolved_expectation_match": unresolved_expectation_matches,
                "pass": not false_positive and not false_negative and unresolved_expectation_matches,
            }
        )

    true_positive_total = len(all_expected & all_observed)
    false_positive_total = len(all_observed - all_expected)
    false_negative_total = len(all_expected - all_observed)
    metrics.extend(
        _make_metrics(
            edge_type="overall",
            tp=true_positive_total,
            fp=false_positive_total,
            fn=false_negative_total,
            version=version,
        )
    )
    edge_types = sorted(set(expected_by_type) | set(observed_by_type))
    for edge_type in edge_types:
        expected_type = {item for item in all_expected if item[1][0] == edge_type}
        observed_type = {item for item in all_observed if item[1][0] == edge_type}
        metrics.extend(
            _make_metrics(
                edge_type=edge_type,  # type: ignore[arg-type]
                tp=len(expected_type & observed_type),
                fp=len(observed_type - expected_type),
                fn=len(expected_type - observed_type),
                version=version,
            )
        )

    return GraphEvaluationReport(
        benchmark_version=version,
        case_count=len(cases),
        expected_edge_count=len(all_expected),
        observed_edge_count=len(all_observed),
        true_positive_count=true_positive_total,
        false_positive_count=false_positive_total,
        false_negative_count=false_negative_total,
        unresolved_observed_count=unresolved_observed,
        unresolved_expectation_mismatch_count=unresolved_expectation_mismatches,
        metrics=tuple(metrics),
        case_results=tuple(case_results),
    )


def load_graph_cases(path: str | Path) -> tuple[GraphBenchmarkCase, ...]:
    """Load one metadata JSON file or a directory of graph case metadata."""
    source = Path(path)
    paths = [source] if source.is_file() else sorted(source.glob("*/metadata.json"))
    if not paths:
        raise ValueError("no graph benchmark metadata found")
    cases = tuple(GraphBenchmarkCase.model_validate_json(item.read_text(encoding="utf-8")) for item in paths)
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("graph benchmark case IDs must be unique")
    return cases


__all__ = [
    "GraphBenchmarkCase",
    "GraphEdgeExpectation",
    "GraphEvaluationReport",
    "GraphMetric",
    "load_graph_cases",
    "score_graph_cases",
]
