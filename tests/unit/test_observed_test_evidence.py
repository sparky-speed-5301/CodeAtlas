"""Phase 8C identity, redaction, bounds, policy, and schema contracts."""

import io
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from codeatlas.evidence import EvidenceLogger
from codeatlas.orchestrator.manifest import RunManifest
from codeatlas.orchestrator.validation import build_validation_review
from codeatlas.patching.models import PatchStatus, PatchValidationResult
from codeatlas.patching.proposal import create_patch_proposal
from codeatlas.review.packet import (
    MAX_EVIDENCE_BYTES, OBSERVED_TEST_DISCLAIMER, ObservedTestEvidence,
    ReviewPacket, attach_observed_test_evidence,
)
from codeatlas.review.rendering import render_test_evidence
from codeatlas.verification.models import TestPlan, TestResult


@pytest.fixture
def observation():
    proposal = create_patch_proposal(
        finding_id="CA-TEST", provider_name="operator", provider_version="1",
        base_commit="a" * 40, target_files=["src/calc.py"],
        unified_diff="--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        rationale="fixture", expected_behavior="fixture",
    )
    proposal.status = PatchStatus.VALIDATED
    plan = TestPlan(language="python", exact_command=["python", "-m", "pytest", "tests/test_calc.py"], test_targets=["tests/test_calc.py"])
    result = TestResult(
        proposal_id=proposal.proposal_id, sandbox_id="sb-1",
        status="passed", runner="pytest", language="python", command=plan.exact_command,
        tests_run=plan.test_targets, tests_passed=3, tests_skipped=1, duration_ms=12.5,
        execution_allowed=True, execution_started=True, exit_code=0,
        redaction_audit={"safe": True, "rules_applied": ["regex_secret_scrubbing"], "targets_audited": ["stdout", "stderr"]},
    )
    validation = PatchValidationResult(
        proposal_id=proposal.proposal_id, sandbox_id="sb-1", base_commit=proposal.base_commit,
        patch_hash=proposal.patch_hash, approval_scope="default", approval_verified=True,
        applies_cleanly=True, patch_applied_in_isolated_sandbox=True, syntax_valid=True,
        test_execution_attempted=True, execution_allowed=True,
        tests_status="passed", test_plan=plan.model_dump(), test_result=result,
    )
    return proposal, result, validation


def attach(observation, packet=None):
    proposal, result, validation = observation
    return attach_observed_test_evidence(packet or ReviewPacket(packet_id="pkt", repository="repo"), proposal, result, validation)


def test_targeted_attachment_and_rendering(observation):
    packet = attach(observation)
    observed = packet.observed_test_evidence
    assert observed is not None
    assert observed.tests_passed == 3 and observed.tests_skipped == 1
    assert observed.targets == ["tests/test_calc.py"]
    assert observed.runner == "pytest" and observed.language == "python"
    rendered = render_test_evidence(packet)
    for text in ("targeted=passed", "pytest", "tests/test_calc.py", "3 passed / 0 failed / 1 skipped", "12.50 ms", "Redaction: passed", "network_isolation_verified=false", OBSERVED_TEST_DISCLAIMER):
        assert text in rendered


@pytest.mark.parametrize("full_suite", [False, True])
@pytest.mark.parametrize("status", ["passed", "failed", "timed_out", "error"])
def test_executed_scope_policy_and_manifest(observation, full_suite, status):
    proposal, result, validation = observation
    result.status = status
    result.tests_failed = 0 if status == "passed" else 1
    validation.tests_status = status
    if full_suite:
        result.full_suite = True
        plan = TestPlan.model_validate(validation.test_plan)
        plan.full_suite = True
        validation.full_suite_test_plan = plan
        validation.full_suite_result = result.model_dump()
        validation.full_suite_status = status
        validation.full_suite_requested = validation.full_suite_policy_opted_in = True
        validation.tests_status = "not_run"
    packet, manifest = build_validation_review(proposal, validation, repository="repo")
    assert packet.observed_test_evidence is not None
    assert packet.observed_test_evidence.full_suite is full_suite
    assert packet.policy_summary["decision"] == "requires_human_approval"
    assert proposal.status == ("validated" if status == "passed" else "failed_validation")
    assert manifest.approval_scope == "default"
    assert manifest.patch_hash == proposal.patch_hash
    assert manifest.test_evidence_attached is True
    assert manifest.network_isolation_verified is False
    assert manifest.automatic_application_attempted is False
    assert OBSERVED_TEST_DISCLAIMER in manifest.validation_limitations
    assert RunManifest.model_validate(manifest.model_dump()) == manifest


