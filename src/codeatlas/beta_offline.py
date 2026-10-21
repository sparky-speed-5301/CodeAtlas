"""Deterministic, owned-fixture controlled-beta session evaluation."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.beta import default_beta_config, write_beta_export
from codeatlas.beta_execution import BetaExecutionSummary, summarize_execution
from codeatlas.beta_session import BetaSession, BetaSessionRunner, BetaSessionStore
from codeatlas.git.executable import run_git
from codeatlas.orchestrator.repair_models import repair_payload_is_safe

OfflineCheckName = Literal[
    "owned_fixture_session",
    "session_restart",
    "bounded_safe_export",
    "automatic_actions_disabled",
    "live_provider",
    "public_repository",
    "github",
    "extension",
    "platform_matrix",
    "toolchain_matrix",
]
OfflineCheckStatus = Literal["passed", "failed", "unavailable", "not_run", "blocked"]
EvidenceBasis = Literal["measured", "contract_only", "unavailable"]


class OfflineEvaluationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: OfflineCheckName
    status: OfflineCheckStatus
    evidence_basis: EvidenceBasis
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class OfflineSessionEvaluation(BaseModel):
    """Safe report for one synthetic offline session and explicit unavailable states."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evaluation_version: Literal["beta-offline-evaluation-v1"] = "beta-offline-evaluation-v1"
    generated_at: datetime
    status: Literal["passed_with_limitations", "failed"]
    evidence_basis: Literal["contract_only"] = "contract_only"
    production_ready: Literal[False] = False
    acceptance_complete: Literal[False] = False
    checks: tuple[OfflineEvaluationCheck, ...] = Field(min_length=10, max_length=10)
    session: BetaSession
    execution: BetaExecutionSummary


def _git_fixture(root: Path) -> tuple[str, str, str]:
    run_git(("init", "--initial-branch=main"), cwd=root)
    run_git(("config", "user.name", "CodeAtlas Offline Fixture"), cwd=root)
    run_git(("config", "user.email", "offline-fixture@example.invalid"), cwd=root)
    source = "def add(a, b):\n    return a + b\n"
    path = root / "calc.py"
    path.write_text(source, encoding="utf-8")
    run_git(("add", "calc.py"), cwd=root)
    run_git(("commit", "-m", "offline base"), cwd=root)
    base = run_git(("rev-parse", "HEAD"), cwd=root).stdout.strip()
    path.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    run_git(("add", "calc.py"), cwd=root)
    run_git(("commit", "-m", "offline head"), cwd=root)
    head = run_git(("rev-parse", "HEAD"), cwd=root).stdout.strip()
    return base, head, source


def _check(
    name: OfflineCheckName,
    status: OfflineCheckStatus,
    reason_code: str,
    *,
    basis: EvidenceBasis = "contract_only",
) -> OfflineEvaluationCheck:
    return OfflineEvaluationCheck(
        name=name, status=status, evidence_basis=basis, reason_code=reason_code
    )


def evaluate_offline_session() -> OfflineSessionEvaluation:
    """Run only a fixed offline review in an owned temporary Git fixture."""
    now = datetime.now(UTC)
    checks: list[OfflineEvaluationCheck] = []
    with tempfile.TemporaryDirectory(prefix="codeatlas-beta-offline-") as directory:
        root = Path(directory)
        base, head, original_source = _git_fixture(root)
        runner = BetaSessionRunner.create(
            repository=root,
            case_id="contract-only-offline-session",
            language="python",
            base_sha=base,
            head_sha=head,
            config=default_beta_config(),
        )
        store = BetaSessionStore(root / "session.json")
        store.create(runner.session)

        runner.run_offline_review(repository=root)
        offline_ok = runner.session.events[-1].result == "offline"
        checks.append(
            _check(
                "owned_fixture_session",
                "passed" if offline_ok else "unavailable",
                "offline_fixture_reviewed" if offline_ok else "offline_review_unavailable",
            )
        )

        runner.complete()
        runner.record_cleanup(succeeded=True)
        store.save(runner.session)
        restored = BetaSessionStore(store.path).load()
        checks.append(
            _check(
                "session_restart",
                "passed" if restored == runner.session else "failed",
                "session_restart_preserved" if restored == runner.session else "session_restart_mismatch",
            )
        )

        serialized = json.dumps(runner.session.model_dump(mode="json"), sort_keys=True)
        export_path = root / "session-export.json"
        runner.export(export_path)
        export_text = export_path.read_text(encoding="utf-8")
        export_safe = (
            export_path.stat().st_size <= 32_768
            and str(root) not in export_text
            and original_source.strip() not in export_text
            and "approval_token" not in export_text.lower()
            and "credential" not in export_text.lower()
            and "transcript" not in export_text.lower()
            and repair_payload_is_safe(json.loads(export_text))
        )
        checks.append(
            _check(
                "bounded_safe_export",
                "passed" if export_safe else "failed",
                "metadata_only_export" if export_safe else "unsafe_export",
            )
        )
        checks.append(
            _check(
                "automatic_actions_disabled",
                "passed"
                if not runner.session.automatic_actions_enabled
                else "failed",
                "automatic_actions_locked_off"
                if not runner.session.automatic_actions_enabled
                else "automatic_actions_enabled",
            )
        )

        execution = summarize_execution((runner.session,), evidence_basis="contract_only")

        # Keep the serialized projection referenced so this routine's privacy
        # check also covers the in-memory session representation.
        if not repair_payload_is_safe(json.loads(serialized)):
            checks[-2] = _check("bounded_safe_export", "failed", "unsafe_session_projection")

    for name, status, reason in (
        ("live_provider", "unavailable", "live_provider_not_executed"),
        ("public_repository", "unavailable", "public_repository_not_authorized"),
        ("github", "not_run", "github_dry_run_not_requested"),
        ("extension", "not_run", "extension_host_not_executed"),
        ("platform_matrix", "not_run", "platform_matrix_not_executed"),
        ("toolchain_matrix", "not_run", "toolchain_matrix_not_executed"),
    ):
        checks.append(_check(name, status, reason, basis="unavailable"))

    result = OfflineSessionEvaluation(
        generated_at=now,
        status="passed_with_limitations"
        if all(item.status == "passed" for item in checks[:4])
        else "failed",
        checks=tuple(checks),
        session=runner.session,
        execution=execution,
    )
    return result


def write_offline_evaluation(result: OfflineSessionEvaluation, destination: str | Path) -> None:
    """Publish a new bounded evaluation file through the existing safe exporter."""
    write_beta_export(result, destination)


__all__ = [
    "OfflineEvaluationCheck",
    "OfflineSessionEvaluation",
    "evaluate_offline_session",
    "write_offline_evaluation",
]
