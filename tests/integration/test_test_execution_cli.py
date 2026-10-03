"""Integration tests / CLI smoke tests for Phase 8A test execution."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest

from codeatlas.patching.apply import apply_patch_in_isolated_sandbox
from codeatlas.patching.models import PatchStatus
from codeatlas.patching.proposal import (
    compute_patch_hash,
    create_patch_proposal,
    generate_approval_token,
)

CALC_PY = "def add(a, b):\n    return a + b\n"
TEST_CALC_PY = "from src.calc import add\ndef test_add():\n    assert add(1, 2) == 3\n"
FAIL_TEST_CALC_PY = "from src.calc import add\ndef test_add():\n    assert add(1, 2) == 999\n"

DIFF_PASS = (
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def add(a, b):\n"
    "+    # safe add\n"
    "     return a + b\n"
)


def _init_repo(repo_dir: Path, test_content: str = TEST_CALC_PY) -> str:
    repo_dir.mkdir(parents=True)
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "calc.py").write_text(CALC_PY, encoding="utf-8")
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_calc.py").write_text(test_content, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "eval@example.invalid")
    git("config", "user.name", "eval")
    git("add", "-A")
    git("commit", "-qm", "init")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()


def test_cli_smoke_no_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8A-NO-TESTS",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Smoke test without --run-tests",
        expected_behavior="Passes isolated validation without running tests",
    )
    p_path = tmp_path / "proposal.json"
    p_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )
    rep_path = tmp_path / "report.json"

    cmd = [
        "python", "-m", "codeatlas.cli", "patch", "validate",
        "--proposal", str(p_path),
        "--repo", str(repo),
        "--base", base_sha,
        "--approval-token", token,
        "--run-id", "run-1",
        "--report-output", str(rep_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    assert report["valid"] is True
    assert report["proposal_status"] == "requires_human_approval"
    assert report["tests_status"] == "not_run"
    assert report["review_packet"]["observed_test_evidence"] is None
    assert report["human_approval_manifest"]["test_evidence_attached"] is False
    assert report["human_approval_manifest"]["approval_scope"] == "run-1"
    assert report["human_approval_manifest"]["network_isolation_verified"] is False


def test_cli_smoke_passing_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8A-PASS-TESTS",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Smoke test with --run-tests passing",
        expected_behavior="Runs targeted tests and passes",
    )
    p_path = tmp_path / "proposal.json"
    p_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )
    rep_path = tmp_path / "report.json"

    cmd = [
        "python", "-m", "codeatlas.cli", "patch", "validate",
        "--proposal", str(p_path),
        "--repo", str(repo),
        "--base", base_sha,
        "--approval-token", token,
        "--run-id", "run-1",
        "--run-tests",
        "--report-output", str(rep_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    assert report["valid"] is True
    assert report["proposal_status"] == "validated"
    assert report["tests_status"] == "passed"
    assert report["test_execution_attempted"] is True
    observed = report["review_packet"]["observed_test_evidence"]
    assert observed["status"] == "passed"
    assert observed["proposal_id"] == proposal.proposal_id
    assert observed["sandbox_id"] == report["sandbox_id"]
    assert observed["tests_passed"] == 1
    assert observed["network_isolation_verified"] is False
    assert report["human_approval_manifest"]["approval_scope"] == "run-1"
    assert report["human_approval_manifest"]["policy_decisions"][0]["decision"] == "requires_human_approval"


def test_cli_smoke_failing_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo, test_content=FAIL_TEST_CALC_PY)

    proposal = create_patch_proposal(
        finding_id="F-8A-FAIL-TESTS",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Smoke test with --run-tests failing",
        expected_behavior="Runs targeted tests and fails",
    )
    p_path = tmp_path / "proposal.json"
    p_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )
    rep_path = tmp_path / "report.json"

    cmd = [
        "python", "-m", "codeatlas.cli", "patch", "validate",
        "--proposal", str(p_path),
        "--repo", str(repo),
        "--base", base_sha,
        "--approval-token", token,
        "--run-id", "run-1",
        "--run-tests",
        "--report-output", str(rep_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 1
    assert "Test status: failed" in res.stdout
    assert "Runner: pytest" in res.stdout
    assert "Network isolation: not independently verified" in res.stdout

    report = json.loads(rep_path.read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert report["proposal_status"] == "failed_validation"
    assert report["tests_status"] == "failed"
    assert report["test_failure_count"] == 1
    assert report["network_policy_requested"] == "disabled"
    assert report["network_policy_enforced"] is True
    assert report["network_isolation_verified"] is False
    assert report["diagnostic_summary"] is not None
    assert report["review_packet"]["observed_test_evidence"]["status"] == "failed"
    assert report["human_approval_manifest"]["patch_proposal_statuses"] == ["failed_validation"]


def test_cli_smoke_blocked_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)

    proposal = create_patch_proposal(
        finding_id="F-8A-BLOCKED-TESTS",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=base_sha,
        target_files=["src/calc.py"],
        unified_diff=DIFF_PASS,
        rationale="Smoke test with blocked command",
        expected_behavior="Blocked by allowlist",
    )
    p_path = tmp_path / "proposal.json"
    p_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    token = generate_approval_token(
        proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files, run_id="run-1"
    )
    rep_path = tmp_path / "report.json"
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"test": {"override_command": ["curl", "https://example.com"]}}), encoding="utf-8")

    cmd = [
        "python", "-m", "codeatlas.cli", "patch", "validate",
        "--proposal", str(p_path),
        "--repo", str(repo),
        "--base", base_sha,
        "--approval-token", token,
        "--run-id", "run-1",
        "--run-tests",
        "--config", str(cfg_path),
        "--report-output", str(rep_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 1
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert report["proposal_status"] == "failed_validation"
    assert report["tests_status"] == "blocked"
    assert report["review_packet"]["observed_test_evidence"] is None
    assert "targeted=blocked" in report["human_approval_manifest"]["test_evidence_summary"]


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
@pytest.mark.parametrize("test_content,status", [(TEST_CALC_PY, "passed"), (FAIL_TEST_CALC_PY, "failed")])
def test_full_suite_review_artifacts(tmp_path, command, test_content, status):
    from typer.testing import CliRunner
    from codeatlas.cli import app
    import jsonschema

    repo = tmp_path / "repo"
    base_sha = _init_repo(repo, test_content)
    proposal = create_patch_proposal(
        finding_id="CA-8C", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Review evidence", expected_behavior="Observed scope only",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"test": {"allow_full_suite": True}}))
    report_path = tmp_path / "report.json"
    evidence_path = tmp_path / "events.jsonl"
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    res = CliRunner().invoke(app, [
        "patch", command, "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--run-full-suite", "--config", str(config_path),
        "--report-output", str(report_path), "--evidence-output", str(evidence_path),
    ])
    assert res.exit_code == (0 if status == "passed" else 1), res.output
    report = json.loads(report_path.read_text())
    packet = report["review_packet"]
    manifest = report["human_approval_manifest"]
    observed = packet["observed_test_evidence"]
    assert observed["status"] == status
    assert observed["full_suite"] is True
    assert observed["tests_passed" if status == "passed" else "tests_failed"] == 1
    assert observed["proposal_id"] == proposal.proposal_id
    assert observed["sandbox_id"] == manifest["sandbox_id"] == report["sandbox_id"]
    assert manifest["test_evidence_attached"] is True
    assert manifest["approval_verified"] is True
    assert manifest["network_isolation_verified"] is False
    assert packet["policy_summary"]["decision"] == "requires_human_approval"
    assert "Observed scope: full-suite" in res.output
    assert "Network isolation: not independently verified" in res.output
    events = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    assert events[-1]["event"] == "observed_test_evidence_attached"
    assert token not in json.dumps(report) + evidence_path.read_text() + res.output
    root = Path(__file__).resolve().parents[2] / "schemas"
    jsonschema.validate(manifest, json.loads((root / "run-manifest.schema.json").read_text()))
    jsonschema.validate(observed, json.loads((root / "observed-test-evidence.schema.json").read_text()))


def test_targeted_then_full_suite_preserves_both_records(tmp_path):
    from codeatlas.orchestrator.validation import build_validation_review
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-BOTH", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Both scopes", expected_behavior="Both scopes run",
    )
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    result = apply_patch_in_isolated_sandbox(
        proposal, repo, approval_token=token, run_tests=True, run_full_suite=True,
        test_config={"allow_full_suite": True},
    )
    assert result.valid is True
    assert result.test_result.status == result.full_suite_result["status"] == "passed"
    packet, manifest = build_validation_review(proposal, result, repository="repo")
    assert packet.tests_status == packet.full_suite_status == "passed"
    assert packet.observed_test_evidence.full_suite is True
    assert manifest.test_evidence_attached is True


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
@pytest.mark.parametrize("run_tests", [False, True])
@pytest.mark.parametrize("outputs", [("packet",), ("manifest",), ("packet", "manifest")])
def test_separate_artifact_outputs(tmp_path, command, run_tests, outputs, monkeypatch):
    import jsonschema
    from typer.testing import CliRunner
    from codeatlas.cli import app
    from codeatlas.review.packet import ReviewPacket
    import codeatlas.orchestrator.validation as validation_review

    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-EXPORT", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Export artifacts", expected_behavior="Existing validation behavior",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    packet_path = tmp_path / "artifacts" / "nested" / "packet.json"
    manifest_path = tmp_path / "artifacts" / "nested" / "manifest.json"
    report_path = tmp_path / "report.json"
    events_path = tmp_path / "events.jsonl"
    calls = []
    original = validation_review.build_validation_review

    def record_build(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(validation_review, "build_validation_review", record_build)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-api-key-must-never-be-exported")
    args = [
        "patch", command, "--proposal", str(proposal_path), "--repo", str(repo),
        "--approval-token", token, "--evidence-output", str(events_path),
    ]
    with_report = len(outputs) == 2
    if with_report:
        args.extend(["--report-output", str(report_path)])
    if run_tests:
        args.append("--run-tests")
    if "packet" in outputs:
        args.extend(["--packet-output", str(packet_path)])
    if "manifest" in outputs:
        args.extend(["--manifest-output", str(manifest_path)])
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    assert len(calls) == 1  # Both exports and the report use the original artifacts.
    packet, manifest = calls[0]
    expected = {"review_packet": packet.model_dump(mode="json"), "human_approval_manifest": manifest.model_dump(mode="json")}
    report = json.loads(report_path.read_text()) if with_report else None
    if report is not None:
        assert report["review_packet"] == expected["review_packet"]
        assert report["human_approval_manifest"] == expected["human_approval_manifest"]
        assert report["valid"] is True
    else:
        assert not report_path.exists()
    assert packet.tests_status == ("passed" if run_tests else "not_run")
    assert packet_path.exists() is ("packet" in outputs)
    assert manifest_path.exists() is ("manifest" in outputs)
    schema_root = Path(__file__).resolve().parents[2] / "schemas"
    for name, path, schema, key in (
        ("packet", packet_path, ReviewPacket.model_json_schema(), "review_packet"),
        ("manifest", manifest_path, json.loads((schema_root / "run-manifest.schema.json").read_text()), "human_approval_manifest"),
    ):
        if name not in outputs:
            continue
        content = path.read_text(encoding="utf-8")
        exported = json.loads(content)
        assert exported == expected[key]
        jsonschema.validate(exported, schema)
        assert token not in content
        assert "fixture-api-key-must-never-be-exported" not in content
        assert "stdout_summary" not in exported
        assert exported["network_isolation_verified"] is False
    if run_tests:
        observed = packet.observed_test_evidence
        assert observed.proposal_id == proposal.proposal_id
        assert observed.sandbox_id == manifest.sandbox_id
        assert observed.redaction_audit.safe is True
        jsonschema.validate(observed.model_dump(mode="json"), json.loads((schema_root / "observed-test-evidence.schema.json").read_text()))
    else:
        assert packet.observed_test_evidence is None
    assert (repo / "src" / "calc.py").read_text() == CALC_PY
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True) == ""
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == base_sha
    if command == "apply-isolated" or run_tests:
        assert manifest.cleanup_status == "completed"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert sum(e["event"].startswith("observed_test_evidence_") for e in events) == 1


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
@pytest.mark.parametrize("failure", ["invalid", "unwritable"])
def test_artifact_output_cli_failure_keeps_report(tmp_path, command, failure, monkeypatch):
    from typer.testing import CliRunner
    from codeatlas.cli import app
    import codeatlas.orchestrator.artifacts as exporter

    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-EXPORT-FAIL", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Output failure", expected_behavior="Validation report is preserved",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    output = tmp_path / "output"
    if failure == "invalid":
        output.mkdir()
    else:
        output.write_text("preserve")

        def deny(*args):
            raise PermissionError("private exception contents")

        monkeypatch.setattr(exporter.os, "replace", deny)
    report_path = tmp_path / "report.json"
    res = CliRunner().invoke(app, [
        "patch", command, "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--manifest-output", str(output), "--report-output", str(report_path),
    ])
    assert res.exit_code == 1
    assert "Error: --manifest-output:" in res.output
    assert "private exception contents" not in res.output
    assert token not in res.output
    assert json.loads(report_path.read_text())["valid"] is True
    assert not list(tmp_path.glob(".codeatlas-*.tmp"))
    if failure == "unwritable":
        assert output.read_text() == "preserve"


def test_redacted_failed_test_evidence_export(tmp_path):
    from typer.testing import CliRunner
    from codeatlas.cli import app

    secret = "ghp_" + "X" * 36
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo, f"def test_calc():\n    raise AssertionError('{secret}')\n")
    proposal = create_patch_proposal(
        finding_id="CA-REDACTED-EXPORT", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Export redacted diagnostics", expected_behavior="Failure remains visible",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    packet_path, manifest_path = tmp_path / "packet.json", tmp_path / "manifest.json"
    res = CliRunner().invoke(app, [
        "patch", "validate", "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--run-tests", "--packet-output", str(packet_path),
        "--manifest-output", str(manifest_path),
    ])
    assert res.exit_code == 1
    content = packet_path.read_text() + manifest_path.read_text()
    assert secret not in content and token not in content
    packet = json.loads(packet_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    assert packet["observed_test_evidence"]["status"] == "failed"
    assert packet["observed_test_evidence"]["redaction_audit"]["safe"] is True
    assert "[REDACTED]" in content
    assert manifest["patch_proposal_statuses"] == ["failed_validation"]


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
@pytest.mark.parametrize("mode", ["independent", "all_formats"])
def test_markdown_output_cli_modes(tmp_path, command, mode, monkeypatch):
    from typer.testing import CliRunner
    from codeatlas.cli import app
    from codeatlas.review.rendering import render_test_evidence
    import codeatlas.orchestrator.validation as validation_review

    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-MD-MATRIX", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Matrix test for markdown output", expected_behavior="Markdown export works cleanly",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    events_path = tmp_path / "events.jsonl"
    md_path = tmp_path / "artifacts" / "nested" / "summary.md"
    report_path = tmp_path / "report.json"
    packet_path = tmp_path / "artifacts" / "packet.json"
    manifest_path = tmp_path / "artifacts" / "manifest.json"

    calls = []
    original = validation_review.build_validation_review

    def record_build(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(validation_review, "build_validation_review", record_build)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-api-key-must-never-be-exported")

    args = [
        "patch", command, "--proposal", str(proposal_path), "--repo", str(repo),
        "--approval-token", token, "--run-tests", "--markdown-output", str(md_path),
        "--evidence-output", str(events_path),
    ]
    if mode == "all_formats":
        args.extend([
            "--report-output", str(report_path),
            "--packet-output", str(packet_path),
            "--manifest-output", str(manifest_path),
        ])

    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    assert md_path.exists()
    packet, manifest = calls[0]
    expected_md = render_test_evidence(packet)
    exported_md = md_path.read_text(encoding="utf-8")
    assert exported_md == expected_md
    assert token not in exported_md
    assert "fixture-api-key-must-never-be-exported" not in exported_md
    assert "network_isolation_verified=false" in exported_md

    if mode == "independent":
        assert not report_path.exists()
        assert not packet_path.exists()
        assert not manifest_path.exists()
    else:
        assert report_path.exists()
        assert packet_path.exists()
        assert manifest_path.exists()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert "markdown_summary" not in report
        assert report["valid"] is True


def test_markdown_output_parent_directory_creation_cli(tmp_path):
    from typer.testing import CliRunner
    from codeatlas.cli import app

    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-MD-PARENT", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Parent dir test", expected_behavior="Directory created",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    deep_path = tmp_path / "deep" / "nested" / "dir" / "summary.md"
    assert not deep_path.parent.exists()

    res = CliRunner().invoke(app, [
        "patch", "validate", "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--markdown-output", str(deep_path),
    ])
    assert res.exit_code == 0, res.output
    assert deep_path.parent.exists()
    assert deep_path.is_file()
    assert "Test status: targeted=not_run" in deep_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
@pytest.mark.parametrize("failure", ["invalid", "unwritable"])
def test_markdown_output_cli_failure_keeps_report(tmp_path, command, failure, monkeypatch):
    from typer.testing import CliRunner
    from codeatlas.cli import app
    import codeatlas.orchestrator.artifacts as exporter

    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-MD-FAIL", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Markdown failure", expected_behavior="Report is preserved",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    output = tmp_path / "output"
    if failure == "invalid":
        output.mkdir()
    else:
        output.write_text("preserve")

        def deny(*args):
            raise PermissionError("private exception contents")

        monkeypatch.setattr(exporter.os, "replace", deny)
    report_path = tmp_path / "report.json"
    res = CliRunner().invoke(app, [
        "patch", command, "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--markdown-output", str(output), "--report-output", str(report_path),
    ])
    assert res.exit_code == 1
    assert "Error: --markdown-output:" in res.output
    assert "private exception contents" not in res.output
    assert token not in res.output
    assert json.loads(report_path.read_text())["valid"] is True
    assert not list(tmp_path.glob(".codeatlas-*.tmp"))
    if failure == "unwritable":
        assert output.read_text() == "preserve"


def test_markdown_output_failed_test_and_redaction(tmp_path):
    from typer.testing import CliRunner
    from codeatlas.cli import app

    secret = "ghp_" + "X" * 36
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo, f"def test_calc():\n    raise AssertionError('{secret}')\n")
    proposal = create_patch_proposal(
        finding_id="CA-MD-FAILED-TEST", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Export failed test markdown", expected_behavior="Redacted failure summary",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    md_path = tmp_path / "summary.md"
    res = CliRunner().invoke(app, [
        "patch", "validate", "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--run-tests", "--markdown-output", str(md_path),
    ])
    assert res.exit_code == 1
    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")
    assert secret not in content
    assert token not in content
    assert "[REDACTED]" in content
    assert "Test status: targeted=failed" in content
    assert "Failure:" in content
    assert "Failed tests:" in content and "test_calc" in content
    assert "Redaction: passed" in content
    assert "network_isolation_verified=false" in content


def test_markdown_output_no_test_flow(tmp_path):
    from typer.testing import CliRunner
    from codeatlas.cli import app

    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-MD-NO-TEST", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="No-test validation flow", expected_behavior="Clean no-test markdown",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)
    md_path = tmp_path / "summary.md"

    res = CliRunner().invoke(app, [
        "patch", "validate", "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--markdown-output", str(md_path),
    ])
    assert res.exit_code == 0, res.output
    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")
    assert "Test status: targeted=not_run; full_suite=not_run" in content
    assert "Test evidence attached: false" in content
    assert "Evidence exclusion: not_run" in content
    assert "Redaction: no attached output" in content
    assert "network_isolation_verified=false" in content


def test_markdown_output_cli_output_regression(tmp_path):
    from typer.testing import CliRunner
    from codeatlas.cli import app

    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-MD-REGRESSION", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Regression verification", expected_behavior="Terminal and report outputs identical",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json())
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)

    report_without = tmp_path / "report_without.json"
    res_without = CliRunner().invoke(app, [
        "patch", "validate", "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--report-output", str(report_without),
    ])
    assert res_without.exit_code == 0, res_without.output

    report_with = tmp_path / "report_with.json"
    md_path = tmp_path / "summary.md"
    res_with = CliRunner().invoke(app, [
        "patch", "validate", "--repo", str(repo), "--proposal", str(proposal_path),
        "--approval-token", token, "--report-output", str(report_with),
        "--markdown-output", str(md_path),
    ])
    assert res_with.exit_code == 0, res_with.output

    def normalize(text: str) -> list[str]:
        return [line for line in text.splitlines() if not line.startswith("Report output:")]

    assert normalize(res_without.output) == normalize(res_with.output)
    data_without = json.loads(report_without.read_text(encoding="utf-8"))
    data_with = json.loads(report_with.read_text(encoding="utf-8"))
    data_without.pop("duration_ms", None)
    data_with.pop("duration_ms", None)
    assert data_without == data_with
