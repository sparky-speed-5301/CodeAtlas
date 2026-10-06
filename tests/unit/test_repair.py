"""Repair foundation trust boundaries, bounded scope, and pipeline compatibility."""

from __future__ import annotations

import io
import json
import socket
import subprocess
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from codeatlas.adapters import JavaScriptAdapter, PythonAdapter, TypeScriptAdapter, select_language_adapter
from codeatlas.evidence import EvidenceLogger
from codeatlas.git.snapshot import Snapshot
from codeatlas.orchestrator import (
    RepairContext, RepairLimits, RepairOrchestrator, RepairRejected, RepairResult, RunManifest, record_repair_result,
)
from codeatlas.patching.parser import parse_unified_diff
from codeatlas.patching.proposal import compute_patch_hash, generate_approval_token, verify_approval_token
from codeatlas.patching.suggestions import preview_patch
from codeatlas.review.packet import ContextItem
from codeatlas.review.provider import ReviewerResult
from codeatlas.verification.command_policy import validate_check_command, validate_test_command
from eval.eval_repair import CASES, OfflineRepairReviewer, SOURCES, evaluate_case, repair_request, repair_suggestion


@pytest.fixture
def request_data(tmp_path):
    return repair_request(tmp_path)


def _record(finding, request, **updates):
    finding = finding.model_copy(update=updates)
    request["manifest"].findings = [finding.model_dump(mode="json")]
    return finding


@pytest.mark.parametrize("kind", CASES)
def test_repair_evaluation(kind):
    result = evaluate_case(kind)
    assert result["pass"], result


