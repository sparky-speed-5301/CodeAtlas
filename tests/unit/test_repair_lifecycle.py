from __future__ import annotations

from pathlib import Path

from codeatlas.orchestrator import RepairLifecycleEvent, RepairVerificationLoop
from codeatlas.providers.repair_config import RepairProviderConfig
from codeatlas.providers.repair_models import RepairOutput
from eval.eval_repair import repair_request, repair_suggestion


class _StructuredFixtureProvider:
    name = "offline-lifecycle-fixture"
    version = "1"
    network_access = False

    def __init__(self, *, patch: str | None = None) -> None:
        self.patch = patch or repair_suggestion()["unified_diff"]
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return RepairOutput(
            diagnosis="The changed expression has a zero divisor.",
            patch=self.patch,
            target_files=[request.context.changed_file],
            explanation="Replace the divisor with the reviewed safe value.",
            expected_behavior_change="Avoid the zero divisor.",
            risk_level="medium",
            confidence=0.9,
        )


def test_repair_loop_reports_bounded_success_lifecycle(tmp_path: Path) -> None:
    finding, request = repair_request(tmp_path)
    provider = _StructuredFixtureProvider()
    result = RepairVerificationLoop(
        provider,
        config=RepairProviderConfig(provider_name="offline", mode="offline", enabled=False, max_repair_attempts=2),
    ).run(finding, fix_eligible=True, **request)

    assert result.status == "accepted"
    assert result.attempt_count == 1
    assert result.lifecycle[-1].stage == "finished"
    assert result.lifecycle[-1].status == "terminal"
    assert [item.stage for item in result.lifecycle] == [
        "proposal_requested",
        "proposal_generated",
        "finished",
    ]
    assert all(isinstance(item, RepairLifecycleEvent) for item in result.lifecycle)
    assert all("divisor" not in item.reason for item in result.lifecycle)


def test_repair_loop_reports_approval_block_without_claiming_validation(tmp_path: Path) -> None:
    finding, request = repair_request(tmp_path)
    result = RepairVerificationLoop(_StructuredFixtureProvider()).run(
        finding,
        fix_eligible=True,
        execute_checks=True,
        run_tests=True,
        **request,
    )

    assert result.status == "abstained"
    assert result.reason == "approval_required"
    assert result.verification is not None and result.verification.status == "blocked"
    assert [item.stage for item in result.lifecycle] == [
        "proposal_requested",
        "verification_blocked",
        "finished",
    ]
    assert result.validation is not None and result.validation.commands_run == []


def test_repair_lifecycle_is_bounded_and_serializable(tmp_path: Path) -> None:
    finding, request = repair_request(tmp_path)
    result = RepairVerificationLoop(_StructuredFixtureProvider()).run(finding, fix_eligible=True, **request)
    restored = type(result).model_validate_json(result.model_dump_json())

    assert restored.model_dump(mode="json") == result.model_dump(mode="json")
    assert len(restored.lifecycle) <= 24
    assert len(restored.attempts) <= 5
