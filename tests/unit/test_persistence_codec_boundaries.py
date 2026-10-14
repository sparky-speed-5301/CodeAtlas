"""Focused WP5 persistence codec boundary regressions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from codeatlas.patching.proposal import compute_patch_hash, create_patch_proposal
from codeatlas.service.models import ReviewCreateRequest
from codeatlas.service.persistence import PersistenceError, repository_id
from codeatlas.service.persistence_codec import (
    PATCH_FIELDS,
    make_snapshot,
    validate_snapshot,
    validation_projection,
)
from codeatlas.service.state import ReviewRunRecord, ReviewStateManager

FINDING_ID = "CA-BOUNDARY"
RUN_ID = "service-run"
BASE_COMMIT = "a" * 40
HEAD_COMMIT = "b" * 40
DIFF = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"


def _finding() -> dict[str, object]:
    return {
        "id": FINDING_ID,
        "severity": "high",
        "category": "STYLE",
        "claim": "bounded finding",
        "file": "src/app.py",
        "line": 1,
        "start_line": 1,
        "end_line": 1,
        "confidence": 0.9,
        "evidence_strength": "strong",
        "status": "review_only",
        "quality_decision": "review_only",
    }


def _snapshot(repo: Path) -> dict[str, object]:
    status = {
        "run_id": RUN_ID,
        "status": "completed",
        "progress_text": "Review completed",
        "repository": str(repo),
        "base": "base",
        "head": "head",
        "base_commit": BASE_COMMIT,
        "head_commit": HEAD_COMMIT,
        "policy_decision": "allowed",
        "finding_counts": {"total": 1, "high": 1},
        "test_status": "not_run",
        "full_suite_status": "not_run",
        "patch_validation_status": "not_run",
        "truncated": False,
        "limitations": [],
        "errors": [],
        "created_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:01:00+00:00",
        "recovery_state": "not_required",
        "retry_of": None,
    }
    request = ReviewCreateRequest(repo=str(repo), base="base", head="head").model_dump(mode="json")
    return {
        "repository_id": repository_id(repo),
        "repository_path": str(repo.resolve()),
        "run_id": RUN_ID,
        "review": {"status": status, "request": request, "latest": {}, "operation": None},
        "entities": [{
            "kind": "findings", "id": FINDING_ID, "finding_id": FINDING_ID,
            "proposal_id": "", "patch_hash": "", "payload": _finding(),
        }, {
            "kind": "finding_feedback", "id": FINDING_ID, "finding_id": FINDING_ID,
            "proposal_id": "", "patch_hash": "", "payload": {"label": None, "dismissed": False},
        }],
        "artifacts": {},
    }


def _with_patch_and_validation(snapshot: dict[str, object], *, approval_scope: str | None = RUN_ID) -> dict[str, object]:
    result = deepcopy(snapshot)
    proposal = create_patch_proposal(
        proposal_id="prop-boundary", finding_id=FINDING_ID, provider_name="test-provider",
        provider_version="1", base_commit=HEAD_COMMIT, target_files=["src/app.py"],
        unified_diff=DIFF, rationale="bounded rationale", expected_behavior="bounded behavior",
    )
    patch_hash = proposal.patch_hash
    result["review"]["latest"] = {FINDING_ID: proposal.proposal_id}  # type: ignore[index]
    patch_payload = {
        key: value for key, value in proposal.model_dump(mode="json").items()
        if key in PATCH_FIELDS
    }
    result["entities"].extend([  # type: ignore[union-attr]
        {
            "kind": "patch_proposals", "id": proposal.proposal_id, "finding_id": FINDING_ID,
            "proposal_id": proposal.proposal_id, "patch_hash": patch_hash,
            "payload": patch_payload,
        },
        {
            "kind": "validation_evidence", "id": proposal.proposal_id, "finding_id": FINDING_ID,
            "proposal_id": proposal.proposal_id, "patch_hash": patch_hash,
            "payload": {
                "proposal_id": proposal.proposal_id, "base_commit": HEAD_COMMIT,
                "patch_hash": patch_hash, "valid": True, "applies_cleanly": True,
                "syntax_valid": True, "approval_scope": approval_scope,
                "errors": [], "warnings": [],
            },
        },
    ])
    return result


def _reject(snapshot: dict[str, object]) -> None:
    with pytest.raises(PersistenceError, match="persisted_identity_invalid"):
        validate_snapshot(snapshot)


@pytest.mark.parametrize("field", ["base_commit", "head_commit"])
def test_snapshot_requires_resolved_revision_identity(tmp_path: Path, field: str) -> None:
    snapshot = _with_patch_and_validation(_snapshot(tmp_path))
    snapshot["review"]["status"][field] = None  # type: ignore[index]
    _reject(snapshot)


def test_allowlisted_fields_reject_nested_feedback_and_metadata(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    nested_feedback = deepcopy(snapshot)
    nested_feedback["entities"][1]["payload"]["label"] = {"diagnostic": "raw"}  # type: ignore[index]
    _reject(nested_feedback)

    nested_provider = deepcopy(snapshot)
    nested_provider["entities"].append({  # type: ignore[union-attr]
        "kind": "provider_metadata", "id": "review", "finding_id": "", "proposal_id": "", "patch_hash": "",
        "payload": {"provider_name": {"name": "provider"}},
    })
    _reject(nested_provider)


def test_validation_diagnostics_are_fixed_codes_only() -> None:
    projected = validation_projection({
        "errors": ["operation_interrupted", "PRIVATE_DIAGNOSTIC"],
        "warnings": ["private warning"],
    })
    assert projected["errors"] == ["operation_interrupted"]
    assert projected["warnings"] == ["validation_limitations_omitted"]


def test_validation_approval_scope_is_bound_to_service_run(tmp_path: Path) -> None:
    valid = _with_patch_and_validation(_snapshot(tmp_path))
    validate_snapshot(valid)

    wrong_scope = _with_patch_and_validation(_snapshot(tmp_path), approval_scope="orchestrator-run")
    _reject(wrong_scope)


def test_proposal_hash_and_identity_are_exact(tmp_path: Path) -> None:
    snapshot = _with_patch_and_validation(_snapshot(tmp_path))
    validate_snapshot(snapshot)

    tampered = deepcopy(snapshot)
    tampered["entities"][2]["payload"]["patch_hash"] = "c" * 64  # type: ignore[index]
    _reject(tampered)

    tampered_header = deepcopy(snapshot)
    tampered_header["entities"][2]["patch_hash"] = compute_patch_hash(DIFF + "# changed\n")  # type: ignore[index]
    _reject(tampered_header)


def test_manifest_uses_its_orchestrator_identity_and_repository_scope(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["artifacts"] = {  # type: ignore[assignment]
        "manifest": {
            "run_id": "orchestrator-run",
            "repository": str(tmp_path.resolve()),
            "base_commit": BASE_COMMIT,
            "head_commit": HEAD_COMMIT,
            "base_ref": "base",
            "head_ref": "head",
        },
    }
    validate_snapshot(snapshot)

    tampered = deepcopy(snapshot)
    tampered["artifacts"]["manifest"]["repository"] = str((tmp_path / "other").resolve())  # type: ignore[index]
    _reject(tampered)


def test_make_snapshot_preserves_operation_interrupted(tmp_path: Path) -> None:
    manager = ReviewStateManager()
    record = ReviewRunRecord(RUN_ID, ReviewCreateRequest(repo=str(tmp_path)), tmp_path)
    record.status = "failed"
    record.errors = ["raw provider diagnostic"]
    record.safe_error_code = "operation_interrupted"
    manager._reviews[RUN_ID] = record

    snapshot = make_snapshot(manager, record)
    assert snapshot["review"]["status"]["errors"] == ["operation_interrupted"]  # type: ignore[index]
    assert snapshot["entities"][-1]["payload"] == {"code": "operation_interrupted"}  # type: ignore[index]
    manager.close()


def test_make_snapshot_excludes_prompt_transcript_token_environment_and_source(tmp_path: Path) -> None:
    manager = ReviewStateManager()
    record = ReviewRunRecord(RUN_ID, ReviewCreateRequest(repo=str(tmp_path)), tmp_path)
    finding = _finding()
    finding.update({
        "prompt": "PRIVATE_PROMPT",
        "transcript": "PRIVATE_TRANSCRIPT",
        "token": "PRIVATE_TOKEN",
        "env": {"PRIVATE_ENV": "secret"},
        "source": "PRIVATE_SOURCE",
    })
    record.findings = [finding]
    manager._reviews[RUN_ID] = record

    snapshot = make_snapshot(manager, record)
    finding_payload = snapshot["entities"][0]["payload"]  # type: ignore[index]
    assert not {"prompt", "transcript", "token", "env", "source"} & finding_payload.keys()
    manager.close()