@pytest.mark.parametrize("language,path", [("python", "src/calc.py"), ("javascript", "src/calc.js"), ("typescript", "src/calc.ts")])
def test_proposal_only_uses_existing_static_pipeline_and_never_executes(tmp_path, monkeypatch, language, path):
    finding, request = repair_request(tmp_path, path, language)
    workspace = Path(request["state"].repository)
    workspace.mkdir()
    dirty = workspace / "draft.txt"
    dirty.write_text("Intentional uncommitted user work", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    manifest_before = request["manifest"].model_dump()
    packet_before = request["packet"].model_dump()

    def forbidden(*args, **kwargs):
        pytest.fail("Repair invoked execution, network, approval generation, or isolated application")

    for method in ("run", "Popen", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, method, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("codeatlas.patching.proposal.generate_approval_token", forbidden)
    monkeypatch.setattr("codeatlas.patching.validator.apply_patch_in_isolated_sandbox", forbidden)
    monkeypatch.setattr("codeatlas.verification.executor.execute_test_command", forbidden)
    import codeatlas.orchestrator.repair as module
    original = module.materialize_patch_suggestions
    calls = []

    def materialize(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "materialize_patch_suggestions", materialize)
    provider = OfflineRepairReviewer(repair_suggestion(path, language))
    stream = io.StringIO()
    result = RepairOrchestrator(provider).plan(finding, evidence=EvidenceLogger(stream), **request)
    assert result.status == "proposed", result.reason
    assert provider.calls == len(calls) == 1
    proposal = result.proposal
    assert proposal.approval_required and result.approval_required
    assert proposal.status == "requires_human_approval"
    assert proposal.patch_hash == compute_patch_hash(proposal.unified_diff)
    assert proposal.base_commit == request["state"].head_commit
    assert proposal.provenance["review_base_commit"] == request["state"].base_commit
    assert proposal.policy_decision["allowed"] and proposal.redaction_audit.safe
    assert result.validation.valid and result.validation.approval_verified is False
    assert result.validation.applies_cleanly is False  # No actual application was observed.
    assert result.validation.syntax_valid is (True if language == "python" else None)
    assert result.validation.tests_status == result.validation.build_status == "not_run"
    assert result.validation.sandbox_id is None and not result.validation.commands_run
    assert not result.validation.execution_allowed and not result.validation.network_allowed
    assert not result.validation.dependency_install_allowed
    assert request["manifest"].model_dump() == manifest_before
    assert request["packet"].model_dump() == packet_before
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    logged = stream.getvalue()
    assert "return value" not in logged and "unified_diff" not in logged and "CAT-APP-" not in logged
    events = [json.loads(line) for line in logged.splitlines()]
    assert events[-1]["approval_required"] and events[-1]["automatic_application_blocked"]


@pytest.mark.parametrize("updates,reason", [
    ({"status": "abstained"}, "abstained"), ({"quality_decision": "suppress_duplicate"}, "suppressed_duplicate"),
    ({"status": "review_only"}, "review_only_without_fix_eligibility"),
    ({"quality_decision": "review_only"}, "review_only_without_fix_eligibility"),
    ({"quality_decision": "suppress_low_evidence"}, "low_evidence"),
    ({"evidence_strength": "weak"}, "low_evidence"), ({"confidence": 0.69}, "low_evidence"),
    ({"evidence": []}, "low_evidence"), ({"quality_version": "obsolete"}, "low_evidence"),
    ({"ambiguity_score": 0.1}, "ambiguous"), ({"unsupported_flow": True}, "unsupported_flow"),
    ({"fixability": "unknown"}, "finding_not_fix_eligible"),
    ({"start_line": 99, "end_line": 99}, "invalid_location"),
    ({"provenance": {"origin": "reviewer"}}, "low_evidence"),
])
def test_ineligible_findings_never_reach_provider(request_data, updates, reason):
    finding, request = request_data
    finding = _record(finding, request, **updates)
    provider = OfflineRepairReviewer()
    result = RepairOrchestrator(provider).plan(finding, **request)
    assert result.status == "rejected" and result.reason == reason
    assert result.proposal is None and provider.calls == 0


def test_review_only_requires_explicit_boolean_eligibility(request_data):
    finding, request = request_data
    finding = _record(finding, request, status="review_only", quality_decision="review_only")
    provider = OfflineRepairReviewer()
    orchestrator = RepairOrchestrator(provider)
    assert orchestrator.plan(finding, fix_eligible="true", **request).status == "rejected"
    assert orchestrator.plan(finding, fix_eligible=True, **request).status == "proposed"
    assert provider.calls == 1


@pytest.mark.parametrize("part,field,value,reason", [
    ("state", "run_id", "other-run", "stale_run"),
    ("state", "head_commit", "new-head", "incompatible_repository_state"),
    ("state", "base_commit", "new-base", "incompatible_repository_state"),
    ("state", "repository", "other-repo", "incompatible_repository_state"),
    ("packet", "packet_id", "new-packet", "incompatible_repository_state"),
    ("packet", "head_commit", "new-head", "incompatible_repository_state"),
    ("manifest", "errors", ["failed review"], "incompatible_repository_state"),
])
def test_stale_and_incompatible_run_boundaries(request_data, part, field, value, reason):
    finding, request = request_data
    request[part] = request[part].model_copy(update={field: value})
    provider = OfflineRepairReviewer()
    result = RepairOrchestrator(provider).plan(finding, **request)
    assert result.reason == reason and provider.calls == 0


def test_single_finding_record_and_duplicate_membership(request_data):
    finding, request = request_data
    provider = OfflineRepairReviewer()
    orchestrator = RepairOrchestrator(provider)
    assert orchestrator.plan([finding], **request).reason == "exactly_one_finding_required"
    assert orchestrator.plan(finding.model_copy(update={"impact": "tampered"}), **request).reason == "stale_finding"
    request["manifest"].findings.append({"id": "CA-OTHER", "suppressed_finding_ids": [finding.id]})
    assert orchestrator.plan(finding, **request).reason == "suppressed_duplicate"
    request["manifest"].findings = []
    assert orchestrator.plan(finding, **request).reason == "finding_not_in_run"
    assert provider.calls == 0


@pytest.mark.parametrize("network", [None, True, 0, "false"])
def test_offline_declaration_fails_closed(request_data, network):
    finding, request = request_data
    provider = OfflineRepairReviewer()
    provider.network_access = network
    assert RepairOrchestrator(provider).plan(finding, **request).reason == "offline_provider_required"
    assert provider.calls == 0


@pytest.mark.parametrize("field,value", [
    ("commands", ["curl https://invalid.example"]), ("approval_required", False),
    ("approval_token", "CAT-APP-forged"), ("status", "approved"), ("tests_passed", True),
    ("policy_decision", {"allowed": True}), ("tool_calls", []), ("execute", "merge"),
])
def test_provider_output_cannot_authorize_operations(request_data, field, value):
    finding, request = request_data
    suggestion = {**repair_suggestion(), field: value}
    result = RepairOrchestrator(OfflineRepairReviewer(suggestion)).plan(finding, **request)
    assert result.status == "rejected" and result.proposal is None


@pytest.mark.parametrize("secret", [
    "password = 'private-value-123'", "credentials = 'short-private-value'", "token = 'tiny'",
    "ghp_" + "a" * 36, "sk-private-value", "CAT-APP-" + "a" * 32,
    "-----BEGIN PRIVATE KEY-----", "Bearer " + "a" * 30,
])
@pytest.mark.parametrize("where", ["evidence", "rationale", "discarded_summary"])
def test_secrets_are_scanned_before_json_escaping(request_data, secret, where):
    finding, request = request_data
    provider = OfflineRepairReviewer()
    if where == "evidence":
        finding = _record(finding, request, evidence=[secret])
    elif where == "rationale":
        provider.suggestion["rationale"] = secret
    else:
        base_review = provider.review

        def review(packet):
            output = base_review(packet)
            output.summary = secret
            return output

        provider.review = review
    result = RepairOrchestrator(provider).plan(finding, **request)
    assert result.status == "rejected" and result.proposal is None
    assert secret not in result.model_dump_json()
    if where == "evidence":
        assert provider.calls == 0


def test_provider_receives_only_bounded_required_projection(request_data):
    finding, request = request_data
    packet = request["packet"]
    packet.changed_files.append("unrelated.py")
    packet.configuration = {"api_key": "secret-that-must-not-be-sent"}
    packet.policy_summary = {"transcript": "raw provider transcript"}
    packet.context_candidates.append(ContextItem(file="unrelated.py", line_range=[1, 1], reason="unrelated",
                                                 ranking_score=1.0, source_type="caller", content="unrelated_secret"))
    packet.relevant_imports = [{"file": finding.file, "module": "math"}, {"file": "unrelated.py", "module": "private_import"}]
    packet.relevant_tests = ["tests/test_calc.py", "tests/test_unrelated.py", "../../tests/test_calc.py"]
    finding = _record(finding, request, provenance={**finding.provenance, "transcript": "raw provider transcript"})
    provider = OfflineRepairReviewer()
    result = RepairOrchestrator(provider).plan(finding, **request)
    assert result.status == "proposed", result.reason
    received = provider.received.model_dump_json()
    for excluded in ("secret-that-must-not-be-sent", "raw provider transcript", "unrelated_secret", "private_import", "test_unrelated"):
        assert excluded not in received
    assert provider.received.changed_files == [finding.file]
    assert result.context.related_tests == ("tests/test_calc.py",)
    assert result.context.repository_context == ("math",)
    assert result.context.repository_identity == "repository"
    assert len(received.encode()) < 48_000


@pytest.mark.parametrize("limits,reason", [
    (RepairLimits(max_source_bytes=5), "unreadable_or_unbounded_target"),
    (RepairLimits(max_context_lines=1), "required_context_exceeds_bounds"),
    (RepairLimits(max_context_bytes=5), "required_context_exceeds_bounds"),
    (RepairLimits(max_patch_bytes=20), "patch_parse_failed"),
    (RepairLimits(max_patch_lines=1), "patch_parse_failed"),
    (RepairLimits(prohibited_paths=("src/**",)), "protected_or_generated_target"),
])
def test_request_and_patch_budgets(request_data, limits, reason):
    finding, request = request_data
    result = RepairOrchestrator(OfflineRepairReviewer(), limits=limits).plan(finding, **request)
    assert result.reason == reason and result.proposal is None


@pytest.mark.parametrize("mutation,reason", [
    ("truncated", "stale_or_truncated_context"), ("stale", "stale_or_truncated_context"),
    ("missing", "missing_required_context"), ("redaction", "packet_redaction_failed"),
])
def test_required_context_freshness(request_data, mutation, reason):
    finding, request = request_data
    if mutation == "truncated":
        request["packet"].context_candidates[0].truncation_status = "truncated"
    elif mutation == "stale":
        request["packet"].context_candidates[0].content = "stale source"
    elif mutation == "missing":
        request["packet"].context_candidates = []
    else:
        request["packet"].redaction_status.redacted = False
    provider = OfflineRepairReviewer()
    assert RepairOrchestrator(provider).plan(finding, **request).reason == reason
    assert provider.calls == 0


@pytest.mark.parametrize("diff,reason", [
    ("garbage", "patch_parse_failed"),
    (repair_suggestion()["unified_diff"].replace("a/src/calc.py", "a/../../private.py"), "patch_scope_invalid"),
    (repair_suggestion()["unified_diff"].replace("b/src/calc.py", "b/src/other.py"), "patch_scope_invalid"),
    (repair_suggestion()["unified_diff"].replace("-2 +2", "-2 +20"), "patch_preview_failed"),
    (repair_suggestion()["unified_diff"].replace("// 2", "// 0"), "empty_repair"),
    (repair_suggestion()["unified_diff"].replace("+    return value // 2", "+    return ("), "syntax_validation_failed"),
    (repair_suggestion()["unified_diff"].replace("-    return value // 0", "-    return value // 9"), "patch_preview_failed"),
])
def test_malformed_conflicting_noop_and_unsafe_patches(request_data, diff, reason):
    finding, request = request_data
    result = RepairOrchestrator(OfflineRepairReviewer({**repair_suggestion(), "unified_diff": diff})).plan(finding, **request)
    assert result.reason == reason and result.proposal is None


def test_hunk_context_cannot_anchor_an_unrelated_edit(request_data):
    finding, request = request_data
    diff = "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n-def compute(value):\n+def unrelated(value):\n     return value // 0\n"
    result = RepairOrchestrator(OfflineRepairReviewer({**repair_suggestion(), "unified_diff": diff})).plan(finding, **request)
    assert result.reason == "patch_scope_invalid"


def test_provider_packet_mutation_does_not_change_authoritative_inputs(request_data):
    finding, request = request_data
    provider = OfflineRepairReviewer()
    base_review = provider.review

    def review(packet):
        output = base_review(packet)
        packet.changed_files.clear()
        packet.changed_line_ranges.clear()
        return output

    provider.review = review
    assert RepairOrchestrator(provider).plan(finding, **request).status == "proposed"
    assert request["packet"].changed_files == [finding.file]


@pytest.mark.parametrize("output", [
    "not json", "[]", '{"patch_suggestions": [], "patch_suggestions": []}',
    {"patch_suggestions": [repair_suggestion()], "commands": ["git commit"]},
    {"patch_suggestions": [repair_suggestion(), repair_suggestion()]},
    {"patch_suggestions": [repair_suggestion()], "abstentions": ["insufficient context"]},
])
def test_structured_repair_operation_rejects_bad_envelopes(request_data, output):
    class Provider:
        name = "offline"
        version = "1"
        network_access = False

        def propose(self, context):
            return output

    finding, request = request_data
    result = RepairOrchestrator(Provider()).plan(finding, **request)
    assert result.status == "rejected" and result.proposal is None


def test_structured_json_repair_operation(request_data):
    class Provider:
        name = "offline"
        version = "1"
        network_access = False

        def propose(self, context):
            assert isinstance(context, RepairContext)
            return json.dumps(repair_suggestion())

    finding, request = request_data
    assert RepairOrchestrator(Provider()).plan(finding, **request).status == "proposed"


def test_manifest_schema_proposal_token_compatibility_and_idempotency(request_data):
    finding, request = request_data
    result = RepairOrchestrator(OfflineRepairReviewer()).plan(finding, **request)
    manifest = record_repair_result(request["manifest"], result)
    assert request["manifest"].patch_proposals == []
    assert manifest.patch_proposal_ids == [result.proposal.proposal_id]
    assert record_repair_result(manifest, result).model_dump() == manifest.model_dump()
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas/run-manifest.schema.json").read_text())
    jsonschema.validate(manifest.model_dump(mode="json"), schema)
    context_schema = json.loads((Path(__file__).resolve().parents[2] / "schemas/repair-context.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(context_schema)
    jsonschema.validate(result.context.model_dump(mode="json"), context_schema)
    token = generate_approval_token(result.proposal.proposal_id, result.proposal.base_commit,
                                    result.proposal.patch_hash, result.proposal.target_files, request["state"].run_id)
    assert verify_approval_token(token, result.proposal, result.proposal.base_commit, request["state"].run_id)[0]
    assert not verify_approval_token(token, result.proposal, result.proposal.base_commit, "other-run")[0]
    other = manifest.model_copy(update={"run_id": "other-run"})
    with pytest.raises(RepairRejected, match="incompatible_repository_state"):
        record_repair_result(other, result)


def test_context_bounds_immutable_contract_and_result_approval(request_data):
    finding, request = request_data
    result = RepairOrchestrator(OfflineRepairReviewer()).plan(finding, **request)
    assert RepairContext.model_validate_json(result.context.model_dump_json()) == result.context
    data = result.context.model_dump()
    for field, value in (("approval_token", "forged"), ("prohibited_paths", ("other/**",)), ("confidence", float("nan")),
                         ("deterministic_evidence", ("evidence",) * 11), ("code_context", "x" * 17_000)):
        with pytest.raises(ValidationError):
            RepairContext.model_validate({**data, field: value})
    with pytest.raises(ValidationError):
        result.context.claim = "changed"
    with pytest.raises(ValidationError):
        RepairResult.model_validate({**result.model_dump(), "approval_required": False})
    with pytest.raises(ValidationError):
        RepairLimits(max_target_files=2)


@pytest.mark.parametrize("path,language", [("a.py", "python"), ("a.js", "javascript"), ("a.jsx", "javascript"),
                                           ("a.ts", "typescript"), ("a.tsx", "typescript"), ("a.rs", None), ("a.go", None)])
def test_language_selection(path, language):
    adapter = select_language_adapter(path)
    assert (adapter.language if adapter else None) == language


@pytest.mark.parametrize("adapter", [PythonAdapter(), JavaScriptAdapter(), TypeScriptAdapter()])
def test_adapter_tools_discovery_and_command_policy(adapter, monkeypatch):
    monkeypatch.setattr("codeatlas.adapters.base.shutil.which", lambda name: None)
    monkeypatch.setattr("codeatlas.adapters.base._is_pytest_available", lambda: False)
    tools = adapter.report_tool_availability()
    assert tools["formatter"] is False
    assert adapter.produce_allowlisted_commands([], tools) == ()
    path = "src/calc.py" if adapter.language == "python" else "src/calc.ts" if adapter.language == "typescript" else "src/calc.js"
    test = "tests/test_calc.py" if adapter.language == "python" else "tests/calc.test.ts"
    available = [test, "tests/test_unrelated.py", "../../tests/test_calc.py", "tests/calc.test.js;curl", "C:/tests/test_calc.py"]
    assert adapter.discover_targeted_tests(path, available) == (test,)
    commands = adapter.produce_allowlisted_commands([test], {"pytest": True, "vitest": True})
    assert commands and all(validate_test_command(command).allowed for command in commands)
    assert adapter.produce_allowlisted_commands(["tests/test_calc.py;curl"], {"pytest": True, "vitest": True}) == ()
    checks = adapter.allowlisted_check_commands(path, {"ruff": True, "compiler": True, "eslint": True, "prettier": True})
    assert checks and all(validate_check_command(command).allowed for command in checks)
    assert adapter.allowlisted_check_commands("../escape.py", {"ruff": True, "prettier": True}) == ()


@pytest.mark.parametrize("path", ["tests/test_calc.py", ".github/workflows/build.py", "setup.py", "vendor/calc.py",
                                 "src/calc.generated.ts", "src/calc.d.ts", "src/calc.min.js", "node_modules/calc.js"])
def test_protected_generated_targets(path):
    adapter = select_language_adapter(path)
    assert adapter.identify_protected_generated_files(path)


def test_language_parser_limitations_and_regex_literals():
    assert PythonAdapter().syntax_check("a.py", "def bad(:\n").syntax_valid is False
    assert JavaScriptAdapter().syntax_check("a.js", "const pattern = /[)]/;\n").errors == ()
    assert JavaScriptAdapter().syntax_check("a.js", "function bad() {\n").syntax_valid is False
    parsed = TypeScriptAdapter().syntax_check("a.ts", "const value: number = 2;\n")
    assert parsed.syntax_valid is None and parsed.limitations


@pytest.mark.parametrize("command", [("ruff", "check", "--fix", "src/a.py"), ("npm", "install", "tool"),
                                     ("eslint", "--fix", "src/a.js"), ("black", "src/a.py"),
                                     ("prettier", "--check", "--write"), ("tsc", "--noEmit", "--pretty", "false", "C:/a.ts")])
def test_adapter_check_policy_rejects_writing_or_unsafe_commands(command):
    assert not validate_check_command(command).allowed


def test_memory_preview_handles_insertion_and_rejects_overlapping_hunks():
    patch, errors = parse_unified_diff("--- a/a.py\n+++ b/a.py\n@@ -1,0 +2 @@\n+second = 2\n")
    assert not errors
    assert preview_patch("first = 1\n", patch[0]) == "first = 1\nsecond = 2\n"
    patch[0].hunks.append(patch[0].hunks[0])
    with pytest.raises(ValueError):
        preview_patch("first = 1\n", patch[0])


def test_optional_context_truncation_is_explicit(request_data):
    finding, request = request_data
    request["packet"].relevant_imports = [{"file": finding.file, "module": f"module_{i}"} for i in range(12)]
    result = RepairOrchestrator(OfflineRepairReviewer()).plan(finding, **request)
    assert result.status == "proposed" and result.context.context_truncated
    assert len(result.context.repository_context) == 10
    assert result.proposal.provenance["context_truncated"] is True
    assert any("truncated" in limitation for limitation in result.limitations)


def test_snapshot_change_during_provider_call_is_rejected(request_data):
    finding, request = request_data
    target = request["snapshot"].path / finding.file
    provider = OfflineRepairReviewer()
    base_review = provider.review

    def review(packet):
        target.write_text(target.read_text() + "# concurrent snapshot change\n", encoding="utf-8")
        return base_review(packet)

    provider.review = review
    result = RepairOrchestrator(provider).plan(finding, **request)
    assert result.reason == "repository_state_changed_during_repair" and result.proposal is None


def test_original_workspace_is_not_a_snapshot(request_data):
    finding, request = request_data
    request["snapshot"] = Snapshot(Path(request["state"].repository), request["state"].head_commit)
    provider = OfflineRepairReviewer()
    assert RepairOrchestrator(provider).plan(finding, **request).reason == "incompatible_repository_state"
    assert provider.calls == 0


def test_noop_crlf_patch_is_not_a_repair(request_data):
    finding, request = request_data
    target = request["snapshot"].path / finding.file
    target.write_bytes(SOURCES["python"].replace("\n", "\r\n").encode())
    suggestion = repair_suggestion()
    suggestion["unified_diff"] = suggestion["unified_diff"].replace("// 2", "// 0")
    assert RepairOrchestrator(OfflineRepairReviewer(suggestion)).plan(finding, **request).reason == "empty_repair"


@pytest.mark.parametrize("operation", ["add", "delete", "rename", "multi_file"])
def test_unsupported_file_operations_and_scope(request_data, operation):
    finding, request = request_data
    suggestion = repair_suggestion()
    if operation == "add":
        suggestion["unified_diff"] = "--- /dev/null\n+++ b/src/calc.py\n@@ -0,0 +1 @@\n+value = 1\n"
    elif operation == "delete":
        suggestion["unified_diff"] = "--- a/src/calc.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def compute(value):\n-    return value // 0\n"
    elif operation == "rename":
        suggestion["unified_diff"] = "rename from src/calc.py\nrename to src/calc2.py\n"
    else:
        suggestion["unified_diff"] += repair_suggestion("src/other.py")["unified_diff"]
    result = RepairOrchestrator(OfflineRepairReviewer(suggestion)).plan(finding, **request)
    assert result.status == "rejected" and result.proposal is None


def test_out_of_context_hunk_and_oversized_provider_output(request_data):
    finding, request = request_data
    target = request["snapshot"].path / finding.file
    target.write_text(SOURCES["python"] + "\ndef unrelated():\n    return 1\n", encoding="utf-8")
    suggestion = repair_suggestion()
    suggestion["unified_diff"] += "@@ -4,2 +4,2 @@\n def unrelated():\n-    return 1\n+    return 2\n"
    assert RepairOrchestrator(OfflineRepairReviewer(suggestion)).plan(finding, **request).reason == "patch_scope_invalid"
    suggestion["rationale"] = "x" * 50_000
    assert RepairOrchestrator(OfflineRepairReviewer(suggestion)).plan(finding, **request).reason == "provider_output_exceeds_bounds"


def test_manifest_copy_preserves_existing_proposals(request_data):
    finding, request = request_data
    manifest = request["manifest"]
    manifest.patch_proposals = [{"proposal_id": "existing"}]
    manifest.patch_proposal_ids = ["existing"]
    manifest.patch_proposal_statuses = ["requires_human_approval"]
    result = RepairOrchestrator(OfflineRepairReviewer()).plan(finding, **request)
    updated = record_repair_result(manifest, result)
    assert updated.patch_proposal_ids == ["existing", result.proposal.proposal_id]
    assert updated.patch_proposals[0] == {"proposal_id": "existing"}
    assert manifest.patch_proposal_ids == ["existing"]


def test_propose_raises_bounded_rejection_and_does_not_retry(request_data):
    finding, request = request_data
    provider = OfflineRepairReviewer()

    def broken(packet):
        provider.calls += 1
        raise RuntimeError("raw transcript and private details")

    provider.review = broken
    with pytest.raises(RepairRejected, match="^provider_failed$"):
        RepairOrchestrator(provider).propose(finding, **request)
    assert provider.calls == 1
