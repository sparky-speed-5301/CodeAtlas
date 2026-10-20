"""Execute bounded local beta contracts; external integrations require supplied evidence."""

from __future__ import annotations

import argparse
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.beta import (
    BetaReport,
    build_beta_report,
    default_beta_config,
    write_beta_export,
)
from codeatlas.beta_execution import BetaExecutionSummary, summarize_execution
from codeatlas.beta_session import BetaSession, BetaSessionEvent, BetaSessionStore


class BetaEvaluationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Literal["session_restart", "bounded_export", "no_overwrite", "mode_separation", "terminal_scope", "live_provider", "public_repository", "github", "extension", "platform_matrix", "toolchain_matrix"]
    status: Literal["passed", "failed", "unavailable", "not_run"]
    evidence_basis: Literal["measured", "contract_only", "unavailable"]


class BetaExecutionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evaluation_version: Literal["beta-execution-evaluation-v1"] = "beta-execution-evaluation-v1"
    generated_at: datetime
    status: Literal["passed_with_limitations", "failed"]
    production_ready: Literal[False] = False
    acceptance_complete: Literal[False] = False
    checks: tuple[BetaEvaluationCheck, ...] = Field(min_length=11, max_length=11)
    execution: BetaExecutionSummary
    beta: BetaReport


def evaluate() -> BetaExecutionEvaluation:
    """Run owned temporary fixtures only; never touch caller repositories or providers."""
    from codeatlas.beta_session import BetaSessionRunner

    now = datetime.now(UTC)
    session = BetaSession(
        session_id="beta-session-" + "1" * 32,
        repository_identity_hash="repo-" + "a" * 64,
        case_id="contract-only-beta-execution", language="python",
        base_sha="b" * 40, head_sha="c" * 40, provider_mode="offline",
        created_at=now, updated_at=now,
        events=(BetaSessionEvent(sequence=1, stage="created", result="not_run", at=now),),
        events_total=1,
    )
    checks = []

    def record(name: str, passed: bool) -> None:
        checks.append(BetaEvaluationCheck.model_validate({"name": name, "status": "passed" if passed else "failed", "evidence_basis": "contract_only"}))

    with tempfile.TemporaryDirectory(prefix="codeatlas-beta-eval-") as directory:
        root = Path(directory)
        store = BetaSessionStore(root / "session.json")
        store.create(session)
        record("session_restart", BetaSessionStore(store.path).load() == session)
        export = root / "export.json"
        BetaSessionRunner(store.load()).export(export)
        record("bounded_export", export.stat().st_size <= 32768)
        try:
            BetaSessionRunner(store.load()).export(export)
        except ValueError:
            record("no_overwrite", True)
        else:
            record("no_overwrite", False)
        summary = summarize_execution((session,), evidence_basis="contract_only")
        record("mode_separation", len(summary.buckets) == 6 and summary.buckets[0].sessions == 1 and all(bucket.sessions == 0 for bucket in summary.buckets[1:]))
        runner = BetaSessionRunner(session)
        runner.cancel()
        try:
            runner.complete()
        except ValueError:
            record("terminal_scope", True)
        else:
            record("terminal_scope", False)
    for name in ("live_provider", "public_repository", "github", "extension", "platform_matrix", "toolchain_matrix"):
        checks.append(BetaEvaluationCheck.model_validate({"name": name, "status": "not_run", "evidence_basis": "unavailable"}))
    return BetaExecutionEvaluation(
        generated_at=now, status="failed" if any(check.status == "failed" for check in checks) else "passed_with_limitations",
        checks=tuple(checks), execution=summary,
        beta=build_beta_report(default_beta_config(), execution=summary),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New bounded JSON output; existing files are never overwritten.")
    args = parser.parse_args()
    try:
        result = evaluate()
        if args.output:
            write_beta_export(result, args.output)
    except (OSError, ValueError, TypeError):
        parser.exit(2, "Beta execution evaluation could not complete within its safe contract.\n")
    print(result.model_dump_json(indent=2))
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
