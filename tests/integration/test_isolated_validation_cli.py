"""Integration tests for Phase 7C: CLI isolated-validation with scoped approval tokens."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.patching.proposal import create_patch_proposal, generate_approval_token

cli_runner = CliRunner()

APP_CONTENT = "def run():\n    return 42\n"
DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def run():\n"
    "+    # guarded return\n"
    "     return 42\n"
)


def _init_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True)
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "app.py").write_text(APP_CONTENT, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t.invalid")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "init")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()


def _proposal_file(tmp_path: Path, commit: str) -> tuple[Path, object]:
    prop = create_patch_proposal(
        finding_id="F-CLI-7C",
        provider_name="operator",
        provider_version="1.0.0",
        base_commit=commit,
        target_files=["src/app.py"],
        unified_diff=DIFF,
        rationale="CLI integration test",
        expected_behavior="Applies cleanly",
    )
    path = tmp_path / "proposal.json"
    path.write_text(prop.model_dump_json(indent=2), encoding="utf-8")
    return path, prop


def test_cli_apply_isolated_success(tmp_path: Path):
    commit = _init_repo(tmp_path / "repo")
    prop_path, prop = _proposal_file(tmp_path, commit)
    token = generate_approval_token(prop.proposal_id, commit, prop.patch_hash, prop.target_files)
    report_path = tmp_path / "report.json"
    evidence_path = tmp_path / "evidence.jsonl"

    result = cli_runner.invoke(app, [
        "patch", "apply-isolated",
        "--proposal", str(prop_path),
        "--repo", str(tmp_path / "repo"),
        "--base", commit,
        "--approval-token", token,
        "--report-output", str(report_path),
        "--evidence-output", str(evidence_path),
    ])
    assert result.exit_code == 0, result.output
    assert "Approval Verified:  True" in result.output
    assert "Applies Cleanly:    True" in result.output
    assert "Syntax Valid:       True" in result.output
    assert "Tests Status:       not_run" in result.output
    assert "Build Status:       not_run" in result.output
    assert "Execution Allowed:  False" in result.output
    assert "Cleanup Status:     completed" in result.output

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["approval_verified"] is True
    assert report["applies_cleanly"] is True
    assert report["syntax_valid"] is True
    assert report["tests_status"] == "not_run"
    assert report["build_status"] == "not_run"
    assert report["execution_allowed"] is False
    assert report["cleanup_status"] == "completed"
    assert report["proposal_status"] == "validated"
    assert report["resulting_diff_hash"]
    assert report["base_commit"] == commit
    assert report["proposal_id"] == prop.proposal_id

    events = [json.loads(line)["event"] for line in evidence_path.read_text(encoding="utf-8").splitlines() if line]
    assert "patch_validation_requested" in events
    assert "approval_verified" in events
    assert "sandbox_created" in events
    assert "patch_applied_isolated" in events
    assert "sandbox_cleanup_completed" in events
    assert "patch_validation_completed" in events

    # The token never reaches the report or the evidence log.
    assert token not in report_path.read_text(encoding="utf-8")
    assert token not in evidence_path.read_text(encoding="utf-8")
    # The original repository was never modified.
    assert "# guarded return" not in (tmp_path / "repo" / "src" / "app.py").read_text(encoding="utf-8")


def test_cli_apply_isolated_invalid_token_fails_closed(tmp_path: Path):
    commit = _init_repo(tmp_path / "repo")
    prop_path, _ = _proposal_file(tmp_path, commit)
    report_path = tmp_path / "report.json"

    result = cli_runner.invoke(app, [
        "patch", "apply-isolated",
        "--proposal", str(prop_path),
        "--repo", str(tmp_path / "repo"),
        "--base", commit,
        "--approval-token", "CAT-APP-invalidtoken0000000000000000",
        "--report-output", str(report_path),
    ])
    assert result.exit_code != 0
    assert "does not match expected token" in result.output
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["approval_verified"] is False
    assert report["applies_cleanly"] is False
    # No patch application occurred.
    assert "# guarded return" not in (tmp_path / "repo" / "src" / "app.py").read_text(encoding="utf-8")


def test_cli_apply_isolated_missing_token_fails_cleanly(tmp_path: Path):
    commit = _init_repo(tmp_path / "repo")
    prop_path, _ = _proposal_file(tmp_path, commit)

    result = cli_runner.invoke(app, [
        "patch", "apply-isolated",
        "--proposal", str(prop_path),
        "--repo", str(tmp_path / "repo"),
        "--base", commit,
    ])
    assert result.exit_code == 1
    assert "requires human approval token" in result.output


def test_cli_apply_isolated_missing_proposal_file(tmp_path: Path):
    commit = _init_repo(tmp_path / "repo")
    result = cli_runner.invoke(app, [
        "patch", "apply-isolated",
        "--proposal", str(tmp_path / "nonexistent.json"),
        "--repo", str(tmp_path / "repo"),
        "--base", commit,
        "--approval-token", "CAT-APP-anything",
    ])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_cli_apply_isolated_malformed_proposal_file(tmp_path: Path):
    commit = _init_repo(tmp_path / "repo")
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    result = cli_runner.invoke(app, [
        "patch", "apply-isolated",
        "--proposal", str(bad),
        "--repo", str(tmp_path / "repo"),
        "--base", commit,
        "--approval-token", "CAT-APP-anything",
    ])
    assert result.exit_code == 1
    assert "parsing proposal JSON" in result.output


def test_review_command_never_attempts_isolated_validation(tmp_path: Path):
    """The review command's manifest always records that isolated validation is forbidden there."""
    commit = _init_repo(tmp_path / "repo")
    manifest_path = tmp_path / "manifest.json"
    result = cli_runner.invoke(app, [
        "review",
        "--repo", str(tmp_path / "repo"),
        "--base", commit,
        "--head", commit,
        "--manifest-output", str(manifest_path),
    ])
    assert result.exit_code == 0, result.output
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["isolated_validation_attempted"] is False
    assert manifest["isolated_validation_status"] == "not_allowed_in_review_command"
    assert manifest["execution_allowed"] is False


def test_patch_inspect_does_not_print_diff(tmp_path: Path):
    commit = _init_repo(tmp_path / "repo")
    prop_path, prop = _proposal_file(tmp_path, commit)
    result = cli_runner.invoke(app, ["patch", "inspect", "--proposal", str(prop_path)])
    assert result.exit_code == 0
    assert prop.proposal_id in result.output
    assert "@@" not in result.output
    assert "guarded return" not in result.output
