from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeatlas.benchmark.quality_collection import (
    collect_deterministic_reviews,
    collect_repair_quality,
    quality_report,
)
from codeatlas.benchmark.repository import RepositoryBenchmarkCase, load_benchmark_suite
from tests.unit.test_provider_collection import OnlineProvider, _output, _request


class BoundedOnlineProvider(OnlineProvider):
    def __init__(self, failure: str | None = None) -> None:
        super().__init__(failure)
        self.config = SimpleNamespace(model_name="safe-model-v1", timeout_seconds=1.0, max_response_bytes=32_000)


def _case() -> RepositoryBenchmarkCase:
    suite = load_benchmark_suite("eval/cases/real-repositories/v1/suite.json")
    data = suite.cases[1].model_dump(mode="python")
    data.update(repository_identity="public/example", base_sha="1" * 40, head_sha="2" * 40,
                changed_files=("src/app.py",), expected_finding_anchors=(),
                expected_finding_categories=(), expected_severity=(), population="negative")
    return RepositoryBenchmarkCase.model_validate(data)


def test_actual_deterministic_collection_is_reproducible() -> None:
    path = Path("eval/cases/real-repositories/v1/suite.json")
    first = collect_deterministic_reviews(path)
    second = collect_deterministic_reviews(path)
    assert len(first) == 12
    assert [(x.case_id, x.findings) for x in first] == [(x.case_id, x.findings) for x in second]
    report = quality_report(load_benchmark_suite(path), first)
    assert report["repair_reports"] == []
    assert "code_context" not in json.dumps(report)


@pytest.mark.parametrize("reason,allow,network,credential", [
    ("live_not_selected", False, False, None),
    ("network_disabled", True, False, None),
    ("missing_credentials", True, True, None),
])
def test_live_unavailable_never_substitutes_offline(reason, allow, network, credential) -> None:
    provider = BoundedOnlineProvider()
    attribution, result = collect_repair_quality(_case(), _request(), mode="live", provider=provider,
                                                allow_live=allow, network_allowed=network, credential_env=credential)
    assert attribution.status == "unavailable"
    assert attribution.provider_mode_used == "unavailable"
    assert attribution.reason == reason
    assert result is None
    assert provider.calls == 0


@pytest.mark.parametrize("failure", ["timeout", "rate_limit", "schema_failure", "provider_unavailable"])
def test_failed_live_is_attributed_without_fallback(monkeypatch, failure) -> None:
    monkeypatch.setenv("BENCHMARK_TEST_CREDENTIAL", "synthetic-test-value")
    provider = BoundedOnlineProvider(failure)
    attribution, result = collect_repair_quality(_case(), _request(), mode="live", provider=provider,
        allow_live=True, network_allowed=True, credential_env="BENCHMARK_TEST_CREDENTIAL")
    assert attribution.reason == failure
    assert attribution.provider_mode_used == "unavailable"
    assert result is None
    assert provider.calls == 1


def test_explicit_fallback_keeps_attribution_and_no_transcript(monkeypatch) -> None:
    from codeatlas.providers import DeterministicRepairProvider

    monkeypatch.setenv("BENCHMARK_TEST_CREDENTIAL", "synthetic-test-value")
    attribution, result = collect_repair_quality(_case(), _request(), mode="fallback", provider=BoundedOnlineProvider("timeout"),
        allow_live=True, network_allowed=True, credential_env="BENCHMARK_TEST_CREDENTIAL",
        fallback_provider=DeterministicRepairProvider(lambda request: _output()))
    assert attribution.provider_mode_used == "fallback"
    assert attribution.fallback_trigger == "timeout"
    assert result is not None and result.mode == "deterministic"
    assert result.targeted_tests == result.full_suite == "not_run"
    payload = attribution.model_dump_json() + result.model_dump_json()
    assert "return value" not in payload
    assert "synthetic-test-value" not in payload


def test_context_identity_mismatch_prevents_call() -> None:
    provider = BoundedOnlineProvider()
    case = _case().model_copy(update={"base_sha": "3" * 40})
    attribution, result = collect_repair_quality(case, _request(), mode="live", provider=provider)
    assert attribution.reason == "context_identity_mismatch"
    assert provider.calls == 0
    assert result is None
