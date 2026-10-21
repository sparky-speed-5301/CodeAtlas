from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeatlas.beta_offline import evaluate_offline_session, write_offline_evaluation


def test_offline_evaluator_runs_owned_fixture_and_preserves_unavailable_states() -> None:
    result = evaluate_offline_session()

    assert result.status == "passed_with_limitations"
    assert result.evidence_basis == "contract_only"
    assert result.production_ready is False
    assert result.acceptance_complete is False
    assert result.session.provider_mode == "offline"
    assert result.session.lifecycle_status == "completed"
    assert result.session.cleanup_status == "completed"
    assert result.execution.session_count == 1
    assert tuple(check.name for check in result.checks) == (
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
    )
    assert result.checks[0].status == "passed"
    assert result.checks[4].status == "unavailable"
    assert result.checks[6].status == "not_run"


def test_offline_evaluator_export_is_bounded_metadata_only(tmp_path: Path) -> None:
    result = evaluate_offline_session()
    output = tmp_path / "offline-evaluation.json"

    write_offline_evaluation(result, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    encoded = json.dumps(payload).lower()

    assert output.stat().st_size <= 32_768
    assert "approval_token" not in encoded
    assert "credential" not in encoded
    assert "provider_transcript" not in encoded
    assert "source_code" not in encoded
    assert payload["production_ready"] is False

    with pytest.raises(ValueError, match="new regular file"):
        write_offline_evaluation(result, output)
