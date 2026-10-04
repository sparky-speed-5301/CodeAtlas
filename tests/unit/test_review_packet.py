"""Unit and integration tests for Phase 5 review packet, policy gating, and reviewer provider."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch
import pytest

from codeatlas.findings.models import Finding
from codeatlas.git.models import ChangeStatus, Diff, FileChange, LineRange
from codeatlas.orchestrator.review import run_review
from codeatlas.repository.models import ContextCandidate, RepositoryIndex, Symbol
from codeatlas.review import (
    ContextItem,
    MockReviewer,
    PacketSizeStats,
    PolicyDecision,
    RedactionAudit,
    ReviewPacket,
    ReviewerResult,
    assemble_review_packet,
    audit_packet_redaction,
    evaluate_policy,
    merge_and_rank_findings,
    redact_text,
    validate_provider_output,
)


def _make_dummy_diff(file_path: str = "src/app.py", start: int = 10, count: int = 5) -> Diff:
    change = FileChange(
        path=file_path,
        old_path=None,
        status=ChangeStatus.MODIFIED,
        old_ranges=(LineRange(start=start, count=count),),
        new_ranges=(LineRange(start=start, count=count),),
    )
    return Diff("base", "head", files=(change,))


def test_review_packet_schema_and_deterministic_id(tmp_path: Path):
    f = tmp_path / "src" / "app.py"
    f.parent.mkdir(parents=True)
    f.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    diff = _make_dummy_diff("src/app.py", 1, 2)
    p1 = assemble_review_packet(tmp_path, diff, base_commit="c1", head_commit="c2")
    p2 = assemble_review_packet(tmp_path, diff, base_commit="c1", head_commit="c2")

    assert p1.packet_id == p2.packet_id
    assert p1.packet_id.startswith("pkt-")
    assert isinstance(p1.redaction_status, RedactionAudit)
    assert isinstance(p1.packet_size_statistics, PacketSizeStats)
    assert p1.changed_files == ["src/app.py"]
    assert "src/app.py" in p1.changed_line_ranges


def test_packet_redaction_and_raw_secret_absence(tmp_path: Path):
    f = tmp_path / "src" / "secret.py"
    f.parent.mkdir(parents=True)
    secret_val = "AKIAIOSFODNN7EXAMPLE"
    f.write_text(f"api_key = '{secret_val}'\n", encoding="utf-8")

    diff = _make_dummy_diff("src/secret.py", 1, 1)
    packet = assemble_review_packet(tmp_path, diff)

    packet_json = json.dumps(packet.model_dump(mode="json"))
    assert secret_val not in packet_json
    assert packet.redaction_status.redacted is True
    assert packet.redaction_status.raw_value_matches == 0


def test_redaction_audit_catches_leaked_secret():
    leaked_dict = {
        "content": "Connecting to postgres://admin:secretpass@db.internal:5432/app",
        "nested": {"key": "AKIA1234567890ABCDEF"},
    }
    audit = audit_packet_redaction(leaked_dict)
    assert audit.redacted is False
    assert audit.raw_value_matches == 2
    assert len(audit.failed_checks) > 0


def test_bounded_context_size_limits_and_truncation(tmp_path: Path):
    f = tmp_path / "src" / "big.py"
    f.parent.mkdir(parents=True)
    f.write_text("\n".join(f"line_{i} = {i}" for i in range(1, 300)), encoding="utf-8")

    diff = _make_dummy_diff("src/big.py", 1, 250)
    config = {
        "review": {
            "max_lines_per_file": 50,
            "max_total_context_lines": 80,
            "max_packet_bytes": 5000,
        }
    }
    packet = assemble_review_packet(tmp_path, diff, config=config)

    assert packet.truncated is True
    assert any("truncated" in lim for lim in packet.limitations)
    for c in packet.context_candidates:
        assert c.lines_included <= 50


def test_context_retention_priority_ordering(tmp_path: Path):
    f1 = tmp_path / "src" / "changed.py"
    f2 = tmp_path / "src" / "util.py"
    f3 = tmp_path / "tests" / "test_app.py"
    f1.parent.mkdir(parents=True, exist_ok=True)
    f3.parent.mkdir(parents=True, exist_ok=True)
    f1.write_text("def changed(): pass\n", encoding="utf-8")
    f2.write_text("def util(): pass\n", encoding="utf-8")
    f3.write_text("def test_app(): pass\n", encoding="utf-8")

    diff = _make_dummy_diff("src/changed.py", 1, 1)

    c_changed = ContextCandidate(file="src/changed.py", line_range=[1, 1], reason="directly changed", score=1.0, is_directly_changed=True)
    c_util = ContextCandidate(file="src/util.py", line_range=[1, 1], reason="symbol callee", score=0.8, signals=["callee"])
    c_test = ContextCandidate(file="tests/test_app.py", line_range=[1, 1], reason="test candidate", score=0.6, is_test=True)

    config = {
        "review": {
            "max_context_files": 2,
        }
    }
    packet = assemble_review_packet(
        tmp_path,
        diff,
        context_candidates=[c_test, c_util, c_changed],  # passed out of priority order
        config=config,
    )

    included_files = [c.file for c in packet.context_candidates]
    assert "src/changed.py" in included_files
    assert len(included_files) <= 2
    assert any(ex["file"] == "tests/test_app.py" for ex in packet.excluded_candidates)


def test_policy_clean_allowed(tmp_path: Path):
    f = tmp_path / "src" / "clean.py"
    f.parent.mkdir(parents=True)
    f.write_text("def ok(): return 1\n", encoding="utf-8")

    diff = _make_dummy_diff("src/clean.py", 1, 1)
    packet = assemble_review_packet(tmp_path, diff)
    decision = evaluate_policy(packet, [])

    assert decision.allowed is True
    assert decision.decision == "allowed"
    assert decision.human_approval_required is False


def test_policy_redaction_failure_blocks(tmp_path: Path):
    diff = _make_dummy_diff("src/clean.py", 1, 1)
    packet = assemble_review_packet(tmp_path, diff)
    packet.redaction_status.redacted = False
    packet.redaction_status.raw_value_matches = 1
    packet.redaction_status.failed_checks = ["raw_marker:AKIA"]

    decision = evaluate_policy(packet, [])
    assert decision.allowed is False
    assert decision.decision == "blocked"
    assert any("Redaction validation failed" in r for r in decision.blocking_reasons)


def test_policy_blocker_and_high_severity_require_human():
    packet = ReviewPacket(
        packet_id="pkt-test",
        repository="test",
        changed_files=["src/app.py"],
        redaction_status=RedactionAudit(redacted=True),
    )
    blocker_finding = Finding(
        id="CA-BLK-1",
        file="src/app.py",
        start_line=1,
        end_line=2,
        severity="blocker",
        category="SECURITY",
        claim="Critical issue",
        impact="Denial of service",
        evidence_strength="supported",
        confidence=0.95,
        evidence=["Log evidence"],
        fixability="review_required",
        status="detected",
    )
    decision = evaluate_policy(packet, [blocker_finding])
    assert decision.allowed is True
    assert decision.decision == "requires_human_approval"
    assert decision.human_approval_required is True


def test_policy_abstain_on_truncated_changed_code(tmp_path: Path):
    c_item = ContextItem(
        file="src/app.py",
        line_range=[1, 10],
        reason="changed code",
        ranking_score=1.0,
        source_type="changed_code",
        truncation_status="truncated",
    )
    packet = ReviewPacket(
        packet_id="pkt-test",
        repository="test",
        changed_files=["src/app.py"],
        context_candidates=[c_item],
        redaction_status=RedactionAudit(redacted=True),
    )
    decision = evaluate_policy(packet, [], config={"policy": {"abstain_on_truncated_changed_code": True}})
    assert decision.decision == "abstain"
    assert any("mandates abstention" in r for r in decision.reasons)


def test_mock_reviewer_determinism():
    packet = ReviewPacket(
        packet_id="pkt-test",
        repository="test",
        changed_files=["src/app.py"],
        changed_line_ranges={"src/app.py": [[5, 10]]},
        redaction_status=RedactionAudit(redacted=True),
    )
    mock1 = MockReviewer(mode="echo_changed")
    mock2 = MockReviewer(mode="echo_changed")

    res1 = mock1.review(packet)
    res2 = mock2.review(packet)

    assert res1.findings == res2.findings
    assert len(res1.findings) == 1
    assert res1.findings[0]["file"] == "src/app.py"


def test_validator_rejects_invalid_json():
    packet = ReviewPacket(
        packet_id="pkt-test",
        repository="test",
        changed_files=["src/app.py"],
        redaction_status=RedactionAudit(redacted=True),
    )
    invalid_raw = [{"file": "src/app.py", "start_line": 1}]
    result = validate_provider_output(packet, invalid_raw)

    assert result.is_valid is False
    assert len(result.validation_errors) == 1
    assert "failed schema validation" in result.validation_errors[0]


def test_validator_rejects_out_of_bounds_path_and_lines():
    packet = ReviewPacket(
        packet_id="pkt-test",
        repository="test",
        changed_files=["src/app.py"],
        changed_line_ranges={"src/app.py": [[1, 10]]},
        redaction_status=RedactionAudit(redacted=True),
    )
    bad_path_raw = [{
        "id": "CA-1",
        "file": "outside/repo.py",
        "start_line": 1,
        "end_line": 2,
        "severity": "low",
        "category": "STYLE",
        "claim": "Test claim",
        "impact": "Test impact",
        "evidence_strength": "supported",
        "confidence": 0.9,
        "evidence": [],
        "fixability": "review_required",
        "status": "detected",
    }]
    res_path = validate_provider_output(packet, bad_path_raw)
    assert res_path.is_valid is False
    assert "outside review packet" in res_path.validation_errors[0]

    bad_line_raw = [{
        "id": "CA-2",
        "file": "src/app.py",
        "start_line": 10,
        "end_line": 5,  # end < start
        "severity": "low",
        "category": "STYLE",
        "claim": "Test claim",
        "impact": "Test impact",
        "evidence_strength": "supported",
        "confidence": 0.9,
        "evidence": [],
        "fixability": "review_required",
        "status": "detected",
    }]
    res_line = validate_provider_output(packet, bad_line_raw)
    assert res_line.is_valid is False
    assert "validation error" in res_line.validation_errors[0].lower() or "line" in res_line.validation_errors[0].lower()


def test_validator_rejects_secret_leak_and_unsupported_validated():
    packet = ReviewPacket(
        packet_id="pkt-test",
        repository="test",
        changed_files=["src/app.py"],
        changed_line_ranges={"src/app.py": [[1, 10]]},
        redaction_status=RedactionAudit(redacted=True),
    )
    leak_raw = [{
        "id": "CA-LK",
        "file": "src/app.py",
        "start_line": 1,
        "end_line": 2,
        "severity": "high",
        "category": "HARDCODED_SECRET",
        "claim": "Found AKIAIOSFODNN7EXAMPLE in code",
        "impact": "Credential exposure",
        "evidence_strength": "supported",
        "confidence": 0.95,
        "evidence": ["AKIAIOSFODNN7EXAMPLE"],
        "fixability": "review_required",
        "status": "detected",
    }]
    res_leak = validate_provider_output(packet, leak_raw)
    assert res_leak.is_valid is False
    assert any("unredacted secret data" in err for err in res_leak.validation_errors)

    validated_raw = [{
        "id": "CA-VAL",
        "file": "src/app.py",
        "start_line": 1,
        "end_line": 2,
        "severity": "low",
        "category": "STYLE",
        "claim": "Valid style fix",
        "impact": "None",
        "evidence_strength": "supported",
        "confidence": 0.9,
        "evidence": [],
        "fixability": "validated",
        "status": "detected",
    }]
    res_val = validate_provider_output(packet, validated_raw)
    assert res_val.is_valid is False
    assert any("claims fixability='validated'" in err for err in res_val.validation_errors)


def test_findings_merge_and_precedence():
    det_finding = Finding(
        id="CA-SEC-001",
        file="src/app.py",
        start_line=10,
        end_line=15,
        severity="high",
        category="HARDCODED_SECRET",
        claim="Deterministic secret detector found AWS key",
        impact="Credential compromise",
        evidence_strength="supported",
        confidence=0.95,
        evidence=["Pattern match on line 12"],
        fixability="review_required",
        status="detected",
    )

    rev_finding = Finding(
        id="CA-REV-101",
        file="src/app.py",
        start_line=12,
        end_line=14,
        severity="low",
        category="HARDCODED_SECRET",
        claim="Reviewer noticed AWS key format",
        impact="Minor issue",
        evidence_strength="weak",
        confidence=0.80,
        evidence=["Line 12 regex"],
        fixability="review_required",
        status="detected",
    )

    merged = merge_and_rank_findings([det_finding], [rev_finding])
    assert len(merged) == 1
    m = merged[0]
    # Deterministic precedence
    assert m.id == "CA-MRG-SEC-001"
    assert m.severity == "high"
    assert m.evidence_strength == "supported"
    assert m.provenance["origin"] == "merged"
    assert len(m.evidence) == 2


def test_cli_and_manifest_packet_integration(tmp_path: Path):
    import subprocess
    import sys

    # Initialize a small git repository
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    f = repo_dir / "main.py"
    f.write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_dir, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo_dir, check=True)

    time.sleep(0.05)
    f.write_text("print('hello')\nprint('world')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=repo_dir, check=True)

    packet_out = tmp_path / "packet.json"
    policy_out = tmp_path / "policy.json"
    manifest_out = tmp_path / "manifest.json"

    res = run_review(
        repo_dir,
        base="HEAD~1",
        head="HEAD",
        assemble_review_packet=True,
        review_provider="mock:echo_changed",
        packet_output=packet_out,
        policy_output=policy_out,
        manifest_output=manifest_out,
    )

    if res.manifest.errors:
        print("Manifest errors:", res.manifest.errors)
    assert res.manifest.review_packet_id is not None
    assert res.manifest.packet_bytes is not None and res.manifest.packet_bytes > 0
    assert packet_out.is_file()
    assert policy_out.is_file()
    assert manifest_out.is_file()

    packet_data = json.loads(packet_out.read_text(encoding="utf-8"))
    assert packet_data["packet_id"] == res.manifest.review_packet_id
    assert packet_data["redaction_status"]["redacted"] is True

    policy_data = json.loads(policy_out.read_text(encoding="utf-8"))
    assert policy_data["allowed"] is True
    assert res.manifest.provider_output_valid is True
    assert len(res.manifest.merged_findings) >= 1

    import jsonschema
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "run-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    manifest_data = json.loads(manifest_out.read_text(encoding="utf-8"))
    jsonschema.validate(manifest_data, schema)
