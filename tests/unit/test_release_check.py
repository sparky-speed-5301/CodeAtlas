from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.release_check import DEFAULT_REQUIRED_DOCUMENTS, run_release_check


def test_release_check_blocks_without_explicit_evidence() -> None:
    result = run_release_check()

    assert result.decision == "blocked"
    assert "missing_required_evidence" in result.blocking_conditions
    assert result.production_ready is False
    assert len(result.checks) == 8


def test_release_check_requires_explicit_artifact_and_recovery_evidence(tmp_path: Path) -> None:
    for relative in DEFAULT_REQUIRED_DOCUMENTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("controlled release instructions\n", encoding="utf-8")

    plan = tmp_path / "plan.json"
    plan.write_text(
        Path("config/controlled-beta-evidence-plan.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = run_release_check(repository_root=tmp_path, plan_path=plan)

    assert result.decision == "blocked"
    assert "missing_required_evidence" in result.blocking_conditions
    assert any(item.reason_code == "missing_artifact_evidence" for item in result.checks)


def test_release_check_cli_returns_blocked_exit_and_machine_decision(tmp_path: Path) -> None:
    output = tmp_path / "release-check.json"
    result = CliRunner().invoke(
        app,
        [
            "--json",
            "release-check",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["decision"] == "blocked"
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["decision"] == "blocked"
