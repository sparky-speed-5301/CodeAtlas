"""Integration tests for Phase 7B: opt-in provider patch suggestions in the pipeline."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.orchestrator.review import run_review
from codeatlas.providers import FakeTransport

cli_runner = CliRunner()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return result.stdout.strip()


def _repo_with_change(root: Path) -> tuple[str, str]:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def compute(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        total += i\n"
        "    return total\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "before")
    base = _git(root, "rev-parse", "HEAD")
    time.sleep(0.05)
    (root / "src" / "app.py").write_text(
        "def compute(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        total += i\n"
        "        if total > 100:\n"
        "            total = total // 0\n"
        "    return total\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "after")
    return base, _git(root, "rev-parse", "HEAD")


DIFF = "\n".join([
    "--- a/src/app.py",
    "+++ b/src/app.py",
    "@@ -5,3 +5,3 @@",
    "         if total > 100:",
    "-            total = total // 0",
    "+            total = total // 2",
    "     return total",
]) + "\n"

FINDING = {
    "id": "CA-REV-IT-1",
    "file": "src/app.py",
    "start_line": 5,
    "end_line": 6,
    "severity": "medium",
    "category": "LOGIC_BUG",
    "claim": "Division by zero when total exceeds the threshold",
    "impact": "compute raises ZeroDivisionError at runtime",
    "evidence": ["changed lines add 'total = total // 0'"],
    "evidence_strength": "supported",
    "confidence": 0.92,
    "limitations": [],
    "status": "detected",
    "fixability": "review_required",
    "provenance": {"origin": "reviewer"},
}

SUGGESTION = {
    "suggestion_id": "CA-SUG-IT-1",
    "finding_id": "CA-REV-IT-1",
    "unified_diff": DIFF,
    "rationale": "Replace division by zero with a safe divisor",
    "expected_behavior": "compute no longer raises ZeroDivisionError",
    "target_files": ["src/app.py"],
    "risk_level": "low",
    "limitations": [],
    "provider_provenance": {"origin": "provider"},
}

RESPONSE = {
    "summary": "One defect with a draft fix.",
    "findings": [FINDING],
    "limitations": [],
    "abstentions": [],
    "patch_suggestions": [SUGGESTION],
}


def _run(work: Path, base: str, head: str, **kwargs):
    defaults = dict(
        assemble_review_packet=True,
        review_provider="live",
        provider_model="test-model",
        provider_transport=FakeTransport(simulated_response=RESPONSE, latency_ms=4.0),
        allow_patch_suggestions=True,
    )
    defaults.update(kwargs)
    return run_review(work, base=base, head=head, **defaults)


def test_suggestion_materializes_to_proposal(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    manifest = _run(tmp_path / "work", base, head).manifest

    assert manifest.patch_suggestions_received == 1
    assert manifest.patch_suggestions_accepted == 1
    assert manifest.patch_suggestions_rejected == 0
    assert len(manifest.patch_proposals) == 1
    proposal = manifest.patch_proposals[0]
    assert proposal["status"] == "requires_human_approval"
    assert proposal["finding_id"] == "CA-REV-IT-1"
    assert proposal["base_commit"] == head
    assert proposal["provenance"]["run_id"] == manifest.run_id
    assert manifest.patch_proposal_ids == [proposal["proposal_id"]]
    assert manifest.patch_proposal_statuses == ["requires_human_approval"]
    assert manifest.patch_policy_decisions[0]["decision"] == "requires_human_approval"
    assert manifest.patch_validation["proposals"][0]["execution_allowed"] is False


def test_evidence_lifecycle_for_suggestions(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    evidence_path = tmp_path / "evidence.jsonl"
    _run(tmp_path / "work", base, head, evidence_output=evidence_path)

    events = [json.loads(line)["event"] for line in evidence_path.read_text(encoding="utf-8").splitlines() if line]
    for expected_event in (
        "patch_suggestion_received",
        "patch_proposal_created",
        "patch_proposal_policy_evaluated",
        "patch_proposal_approval_required",
        "patch_auto_apply_blocked",
        "live_review_completed",
    ):
        assert expected_event in events


def test_proposal_id_deterministic_across_runs(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    ids = set()
    for _ in range(2):
        manifest = _run(tmp_path / "work", base, head).manifest
        ids.update(manifest.patch_proposal_ids)
    # Same repo state, same finding, same diff -> same deterministic proposal ID.
    assert len(ids) == 1


def test_no_automatic_application_and_worktree_unchanged(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    work = tmp_path / "work"
    files_before = {p: p.read_bytes() for p in work.rglob("*.py")}

    manifest = _run(work, base, head).manifest

    assert manifest.automatic_application_attempted is False
    assert manifest.automatic_application_blocked is True
    assert all(t.startswith("git ") for t in manifest.tools_run)
    assert manifest.tests_run == []
    assert _git(work, "rev-parse", "HEAD") == head
    assert _git(work, "status", "--porcelain") == ""
    assert {p: p.read_bytes() for p in work.rglob("*.py")} == files_before
    # The proposed fix was NOT applied to the repository.
    assert "total // 2" not in (work / "src" / "app.py").read_text(encoding="utf-8")


def test_suggestions_opt_in_by_default(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    manifest = _run(tmp_path / "work", base, head, allow_patch_suggestions=False).manifest

    assert manifest.patch_suggestions_received is None
    assert manifest.patch_proposals == []
    assert any("patch suggestions are disabled" in e for e in manifest.provider_validation_errors)


def test_invalid_suggestion_preserves_valid_review(tmp_path: Path):
    response = json.loads(json.dumps(RESPONSE))
    response["patch_suggestions"].append({
        "suggestion_id": "CA-SUG-BAD",
        "finding_id": "CA-REV-NONE",
        "unified_diff": DIFF,
        "rationale": "bad link",
        "expected_behavior": "n/a",
        "target_files": ["src/app.py"],
        "risk_level": "low",
        "limitations": [],
    })
    base, head = _repo_with_change(tmp_path / "work")
    manifest = _run(
        tmp_path / "work", base, head,
        provider_transport=FakeTransport(simulated_response=response),
    ).manifest

    assert manifest.patch_suggestions_accepted == 1
    assert manifest.patch_suggestions_rejected == 1
    assert len(manifest.patch_proposals) == 1
    # The review finding itself was preserved and merged.
    assert any(f["id"] == "CA-REV-IT-1" for f in manifest.merged_findings)
    assert any("no source finding" in r for r in manifest.patch_rejection_reasons)


def test_cli_patch_list_shows_proposals_without_diffs(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    manifest_path = tmp_path / "manifest.json"
    _run(tmp_path / "work", base, head, manifest_output=manifest_path)

    result = cli_runner.invoke(app, ["patch", "list", "--manifest", str(manifest_path)])
    assert result.exit_code == 0
    assert "Patch proposals: 1" in result.output
    assert "requires_human_approval" in result.output
    assert "src/app.py" in result.output
    # Diff content is never printed by the list command.
    assert "total // 2" not in result.output
    assert "--- a/src/app.py" not in result.output


def test_cli_dry_run_banner_mentions_draft_only(tmp_path: Path):
    base, head = _repo_with_change(tmp_path / "work")
    result = cli_runner.invoke(app, [
        "review",
        "--repo", str(tmp_path / "work"),
        "--base", base,
        "--head", head,
        "--index-repository",
        "--assemble-review-packet",
        "--review-provider", "live",
        "--dry-run",
        "--allow-patch-suggestions",
    ])
    assert result.exit_code == 0, result.output
    assert "Patches: draft-suggestions-only (never applied)" in result.output