@pytest.mark.parametrize("field,value,reason", [
    ("status", "not_run", "not_run"),
    ("status", "blocked", "blocked"),
    ("execution_allowed", False, "execution_not_observed"),
    ("execution_started", False, "execution_not_observed"),
    ("redaction_audit", {}, "output_redaction_failed"),
    ("redaction_audit", {"safe": False}, "output_redaction_failed"),
    ("proposal_id", "other", "proposal_identity_mismatch"),
    ("proposal_id", None, "proposal_identity_mismatch"),
    ("sandbox_id", "other", "sandbox_identity_mismatch"),
    ("sandbox_id", None, "sandbox_identity_mismatch"),
    ("runner", "arbitrary", "unsupported_runner_or_command"),
    ("duration_ms", float("inf"), "unbounded_or_invalid_result"),
    ("tests_passed", -1, "unbounded_or_invalid_result"),
    ("tests_run", ["unrelated.py"], "test_plan_mismatch"),
])
def test_result_exclusion(observation, field, value, reason):
    packet_with_old_evidence = attach(observation)
    setattr(observation[1], field, value)
    packet = attach(observation, packet_with_old_evidence)
    assert packet.observed_test_evidence is None
    assert packet.test_evidence_exclusion_reason == reason
    assert packet_with_old_evidence.observed_test_evidence is not None  # no mutation


@pytest.mark.parametrize("field,value,reason", [
    ("approval_verified", False, "approval_not_verified"),
    ("applies_cleanly", False, "isolated_patch_application_not_verified"),
    ("patch_applied_in_isolated_sandbox", False, "isolated_patch_application_not_verified"),
    ("syntax_valid", False, "isolated_patch_application_not_verified"),
    ("proposal_id", "other", "proposal_identity_mismatch"),
    ("sandbox_id", "other", "sandbox_identity_mismatch"),
    ("base_commit", "other", "patch_identity_mismatch"),
    ("patch_hash", "other", "patch_identity_mismatch"),
])
def test_validation_exclusion(observation, field, value, reason):
    setattr(observation[2], field, value)
    packet = attach(observation)
    assert packet.observed_test_evidence is None
    assert packet.test_evidence_exclusion_reason == reason


@pytest.mark.parametrize("field", ["proposal_id", "sandbox_id", "base_commit"])
def test_packet_identity_binding(observation, field):
    packet = ReviewPacket(packet_id="pkt", repository="repo", **{field: "other"})
    assert attach(observation, packet).observed_test_evidence is None


def test_original_review_packet_binds_to_reviewed_head(observation):
    packet = ReviewPacket(packet_id="original", repository="repo", base_commit="b" * 40, head_commit=observation[0].base_commit)
    result = attach(observation, packet)
    assert result.observed_test_evidence is not None
    assert result.base_commit == "b" * 40
    assert result.head_commit == observation[0].base_commit


def test_manifest_does_not_claim_cross_proposal_approval(observation):
    observation[2].proposal_id = "other"
    packet, manifest = build_validation_review(observation[0], observation[2], repository="repo")
    assert packet.observed_test_evidence is None
    assert manifest.approval_verified is False


def test_no_validation_record_or_scope_mismatch(observation):
    proposal, result, validation = observation
    packet = ReviewPacket(packet_id="pkt", repository="repo")
    assert attach_observed_test_evidence(packet, proposal, result).observed_test_evidence is None
    packet, manifest = build_validation_review(proposal, validation, repository="repo", run_id="wrong")
    assert packet.observed_test_evidence is None
    assert manifest.approval_verified is False


@pytest.mark.parametrize("field", ["failure_summary", "stack_trace_summary", "stdout_summary", "stderr_summary", "failed_test_names", "diagnostics_limitations"])
def test_fresh_redaction_audit_rejects_leaks(observation, field):
    secret = "ghp_" + "X" * 36
    setattr(observation[1], field, [secret] if field in {"failed_test_names", "diagnostics_limitations"} else secret)
    packet, manifest = build_validation_review(observation[0], observation[2], repository="repo")
    assert packet.observed_test_evidence is None
    assert packet.test_evidence_exclusion_reason == "output_redaction_failed"
    assert secret not in packet.model_dump_json() + manifest.model_dump_json() + render_test_evidence(packet)


def test_successful_redaction_counts_are_not_residual_leaks(observation):
    observation[1].redaction_audit.update(raw_value_matches=2, redacted_items_count=2)
    observation[1].failure_summary = "[REDACTED]"
    assert attach(observation).observed_test_evidence.redaction_audit.redacted_items_count == 2


def test_bounds_multibyte_diagnostics_and_truncation(observation):
    result = observation[1]
    result.failed_test_names = ["測" * 200 for _ in range(30)]
    result.failure_summary = "測" * 2000
    result.stack_trace_summary = "\n".join("測" * 1000 for _ in range(40))
    result.output_truncated = True
    observed = attach(observation).observed_test_evidence
    assert observed is not None
    assert len(observed.failed_test_names) == 10
    assert len(observed.failure_summary) <= 500
    assert len(observed.stack_trace_summary.splitlines()) <= 6
    diagnostics = observed.failure_summary + observed.stack_trace_summary + "".join(observed.failed_test_names)
    assert len(diagnostics.encode()) <= 4000
    assert len(observed.model_dump_json().encode()) <= MAX_EVIDENCE_BYTES
    assert observed.output_truncated is True
    assert any("bounded" in lim for lim in observed.limitations)


