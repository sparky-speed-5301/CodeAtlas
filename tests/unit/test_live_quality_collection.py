from types import SimpleNamespace

import pytest

from codeatlas.benchmark.quality_collection import collect_live_review_quality
from codeatlas.review.packet import ReviewPacket
from codeatlas.review.provider import ReviewerResult
from tests.unit.test_quality_collection import _case


class FakeReviewer:
    name = "live"
    version = "test-v1"

    def __init__(self, result: ReviewerResult) -> None:
        self.result = result
        self.calls = 0
        self.config = SimpleNamespace(enabled=True, dry_run=False, timeout_seconds=1,
                                      max_response_bytes=32_000, max_packet_bytes=32_000, model_name="safe-test-model")

    def review(self, packet: ReviewPacket) -> ReviewerResult:
        self.calls += 1
        return self.result


def _packet() -> ReviewPacket:
    return ReviewPacket(packet_id="test-packet", repository="public/example", base_commit="1" * 40,
                        head_commit="2" * 40, changed_files=["src/app.py"])


def test_live_review_is_separate_and_does_not_persist_transcripts(monkeypatch) -> None:
    monkeypatch.setenv("QUALITY_TEST_KEY", "synthetic-test-credential")
    provider = FakeReviewer(ReviewerResult(provider_name="live", validation_status="accepted", summary="raw response text"))
    attribution, observation = collect_live_review_quality(_case(), _packet(), provider=provider,
        allow_live=True, network_allowed=True, credential_env="QUALITY_TEST_KEY")
    assert attribution.provider_mode_used == "live"
    assert observation is not None and observation.mode == "live_provider"
    assert observation.cost is None
    assert "raw response text" not in attribution.model_dump_json() + observation.model_dump_json()


@pytest.mark.parametrize("reason", ["timeout", "rate_limited", "malformed_json_response"])
def test_live_failures_are_unavailable(monkeypatch, reason) -> None:
    monkeypatch.setenv("QUALITY_TEST_KEY", "synthetic-test-credential")
    provider = FakeReviewer(ReviewerResult(provider_name="live", validation_status="failed", limitations=[reason]))
    attribution, observation = collect_live_review_quality(_case(), _packet(), provider=provider,
        allow_live=True, network_allowed=True, credential_env="QUALITY_TEST_KEY")
    assert attribution.provider_mode_used == "unavailable"
    assert attribution.reason == reason
    assert observation is None


def test_live_review_provider_identity_checked(monkeypatch) -> None:
    monkeypatch.setenv("QUALITY_TEST_KEY", "synthetic-test-credential")
    provider = FakeReviewer(ReviewerResult(provider_name="another-provider"))
    attribution, observation = collect_live_review_quality(_case(), _packet(), provider=provider,
        allow_live=True, network_allowed=True, credential_env="QUALITY_TEST_KEY")
    assert attribution.reason == "provider_identity_mismatch"
    assert observation is None
