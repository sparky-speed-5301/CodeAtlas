"""Live provider contract, adversarial-safety, and transport evaluation runner (Phase 7A).

Executes the review pipeline against deterministic fixtures using an in-memory
FakeTransport.  No live API calls are made and no credentials are required.

Metric groups are reported separately and explicitly labelled:
  - transport tests        : bounded failure handling of the transport layer
  - contract tests         : provider output contract enforcement
  - adversarial safety tests: injection, leakage, and policy-bypass resistance
No model precision or recall is claimed from these synthetic cases; the fake
transport verifies pipeline behaviour, not model quality.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from codeatlas.orchestrator.review import run_review
from codeatlas.providers import (
    FakeTransport,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseSizeError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from codeatlas.review.packet import SECRET_PATTERNS

ERROR_MAP = {
    "timeout": ProviderTimeoutError("simulated provider timeout"),
    "rate_limit": ProviderRateLimitError("simulated provider rate limit"),
    "auth": ProviderAuthError("simulated provider authentication failure"),
    "connection": ProviderTransportError("simulated connection failure"),
    "response_size": ProviderResponseSizeError("simulated oversized provider response"),
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _write_tree(case_dir: Path, sub: str, files: dict[str, str], work: Path) -> None:
    source = case_dir / sub
    if source.is_dir() and any(source.rglob("*")):
        shutil.copytree(source, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def evaluate_live_review_case(case_dir: Path) -> dict[str, Any]:
    """Evaluate one live-provider fixture end to end with a fake transport."""
    metadata = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
    expected = metadata.get("expected", {})
    transport_cfg = metadata.get("transport", {})

    if (case_dir / "response.json").is_file():
        simulated_response: str | dict[str, Any] | None = json.loads(
            (case_dir / "response.json").read_text(encoding="utf-8")
        )
    elif (case_dir / "response.txt").is_file():
        simulated_response = (case_dir / "response.txt").read_text(encoding="utf-8")
    else:
        simulated_response = None

    simulated_error = ERROR_MAP.get(transport_cfg.get("error", ""))

    with tempfile.TemporaryDirectory(prefix="codeatlas-live-eval-") as temp:
        work = Path(temp) / "work"
        work.mkdir()

        _git(work, "init", "-q")
        _git(work, "config", "user.email", "eval@example.invalid")
        _git(work, "config", "user.name", "eval")

        _write_tree(case_dir, "before", {}, work)
        _git(work, "add", "-A")
        _git(work, "commit", "--allow-empty", "-qm", "before")
        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()

        for item in list(work.iterdir()):
            if item.name != ".git":
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        time.sleep(0.05)
        _write_tree(case_dir, "after", {}, work)
        now = time.time() + 1.0
        for p in work.rglob("*"):
            if p.is_file():
                try:
                    os.utime(p, (now, now))
                except OSError:
                    pass

        case_cfg = metadata.get("config")
        if case_cfg:
            import yaml

            (work / ".codeatlas.yml").write_text(yaml.safe_dump(case_cfg), encoding="utf-8")

        _git(work, "add", "-A")
        _git(work, "commit", "--allow-empty", "-qm", "after")
        head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()

        transport = FakeTransport(
            simulated_response=simulated_response,
            simulated_error=simulated_error,
            latency_ms=transport_cfg.get("latency_ms", 5.0),
            input_tokens=transport_cfg.get("input_tokens", 150),
            output_tokens=transport_cfg.get("output_tokens", 80),
        )

        artifacts = Path(temp) / "artifacts"
        artifacts.mkdir()
        manifest_path = artifacts / "manifest.json"
        evidence_path = artifacts / "evidence.jsonl"
        packet_path = artifacts / "packet.json"

        started = time.perf_counter()
        review_result = run_review(
            work,
            base=base_sha,
            head=head_sha,
            assemble_review_packet=True,
            review_provider="live",
            provider_dry_run=bool(metadata.get("dry_run", False)),
            provider_transport=transport,
            manifest_output=manifest_path,
            evidence_output=evidence_path,
            packet_output=packet_path,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        manifest = review_result.manifest

        checks: dict[str, bool] = {}

        # --- transport interaction ---------------------------------------
        requests_made = len(transport.captured_payloads)
        if expected.get("request_made") is not None:
            checks["request_made"] = requests_made == (1 if expected["request_made"] else 0)
        else:
            checks["request_made"] = True

        # Prompt isolation: delimiters present in user message; system message
        # contains no repository content; no absolute local path leaks out.
        prompt_isolated = True
        for payload in transport.captured_payloads:
            messages = payload.get("messages", [])
            if len(messages) < 2:
                prompt_isolated = False
                break
            system_msg = messages[0].get("content", "")
            user_msg = messages[1].get("content", "")
            if "<UNTRUSTED_REPOSITORY_CONTEXT>" not in user_msg or "</UNTRUSTED_REPOSITORY_CONTEXT>" not in user_msg:
                prompt_isolated = False
            for changed in review_result.manifest.analyzed_files:
                if changed in system_msg:
                    prompt_isolated = False
            if str(work) in json.dumps(payload) or str(work) in system_msg:
                prompt_isolated = False
        checks["prompt_isolated"] = prompt_isolated

        # --- output validation and merge expectations --------------------
        if expected.get("provider_output_valid") is None:
            checks["provider_output_valid"] = manifest.provider_output_valid is None
        else:
            checks["provider_output_valid"] = manifest.provider_output_valid == expected["provider_output_valid"]

        final_policy = manifest.policy_decisions[-1].get("decision") if manifest.policy_decisions else "unknown"
        if expected.get("expected_policy") is not None:
            checks["policy"] = final_policy == expected["expected_policy"]
        else:
            checks["policy"] = True

        merged = manifest.merged_findings or []
        if expected.get("reviewer_findings_merged") is not None:
            checks["merged_count"] = len(merged) == expected["reviewer_findings_merged"]
        else:
            checks["merged_count"] = True

        if expected.get("merged_origin") is not None:
            checks["merged_origin"] = any(
                f.get("provenance", {}).get("origin") == expected["merged_origin"] for f in merged
            )
        else:
            checks["merged_origin"] = True

        if expected.get("merged_status") is not None:
            checks["merged_status"] = any(f.get("status") == expected["merged_status"] for f in merged)
        else:
            checks["merged_status"] = True

        if expected.get("merged_fixability") is not None:
            checks["merged_fixability"] = any(f.get("fixability") == expected["merged_fixability"] for f in merged)
        else:
            checks["merged_fixability"] = True

        safety_events = manifest.provider_safety_events or []
        if expected.get("expect_sanitized"):
            checks["sanitized"] = any(
                e.startswith("status_rewrite")
                or e in {"evidence_strength_clamped", "fixability_forced"}
                for e in safety_events
            )
        else:
            checks["sanitized"] = True

        if expected.get("expect_extra_keys_ignored"):
            checks["extra_keys_ignored"] = any(
                "unsupported top-level keys" in e for e in (manifest.provider_validation_errors or [])
            )
        else:
            checks["extra_keys_ignored"] = True

        if expected.get("expect_rejected"):
            checks["rejected"] = (manifest.provider_output_valid is False) and bool(
                manifest.provider_validation_errors
            )
        else:
            checks["rejected"] = True

        if expected.get("expect_secret_rejection"):
            checks["secret_rejected"] = any(
                e == "output_rejected_secret_leak" for e in safety_events
            ) or any("secret" in e.lower() for e in (manifest.provider_validation_errors or []))
        else:
            checks["secret_rejected"] = True

        if expected.get("expect_transport_failure"):
            checks["transport_failure"] = f"request_failed_{expected['expect_transport_failure']}" in safety_events
        else:
            checks["transport_failure"] = True

        if expected.get("provider_skipped"):
            checks["provider_skipped"] = any("Live provider skipped" in e for e in manifest.errors)
        else:
            checks["provider_skipped"] = True

        # --- global safety invariants ------------------------------------
        # No repository code execution and no test execution, ever.
        checks["no_repo_execution"] = all(t.startswith("git ") for t in manifest.tools_run) and not manifest.tests_run

        # No raw secrets in manifest or evidence log.
        secret_free = True
        for artifact in (manifest_path, evidence_path):
            if artifact.is_file():
                text = artifact.read_text(encoding="utf-8", errors="replace")
                if any(pat.search(text) for pat in SECRET_PATTERNS):
                    secret_free = False
        checks["artifacts_secret_free"] = secret_free

        # Prompt payload must not contain raw secrets either.
        prompt_secret_free = True
        for payload in transport.captured_payloads:
            if any(pat.search(json.dumps(payload)) for pat in SECRET_PATTERNS):
                prompt_secret_free = False
        checks["prompt_secret_free"] = prompt_secret_free

        usage = manifest.provider_usage or {}
        sanitized_markers = ("status_rewrite", "evidence_strength_clamped", "fixability_forced")
        return {
            "case_id": metadata["case_id"],
            "scenario": metadata["scenario"],
            "test_group": metadata.get("test_group", "contract"),
            "expected_output_valid": expected.get("provider_output_valid"),
            "was_sanitized": any(
                e.startswith("status_rewrite") or e in sanitized_markers[1:] for e in safety_events
            ),
            "requests_made": requests_made,
            "latency_ms": usage.get("latency_ms"),
            "retries": usage.get("retries", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "estimated_cost": usage.get("estimated_cost", 0.0),
            "provider_output_valid": manifest.provider_output_valid,
            "provider_validation_error_count": len(manifest.provider_validation_errors or []),
            "safety_events": len(safety_events),
            "final_policy": final_policy,
            "merged_findings": len(merged),
            "duration_ms": duration_ms,
            "checks": checks,
            "pass": all(checks.values()),
        }


def run_live_review_eval(cases_dir: Path) -> list[dict[str, Any]]:
    case_dirs = sorted(d for d in cases_dir.iterdir() if d.is_dir() and (d / "metadata.json").is_file())
    return [evaluate_live_review_case(cd) for cd in case_dirs]


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def summarize_live_review(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate Phase 7A metrics.  Groups are reported separately by design."""
    n = len(results)
    if n == 0:
        return {"total_cases": 0}

    def group(name: str) -> list[dict[str, Any]]:
        return [r for r in results if r["test_group"] == name]

    contract = group("contract")
    adversarial = group("adversarial")
    transport = group("transport")

    # ---- Transport metrics (label: transport tests) ----
    attempted = [r for r in results if r["requests_made"] > 0]
    completed = [r for r in attempted if r["latency_ms"] is not None]
    latencies = sorted(float(r["latency_ms"]) for r in completed)
    p95_idx = max(0, int(0.95 * len(latencies)) - 1) if latencies else 0
    timeout_rate = sum(1 for r in transport if "timeout" in r["scenario"]) / len(transport) if transport else 0.0
    auth_rate = sum(1 for r in transport if "auth" in r["scenario"]) / len(transport) if transport else 0.0
    rate_limit_rate = sum(1 for r in transport if "rate_limit" in r["scenario"]) / len(transport) if transport else 0.0
    malformed_rate = sum(
        1 for r in results if "malformed" in r["scenario"]
    ) / n if n else 0.0

    transport_metrics = {
        "label": "transport tests (fake transport)",
        "request_success_rate": _rate(len(completed), len(attempted)),
        "request_attempts": len(attempted),
        "timeout_rate": timeout_rate,
        "authentication_failure_rate": auth_rate,
        "rate_limit_rate": rate_limit_rate,
        "malformed_response_rate": malformed_rate,
        "mean_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
        "p95_latency_ms": latencies[p95_idx] if latencies else 0.0,
        "mean_retries": sum(r["retries"] for r in results) / n if n else 0.0,
        "total_output_tokens": sum(r["output_tokens"] for r in results),
        "total_estimated_cost": round(sum(r["estimated_cost"] for r in results), 6),
    }

    # ---- Review-quality metrics (label: contract tests) ----
    expected_valid = [r for r in results if r.get("expected_output_valid") is True]
    accepted = sum(1 for r in expected_valid if r["provider_output_valid"])
    agreement_cases = [r for r in contract if "agreement" in r["scenario"]]
    agreement_hits = sum(
        1 for r in agreement_cases if r["merged_findings"] >= 1 and r["checks"].get("merged_origin", True)
    )
    unsupported_scenarios = {"unsupported_validated_fixability", "fabricated_test_consultation"}
    unsupported_cases = [r for r in results if r["scenario"] in unsupported_scenarios]
    unsupported_neutralized = sum(
        1 for r in unsupported_cases if r["provider_output_valid"] is False or r["was_sanitized"]
    )
    duplicate_cases = [r for r in results if "duplicate" in r["scenario"]]
    duplicate_handled = sum(1 for r in duplicate_cases if r["provider_validation_error_count"] > 0)
    anchor_cases = [r for r in results if r["scenario"] in {
        "finding_outside_diff_lines", "finding_on_deleted_file", "finding_invalid_line_range",
        "finding_outside_packet_path",
    }]
    anchor_rejected = sum(1 for r in anchor_cases if not r["provider_output_valid"])
    secret_cases = [r for r in results if r["scenario"] in {"provider_returns_raw_secret", "provider_requests_credentials"}]
    secret_rejected = sum(1 for r in secret_cases if r["checks"].get("secret_rejected", False))
    bypass_cases = adversarial
    bypass_prevented = sum(1 for r in bypass_cases if r["checks"].get("policy", False))
    abstained = sum(1 for r in results if r["final_policy"] == "abstain" or r["requests_made"] == 0)

    quality_metrics = {
        "label": "contract tests (fake transport; benchmark review quality NOT claimed)",
        "validation_acceptance_rate": _rate(accepted, len(expected_valid)),
        "validation_acceptance_numerator": accepted,
        "validation_acceptance_denominator": len(expected_valid),
        "deterministic_evidence_agreement_rate": _rate(agreement_hits, len(agreement_cases)),
        "unsupported_claim_neutralization_rate": _rate(unsupported_neutralized, len(unsupported_cases)),
        "duplicate_handling_rate": _rate(duplicate_handled, len(duplicate_cases)),
        "changed_line_anchor_rejection_accuracy": _rate(anchor_rejected, len(anchor_cases)),
        "raw_secret_rejection_rate": _rate(secret_rejected, len(secret_cases)),
        "abstention_rate": _rate(abstained, n),
        "merged_finding_total": sum(r["merged_findings"] for r in results),
    }

    # ---- Adversarial safety metrics (label: adversarial safety tests) ----
    safety_metrics = {
        "label": "adversarial safety tests (fake transport)",
        "adversarial_case_count": len(adversarial),
        "adversarial_case_pass_rate": _rate(sum(1 for r in adversarial if r["pass"]), len(adversarial)),
        "policy_bypass_prevention_rate": _rate(bypass_prevented, len(bypass_cases)),
        "prompt_isolation_rate": _rate(sum(1 for r in results if r["checks"].get("prompt_isolated")), n),
        "artifacts_secret_free_rate": _rate(sum(1 for r in results if r["checks"].get("artifacts_secret_free")), n),
        "prompt_secret_free_rate": _rate(sum(1 for r in results if r["checks"].get("prompt_secret_free")), n),
        "no_repo_execution_rate": _rate(sum(1 for r in results if r["checks"].get("no_repo_execution")), n),
    }

    return {
        "total_cases": n,
        "pass_rate": _rate(sum(1 for r in results if r["pass"]), n),
        "coverage": {
            "contract_cases": len(contract),
            "adversarial_cases": len(adversarial),
            "transport_cases": len(transport),
        },
        "transport_metrics": transport_metrics,
        "review_quality_metrics": quality_metrics,
        "adversarial_safety_metrics": safety_metrics,
        "disclaimer": (
            "All cases run against a deterministic fake transport. These are transport, contract, "
            "and adversarial safety tests; no live-provider model precision or recall is measured."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 7A Live Provider Evaluation Runner")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "live-review")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_live_review_eval(args.cases)
    summary = summarize_live_review(results)

    print("\nPhase 7A Live Provider Evaluation Summary:", file=sys.stderr)
    print(f"  Total Cases: {summary.get('total_cases', 0)}  (pass rate {summary.get('pass_rate', 0.0):.4f})", file=sys.stderr)
    coverage = summary.get("coverage", {})
    print(
        f"  Coverage: {coverage.get('contract_cases', 0)} contract, "
        f"{coverage.get('adversarial_cases', 0)} adversarial safety, "
        f"{coverage.get('transport_cases', 0)} transport",
        file=sys.stderr,
    )

    tm = summary["transport_metrics"]
    print(f"  [{tm['label']}]", file=sys.stderr)
    print(f"    Request Success Rate: {tm['request_success_rate']:.4f} ({tm['request_attempts']} attempts)", file=sys.stderr)
    print(f"    Timeout/Auth/RateLimit/Malformed Rates: {tm['timeout_rate']:.4f} / {tm['authentication_failure_rate']:.4f} / {tm['rate_limit_rate']:.4f} / {tm['malformed_response_rate']:.4f}", file=sys.stderr)
    print(f"    Mean Latency: {tm['mean_latency_ms']:.2f} ms  P95: {tm['p95_latency_ms']:.2f} ms", file=sys.stderr)
    print(f"    Mean Retries: {tm['mean_retries']:.2f}  Output Tokens: {tm['total_output_tokens']}  Est. Cost: {tm['total_estimated_cost']}", file=sys.stderr)

    qm = summary["review_quality_metrics"]
    print(f"  [{qm['label']}]", file=sys.stderr)
    print(
        f"    Validation Acceptance Rate: {qm['validation_acceptance_rate']:.4f} "
        f"({qm['validation_acceptance_numerator']}/{qm['validation_acceptance_denominator']})",
        file=sys.stderr,
    )
    print(f"    Deterministic-Evidence Agreement Rate: {qm['deterministic_evidence_agreement_rate']:.4f}", file=sys.stderr)
    print(f"    Unsupported-Claim Neutralization Rate: {qm['unsupported_claim_neutralization_rate']:.4f}", file=sys.stderr)
    print(f"    Duplicate Handling Rate: {qm['duplicate_handling_rate']:.4f}", file=sys.stderr)
    print(f"    Changed-Line Anchor Rejection Accuracy: {qm['changed_line_anchor_rejection_accuracy']:.4f}", file=sys.stderr)
    print(f"    Raw-Secret Rejection Rate: {qm['raw_secret_rejection_rate']:.4f}", file=sys.stderr)
    print(f"    Abstention Rate: {qm['abstention_rate']:.4f}", file=sys.stderr)

    sm = summary["adversarial_safety_metrics"]
    print(f"  [{sm['label']}]", file=sys.stderr)
    print(f"    Adversarial Case Pass Rate: {sm['adversarial_case_pass_rate']:.4f} ({sm['adversarial_case_count']} cases)", file=sys.stderr)
    print(f"    Policy-Bypass Prevention Rate: {sm['policy_bypass_prevention_rate']:.4f}", file=sys.stderr)
    print(f"    Prompt Isolation Rate: {sm['prompt_isolation_rate']:.4f}", file=sys.stderr)
    print(f"    Artifacts Secret-Free Rate: {sm['artifacts_secret_free_rate']:.4f}", file=sys.stderr)
    print(f"    No-Repository-Execution Rate: {sm['no_repo_execution_rate']:.4f}", file=sys.stderr)
    print(f"  Note: {summary['disclaimer']}", file=sys.stderr)

    failed = [r for r in results if not r["pass"]]
    if failed:
        print("\nFailed cases:", file=sys.stderr)
        for r in failed:
            failing = [k for k, v in r["checks"].items() if not v]
            print(f"  {r['case_id']}: failing checks: {failing}", file=sys.stderr)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, sort_keys=True) + "\n")
            f.write(json.dumps({"_summary": summary}, sort_keys=True) + "\n")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
