from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from codeatlas.benchmark.provider_comparison import (
    ProviderComparisonControl,
    ProviderComparisonObservation,
    ProviderPatchLimits,
    load_provider_comparisons,
    score_provider_comparisons,
)
from codeatlas.benchmark.repository import (
    RepositoryBenchmarkSuite,
    load_benchmark_suite,
)
from codeatlas.benchmark.runner import run_benchmark_files
from codeatlas.cli import app

ROOT = Path("eval/cases/real-repositories/v1")


def _suite() -> RepositoryBenchmarkSuite:
    return load_benchmark_suite(ROOT / "suite.json")


def _observation(mode: str = "offline", **updates: object) -> ProviderComparisonObservation:
    suite = _suite()
    case = next(case for case in suite.cases if case.case_id == "real-bug")
    control = ProviderComparisonControl(
        repository_identity=case.repository_identity,
        base_sha=case.base_sha,
        head_sha=case.head_sha,
        finding_anchor_id=case.expected_finding_anchors[0].anchor_id,
        context_hash="a" * 64,
        request_hash="b" * 64,
        policy_version="11C-A.1",
        patch_limits=ProviderPatchLimits(
            max_target_files=case.expected_patch_scope.max_files,
            max_patch_lines=case.expected_patch_scope.max_changed_lines,
            max_patch_bytes=64_000,
        ),
        validation_policy="static-only-v1",
        language=case.language,
        target_files=case.expected_patch_scope.allowed_files,
    )
    data = {
        "comparison_id": "real-bug-comparison",
        "case_id": case.case_id,
        "run_mode": mode,
        "provider_name": "offline" if mode in {"offline", "fallback"} else "openai_compatible",
        "model_name": None if mode in {"offline", "fallback"} else "safe-model-v1",
        "provider_mode": "offline" if mode in {"offline", "fallback"} else "online",
        "provider_contacted": mode in {"live", "fallback"},
        "fallback_used": mode == "fallback",
        "fallback_trigger_code": "timeout" if mode == "fallback" else None,
        "control": control.model_dump(mode="json"),
        "request_size_bytes": 1000,
        "response_size_bytes": 0 if mode == "dry_run" else 500,
        "duration_ms": 0 if mode == "dry_run" else 20,
        "outcome": "not_run" if mode == "dry_run" else "completed",
        "patch_hash": None if mode == "dry_run" else "c" * 64,
        "validation_status": "not_run" if mode == "dry_run" else "static_validation_passed",
        "syntax": "not_run" if mode == "dry_run" else "passed",
    }
    data.update(updates)
    return ProviderComparisonObservation.model_validate(data)


def test_all_run_modes_are_separate_and_controlled() -> None:
    observations = [_observation(mode) for mode in ("offline", "live", "fallback", "dry_run")]
    report = score_provider_comparisons(_suite(), observations)
    assert report.comparison_count == 1
    assert report.run_count == 4
    assert [summary.run_mode for summary in report.mode_summaries] == ["offline", "live", "fallback", "dry_run"]
    assert report.comparisons[0].missing_modes == ()
    assert report.mode_summaries[2].fallback_uses == 1
    assert report.mode_summaries[3].provider_contacts == 0
    assert report.mode_summaries[3].patches_generated == 0


@pytest.mark.parametrize("field", ["context_hash", "request_hash", "policy_version", "validation_policy", "patch_limits"])
def test_mismatched_controls_are_rejected(field: str) -> None:
    offline = _observation()
    live_data = _observation("live").model_dump(mode="json")
    if field == "patch_limits":
        live_data["control"][field]["max_patch_bytes"] = 32_000
    elif field.endswith("hash"):
        live_data["control"][field] = "d" * 64
    else:
        live_data["control"][field] = "changed-v2"
    live = ProviderComparisonObservation.model_validate(live_data)
    with pytest.raises(ValueError, match="not controlled"):
        score_provider_comparisons(_suite(), [offline, live])


@pytest.mark.parametrize("field", ["repository_identity", "base_sha", "head_sha", "finding_anchor_id", "language", "target_files"])
def test_case_identity_and_target_controls_are_enforced(field: str) -> None:
    data = _observation().model_dump(mode="json")
    replacements = {
        "repository_identity": "other/repository",
        "base_sha": "e" * 40,
        "head_sha": "f" * 40,
        "finding_anchor_id": "other-anchor",
        "language": ["javascript"],
        "target_files": ["other/file.py"],
    }
    data["control"][field] = replacements[field]
    observation = ProviderComparisonObservation.model_validate(data)
    with pytest.raises(ValueError, match="does not match"):
        score_provider_comparisons(_suite(), [observation])


