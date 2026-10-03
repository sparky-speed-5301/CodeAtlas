"""Phase 8C synthetic evidence-contract evaluation; no execution capabilities.

Actual sandbox/CLI execution is covered by the Phase 8A/8B regression suites
and integration tests. These cases exercise diagnostic parsing and review gates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import jsonschema

from codeatlas.orchestrator.validation import build_validation_review
from codeatlas.patching.models import PatchValidationResult
from codeatlas.patching.proposal import create_patch_proposal
from codeatlas.review.packet import MAX_EVIDENCE_BYTES, OBSERVED_TEST_DISCLAIMER, ReviewPacket
from codeatlas.review.rendering import render_test_evidence
from codeatlas.verification.diagnostics import parse_test_diagnostics
from codeatlas.verification.models import TestPlan, TestResult
from codeatlas.verification.redaction import audit_and_redact_results


def evaluate_case(case: dict) -> dict:
    proposal = create_patch_proposal(
        finding_id="CA-EVAL-8C", provider_name="fixture", provider_version="1",
        base_commit="a" * 40, target_files=["calc.py"],
        unified_diff="--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        rationale="Evaluation fixture", expected_behavior="Review scope only",
    )
    proposal.status = "requires_human_approval"
    full_suite = case.get("full_suite", False)
    plan = TestPlan(
        language="python", full_suite=full_suite,
        exact_command=["python", "-m", "pytest", "tests" if full_suite else "tests/test_calc.py"],
        test_targets=["tests" if full_suite else "tests/test_calc.py"],
    )
    status = case["status"]
    output = "2 passed, 1 skipped in 0.01s" if status == "passed" else (
        "FAILED tests/test_calc.py::test_calc - AssertionError: mismatch\n1 failed, 1 passed in 0.01s"
    )
    stdout, stderr, _, audit = audit_and_redact_results(output, "", [])
    diagnostics = parse_test_diagnostics(
        runner="pytest", exit_code=0 if status == "passed" else 1,
        stdout=stdout, stderr=stderr, output_truncated=case.get("truncated", False),
    )
    audit.safe = case.get("redaction_safe", True)
    result = TestResult(
        proposal_id="wrong-proposal" if case.get("wrong_identity") else proposal.proposal_id,
        sandbox_id="sandbox-eval-8c", runner=case.get("runner", "pytest"), language="python",
        status=status, command=plan.exact_command, tests_run=plan.test_targets,
        execution_allowed=status not in {"blocked", "not_run"},
        execution_started=status not in {"blocked", "not_run"},
        duration_ms=10, full_suite=full_suite, output_truncated=case.get("truncated", False),
        redaction_audit=audit.model_dump(), **diagnostics,
    )
    validation = PatchValidationResult(
        proposal_id=proposal.proposal_id, base_commit=proposal.base_commit, patch_hash=proposal.patch_hash,
        sandbox_id="sandbox-eval-8c", approval_scope="default", approval_verified=True,
        patch_applied_in_isolated_sandbox=True, applies_cleanly=True, syntax_valid=True,
        test_execution_attempted=True, execution_allowed=result.execution_allowed,
        tests_status="not_run" if full_suite else status,
        full_suite_status=status if full_suite else "not_run",
        full_suite_requested=full_suite, full_suite_policy_opted_in=full_suite,
        test_plan=plan.model_dump(), test_result=None if full_suite else result,
        full_suite_test_plan=plan if full_suite else None,
        full_suite_result=result.model_dump() if full_suite else None,
    )
    packet, manifest = build_validation_review(proposal, validation, repository="eval-8c")
    observed = packet.observed_test_evidence
    checks = {
        "attachment": (observed is not None) == case["attached"],
        "exclusion_reason": packet.test_evidence_exclusion_reason == case.get("reason"),
        "human_approval_required": packet.policy_summary["decision"] == "requires_human_approval",
        "no_approval_upgrade": proposal.status in {"requires_human_approval", "failed_validation"},
        "failed_validation_preserved": not (observed and status == "failed") or proposal.status == "failed_validation",
        "network_unverified": packet.network_isolation_verified is False and manifest.network_isolation_verified is False,
        "disclaimer": OBSERVED_TEST_DISCLAIMER in render_test_evidence(packet) and OBSERVED_TEST_DISCLAIMER in manifest.validation_limitations,
        "manifest_attachment": manifest.test_evidence_attached == case["attached"],
        "bounded": observed is None or len(observed.model_dump_json().encode()) <= MAX_EVIDENCE_BYTES,
        "identity": observed is None or (observed.proposal_id == proposal.proposal_id and observed.sandbox_id == validation.sandbox_id),
        "scope": observed is None or observed.full_suite == full_suite,
        "truncation_visible": not case.get("truncated") or bool(observed and observed.output_truncated),
    }
    root = Path(__file__).resolve().parents[1] / "schemas"
    try:
        for path in root.glob("*.schema.json"):
            jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
        jsonschema.validate(manifest.model_dump(mode="json"), json.loads((root / "run-manifest.schema.json").read_text()))
        jsonschema.validate(packet.model_dump(mode="json"), ReviewPacket.model_json_schema())
        if observed:
            jsonschema.validate(observed.model_dump(mode="json"), json.loads((root / "observed-test-evidence.schema.json").read_text()))
        checks["json_schema"] = True
    except jsonschema.ValidationError:
        checks["json_schema"] = False
    return {"case_id": case["case_id"], "checks": checks, "pass": all(checks.values())}


def run_observed_evidence_eval(cases: Path | None = None) -> list[dict]:
    cases = cases or Path(__file__).parent / "cases" / "observed-evidence" / "cases.json"
    return [evaluate_case(case) for case in json.loads(cases.read_text(encoding="utf-8"))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = run_observed_evidence_eval(args.cases)
    for result in results:
        print(json.dumps(result, sort_keys=True))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in results), encoding="utf-8")
    print(f"Phase 8C: {sum(r['pass'] for r in results)}/{len(results)} cases passed")
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
