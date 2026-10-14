"""Offline runner for versioned repository review and repair observations."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .provider_comparison import load_provider_comparisons, score_provider_comparisons
from .repair_metrics import RepairObservation, score_repair_benchmark
from .repository import (
    load_benchmark_suite,
    load_review_observations,
    score_review_benchmark,
)


def _load_repair_observations(path: str | Path) -> list[RepairObservation]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise TypeError("repair observations must be a JSON list")
    return [RepairObservation.model_validate(item) for item in data]


def run_benchmark_files(
    suite_path: str | Path,
    *,
    review_observations_path: str | Path | None = None,
    repair_observations_path: str | Path | None = None,
    provider_comparisons_path: str | Path | None = None,
    fixture_root: str | Path | None = None,
) -> dict[str, Any]:
    """Load and score supplied observations; never clone, execute, or contact a provider."""
    if review_observations_path is None and repair_observations_path is None and provider_comparisons_path is None:
        raise ValueError("at least one observation or comparison file is required")
    suite = load_benchmark_suite(suite_path, fixture_root=fixture_root)
    result: dict[str, Any] = {
        "benchmark_version": suite.benchmark_version,
        "suite_case_count": len(suite.cases),
        "review_reports": [],
        "repair_reports": [],
        "provider_comparison_report": None,
    }
    if review_observations_path is not None:
        result["review_reports"] = [
            report.model_dump(mode="json")
            for report in score_review_benchmark(suite, load_review_observations(review_observations_path))
        ]
    if repair_observations_path is not None:
        result["repair_reports"] = [
            report.model_dump(mode="json")
            for report in score_repair_benchmark(suite, _load_repair_observations(repair_observations_path))
        ]
    if provider_comparisons_path is not None:
        result["provider_comparison_report"] = score_provider_comparisons(
            suite, load_provider_comparisons(provider_comparisons_path),
        ).model_dump(mode="json")
    return result


def write_benchmark_report(
    result: dict[str, Any],
    output: str | Path,
    *,
    protected_inputs: tuple[str | Path, ...] = (),
) -> None:
    """Atomically write a report without replacing labels or observations."""
    destination = Path(output)
    resolved = destination.resolve()
    protected = {Path(path).resolve() for path in protected_inputs}
    if resolved in protected:
        raise ValueError("benchmark output cannot replace an input file")
    if destination.is_symlink():
        raise ValueError("benchmark output cannot be a symbolic link")
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp",
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, destination)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--review-observations", type=Path)
    parser.add_argument("--repair-observations", type=Path)
    parser.add_argument("--provider-comparisons", type=Path)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_benchmark_files(
            args.suite,
            review_observations_path=args.review_observations,
            repair_observations_path=args.repair_observations,
            provider_comparisons_path=args.provider_comparisons,
            fixture_root=args.fixture_root,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        inputs = tuple(path for path in (args.suite, args.review_observations, args.repair_observations, args.provider_comparisons) if path is not None)
        try:
            write_benchmark_report(result, args.output, protected_inputs=inputs)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