@pytest.mark.parametrize("field,value", [
    ("raw_prompt", "unrestricted source"),
    ("raw_transcript", "provider output"),
    ("credentials", "credential-value"),
    ("approval_token", "approval-value"),
    ("diagnostics", "unredacted output"),
])
def test_forbidden_metadata_fields_are_rejected_without_echo(field: str, value: str) -> None:
    data = _observation().model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError) as caught:
        ProviderComparisonObservation.model_validate(data)
    assert value not in str(caught.value)


@pytest.mark.parametrize("field", ["provider_name", "model_name"])
def test_secret_or_transcript_material_cannot_hide_in_names(field: str) -> None:
    value = "sk-this-is-not-a-real-provider-key"
    with pytest.raises(ValidationError) as caught:
        _observation(**{field: value})
    assert value not in str(caught.value)


@pytest.mark.parametrize("updates", [
    {"provider_contacted": True},
    {"response_size_bytes": 1},
    {"input_tokens": 1},
    {"retry_count": 1},
    {"patch_hash": "d" * 64},
    {"syntax": "passed"},
    {"outcome": "completed"},
])
def test_dry_run_has_no_provider_side_effect_claims(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="dry_run"):
        _observation("dry_run", **updates)


def test_fallback_records_primary_trigger_and_effective_provider() -> None:
    observation = _observation("fallback", outcome="failed", failure_code="network_disabled", patch_hash=None, validation_status="not_run", syntax="not_run")
    assert observation.provider_name == "offline"
    assert observation.fallback_trigger_code == "timeout"
    assert observation.failure_code == "network_disabled"
    with pytest.raises(ValidationError, match="fallback"):
        _observation("fallback", fallback_trigger_code=None)


def test_reported_tests_must_obey_controlled_policy() -> None:
    with pytest.raises(ValidationError, match="test policy"):
        _observation("live", targeted_tests="passed", validation_status="validated_in_sandbox")
    with pytest.raises(ValidationError, match="test policy"):
        _observation("live", full_suite="passed", validation_status="validated_in_sandbox")


def test_unknown_failure_codes_and_unbounded_usage_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _observation("live", outcome="failed", failure_code="raw provider error")
    with pytest.raises(ValidationError):
        _observation("live", input_tokens=2_000_001)
    with pytest.raises(ValidationError):
        _observation("live", duration_ms=float("nan"))


def test_comparison_order_is_stable_and_missing_modes_are_explicit() -> None:
    offline = _observation()
    live = _observation("live")
    first = score_provider_comparisons(_suite(), [offline, live])
    second = score_provider_comparisons(_suite(), [live, offline])
    assert first == second
    assert first.comparisons[0].missing_modes == ("fallback", "dry_run")
    with pytest.raises(ValueError, match="duplicate"):
        score_provider_comparisons(_suite(), [offline, offline])


def test_cli_scores_comparison_only_and_protects_input(tmp_path: Path) -> None:
    comparison_path = tmp_path / "comparisons.json"
    comparison_path.write_text(json.dumps([_observation(mode).model_dump(mode="json") for mode in ("offline", "live", "fallback", "dry_run")]), encoding="utf-8")
    result = CliRunner().invoke(app, ["benchmark", "--suite", str(ROOT / "suite.json"), "--provider-comparisons", str(comparison_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["provider_comparison_report"]["run_count"] == 4
    assert payload["review_reports"] == []
    before = comparison_path.read_bytes()
    blocked = CliRunner().invoke(app, ["benchmark", "--suite", str(ROOT / "suite.json"), "--provider-comparisons", str(comparison_path), "--output", str(comparison_path)])
    assert blocked.exit_code == 2
    assert comparison_path.read_bytes() == before


def test_comparison_runner_is_network_and_execution_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import urllib.request

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("scoring must not execute or contact a provider")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    path = tmp_path / "observations.json"
    path.write_text(json.dumps([_observation().model_dump(mode="json")]), encoding="utf-8")
    report = run_benchmark_files(ROOT / "suite.json", provider_comparisons_path=path)
    assert report["provider_comparison_report"]["run_count"] == 1
    assert len(load_provider_comparisons(path)) == 1
