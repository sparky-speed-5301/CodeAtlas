"""Run the controlled offline beta session evaluator on owned temporary fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeatlas.beta_offline import evaluate_offline_session, write_offline_evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Write a new bounded metadata-only evaluation file; existing files are rejected.",
    )
    args = parser.parse_args(argv)
    try:
        result = evaluate_offline_session()
        if args.output is not None:
            write_offline_evaluation(result, args.output)
        print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    except (OSError, TypeError, ValueError):
        print(json.dumps({"status": "unavailable", "reason": "offline_evaluation_unavailable"}))
        return 2
    return 0 if result.status == "passed_with_limitations" else 1


if __name__ == "__main__":
    raise SystemExit(main())
