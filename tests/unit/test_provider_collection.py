from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeatlas.benchmark.provider_collection import collect_provider_comparison
from codeatlas.orchestrator.repair_models import RepairContext
from codeatlas.providers import (
    DeterministicRepairProvider,
    RepairFinding,
    RepairLanguageCapabilities,
    RepairOutput,
    RepairPolicy,
    RepairProviderFailure,
    RepairRequest,
)


def _request() -> RepairRequest:
    context = RepairContext(
        finding_id="CA-BUG-1", run_id="comparison-run", repository_identity="public/example",
        base_commit="1" * 40, head_commit="2" * 40, language="python",
        changed_file="src/app.py", changed_line_range=(2, 2), containing_symbol=None,
        code_line_range=(1, 2), code_context="def total(value):\n    return value // 0\n",
        category="CORRECTNESS", claim="Zero divisor", impact="Raises at runtime",
        deterministic_evidence=("Literal zero divisor",), quality_decision="report",
        confidence=0.9, evidence_strength="supported",
    )
    finding = RepairFinding(
        id=context.finding_id, file=context.changed_file, start_line=2, end_line=2,
        category=context.category, claim=context.claim, impact=context.impact,
        quality_decision="report", confidence=0.9, evidence_strength="supported",
        evidence=context.deterministic_evidence,
    )
    return RepairRequest(
        finding=finding, context=context,
        policy=RepairPolicy(policy_version=context.policy_version),
        capabilities=RepairLanguageCapabilities(language="python"),
    )


def _output() -> RepairOutput:
    return RepairOutput(
        diagnosis="Zero divisor", patch="--- a/src/app.py\n+++ b/src/app.py\n@@ -2 +2 @@\n-    return value // 0\n+    return value // 2\n",
        target_files=["src/app.py"], explanation="Use the reviewed divisor",
        assumptions=[], expected_behavior_change="Avoid zero division",
        risk_level="medium", confidence=0.8, tests_to_run=[], limitations=[],
    )


class OnlineProvider:
    name = "openai_compatible"
    version = "test-v1"
    network_access = True

    def __init__(self, failure: str | None = None) -> None:
        self.config = SimpleNamespace(model_name="safe-model-v1")
        self.failure = failure
        self.calls = 0

    def generate(self, request: RepairRequest) -> RepairOutput:
        self.calls += 1
        if self.failure:
            raise RepairProviderFailure(self.failure)
        return _output()


def _collect(mode: str, provider: object, **kwargs: object):
    return collect_provider_comparison(
        _request(), comparison_id="comparison-1", case_id="case-1",
        finding_anchor_id="anchor-1", validation_policy="static-only-v1",
        run_mode=mode, provider=provider, **kwargs,
    )


def test_offline_collection_keeps_hashes_without_source_or_response() -> None:
    provider = DeterministicRepairProvider(lambda request: _output())
    result = _collect("offline", provider)
    assert provider.calls == 1
    assert result.patch_hash
    assert result.control.context_hash
    assert result.control.request_hash
    assert result.validation_status == "not_run"
    serialized = result.model_dump_json()
    assert "return value" not in serialized
    assert "Zero divisor" not in serialized
    assert result.response_size_bytes > 0


def test_live_collection_requires_opt_in_and_dry_run_has_zero_calls() -> None:
    provider = OnlineProvider()
    with pytest.raises(ValueError, match="allow_live"):
        _collect("live", provider)
    assert provider.calls == 0
    dry = _collect("dry_run", provider)
    assert provider.calls == 0
    assert dry.run_mode == "dry_run"
    assert dry.response_size_bytes == 0
    assert dry.patch_hash is None
    live = _collect("live", provider, allow_live=True)
    assert provider.calls == 1
    assert live.provider_contacted


def test_fallback_collection_preserves_primary_failure_and_effective_provider() -> None:
    primary = OnlineProvider("timeout")
    fallback = DeterministicRepairProvider(lambda request: _output())
    result = _collect("fallback", primary, fallback_provider=fallback, allow_live=True)
    assert primary.calls == fallback.calls == 1
    assert result.run_mode == "fallback"
    assert result.provider_name == "offline"
    assert result.fallback_trigger_code == "timeout"
    assert result.failure_code is None


def test_fallback_is_not_claimed_when_primary_succeeds() -> None:
    primary = OnlineProvider()
    fallback = DeterministicRepairProvider(lambda request: _output())
    result = _collect("fallback", primary, fallback_provider=fallback, allow_live=True)
    assert primary.calls == 1
    assert fallback.calls == 0
    assert result.run_mode == "live"
    assert not result.fallback_used


def test_provider_errors_are_mapped_without_transcript_retention() -> None:
    class BadProvider(OnlineProvider):
        def generate(self, request: RepairRequest) -> RepairOutput:
            raise RuntimeError("unredacted provider transcript and credential")

    result = _collect("live", BadProvider(), allow_live=True)
    assert result.failure_code == "provider_unavailable"
    assert "transcript" not in result.model_dump_json()