def test_unbounded_targets_excluded_without_misrepresenting_scope(observation):
    observation[1].tests_run = [f"tests/test_{n}.py" for n in range(65)]
    observation[2].test_plan["test_targets"] = observation[1].tests_run
    packet = attach(observation)
    assert packet.observed_test_evidence is None
    assert packet.test_evidence_exclusion_reason == "unbounded_or_invalid_result"


def test_packet_budget(observation):
    packet = ReviewPacket(packet_id="pkt", repository="repo", configuration={"review": {"max_packet_bytes": 800}})
    result = attach(observation, packet)
    assert result.observed_test_evidence is None
    assert result.test_evidence_exclusion_reason == "packet_byte_budget_exceeded"


def test_network_isolation_is_never_inferred(observation):
    observation[1].network_isolation_verified = True  # unsupported claim
    observation[2].network_isolation_verified = True
    packet, manifest = build_validation_review(observation[0], observation[2], repository="repo")
    assert packet.network_isolation_verified is False
    assert packet.observed_test_evidence.network_isolation_verified is False
    assert manifest.network_isolation_verified is False
    assert packet.observed_test_evidence.network_policy_enforced is True


def test_failed_tests_preserve_failed_validation_and_log_metadata(observation):
    proposal, result, validation = observation
    proposal.status = "failed_validation"
    result.status = validation.tests_status = "failed"
    result.tests_failed = 1
    stream = io.StringIO()
    packet, manifest = build_validation_review(proposal, validation, repository="repo", evidence=EvidenceLogger(stream))
    assert proposal.status == "failed_validation"
    assert manifest.patch_proposal_statuses == ["failed_validation"]
    assert packet.policy_summary["human_approval_required"] is True
    event = json.loads(stream.getvalue())
    assert event["event"] == "observed_test_evidence_attached"
    assert event["proposal_id"] == proposal.proposal_id
    assert event["network_isolation_verified"] is False


@pytest.mark.parametrize("status", ["blocked", "not_run"])
def test_exclusions_are_visible_in_manifest_and_packet(observation, status):
    proposal, result, validation = observation
    result.status = validation.tests_status = status
    packet, manifest = build_validation_review(proposal, validation, repository="repo")
    assert packet.observed_test_evidence is None
    assert f"targeted={status}" in render_test_evidence(packet)
    assert f"targeted={status}" in manifest.test_evidence_summary
    assert manifest.test_evidence_attached is False
    assert manifest.network_isolation_verified is False


def test_json_schema_validation_and_rejected_unbounded_evidence(observation):
    packet, manifest = build_validation_review(observation[0], observation[2], repository="repo")
    root = Path(__file__).resolve().parents[2] / "schemas"
    evidence_schema = json.loads((root / "observed-test-evidence.schema.json").read_text())
    manifest_schema = json.loads((root / "run-manifest.schema.json").read_text())
    for schema in (evidence_schema, manifest_schema, ReviewPacket.model_json_schema()):
        jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(packet.model_dump(mode="json"), ReviewPacket.model_json_schema())
    jsonschema.validate(manifest.model_dump(mode="json"), manifest_schema)
    data = packet.observed_test_evidence.model_dump(mode="json")
    jsonschema.validate(data, evidence_schema)
    for field, value in (("failed_test_names", ["test"] * 11), ("failure_summary", "x" * 501), ("status", "blocked"), ("limitations", [])):
        invalid = {**data, field: value}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, evidence_schema)
        with pytest.raises(ValidationError):
            ObservedTestEvidence.model_validate(invalid)
    invalid_manifest = manifest.model_dump(mode="json")
    del invalid_manifest["approval_scope"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid_manifest, manifest_schema)


@pytest.mark.parametrize("strength", ["weak", "supported", "reproduced"])
def test_pass_does_not_upgrade_finding_or_approval(observation, strength):
    finding = dict(
        id="CA-TEST", file="src/calc.py", start_line=1, end_line=1,
        severity="low", category="bug", claim="A finding", impact="A possible defect",
        evidence_strength=strength, confidence=0.9, fixability="suggested", status="suggested",
    )
    packet = ReviewPacket(packet_id="pkt", repository="repo", deterministic_findings=[finding])
    proposal = observation[0]
    proposal.status = "requires_human_approval"
    packet, manifest = build_validation_review(proposal, observation[2], repository="repo", packet=packet)
    assert packet.deterministic_findings == [finding]
    assert proposal.status == "requires_human_approval"
    assert manifest.policy_decisions[0]["human_approval_required"] is True


def test_phase8c_evaluation():
    from eval.eval_observed_evidence import run_observed_evidence_eval
    results = run_observed_evidence_eval()
    assert len(results) == 10
    assert all(result["pass"] for result in results), results
