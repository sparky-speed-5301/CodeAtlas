"""Offline repository-graph evaluation runner.

The runner indexes only checked-in local fixtures. It never executes fixture
source or tests and emits graph identities/counts rather than source content.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeatlas.benchmark.graph import GraphEvaluationReport, load_graph_cases, score_graph_cases
from codeatlas.repository import build_repository_index


def run_graph_eval(cases_dir: Path) -> GraphEvaluationReport:
    """Index all graph fixtures and score their bounded edge projections."""
    cases = load_graph_cases(cases_dir)
    observations = {}
    for case in cases:
        fixture = (cases_dir / case.fixture_ref).resolve()
        if not fixture.is_relative_to(cases_dir.resolve()) or not fixture.is_dir():
            raise ValueError(f"graph fixture is unavailable or outside cases directory: {case.case_id}")
        snapshot = fixture / "before"
        if not snapshot.is_dir():
            raise ValueError(f"graph fixture is missing before snapshot: {case.case_id}")
        index = build_repository_index(snapshot)
        observations[case.case_id] = index.graph_edges
    return score_graph_cases(cases, observations)


def _json_report(report: GraphEvaluationReport) -> dict[str, Any]:
    return report.model_dump(mode="json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("eval/cases/graph-intel"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_graph_eval(args.cases)
    payload = _json_report(report)
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if (
        report.false_positive_count == 0
        and report.false_negative_count == 0
        and report.unresolved_expectation_mismatch_count == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
