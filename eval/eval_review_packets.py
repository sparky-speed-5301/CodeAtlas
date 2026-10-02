"""Review packet, policy gating, and reviewer provider evaluation runner (Phase 5).

Executes bounded review packet assembly and policy evaluation on isolated Git fixtures
without executing fixture code, tests, or calling external LLMs or networks.
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
from typing import Any, Mapping

from codeatlas.orchestrator.review import run_review
from codeatlas.review.packet import SECRET_PATTERNS


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def evaluate_review_packet_case(case_dir: Path) -> dict[str, Any]:
    """Evaluate review packet assembly and policy gating on an isolated temporary Git fixture."""
    meta_path = case_dir / "metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))

    before_dir = case_dir / "before"
    after_dir = case_dir / "after"

    with tempfile.TemporaryDirectory(prefix="codeatlas-packet-eval-") as temp:
        work = Path(temp) / "work"
        work.mkdir()

        _git(work, "init", "-q")
        _git(work, "config", "user.email", "eval@example.invalid")
        _git(work, "config", "user.name", "eval")

        # Base commit
        if before_dir.is_dir() and any(before_dir.rglob("*")):
            shutil.copytree(before_dir, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            _git(work, "add", "-A")
            _git(work, "commit", "--allow-empty", "-qm", "before")
        else:
            _git(work, "commit", "--allow-empty", "-qm", "before")

        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()

        # Clean work tree
        for item in list(work.iterdir()):
            if item.name != ".git":
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        # Head commit
        time.sleep(0.05)
        if after_dir.is_dir() and any(after_dir.rglob("*")):
            shutil.copytree(after_dir, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            now = time.time() + 1.0
            for p in work.rglob("*"):
                if p.is_file():
                    try:
                        os.utime(p, (now, now))
                    except OSError:
                        pass

        # Write optional config if specified
        case_cfg = metadata.get("config")
        if case_cfg:
            try:
                import yaml
                (work / ".codeatlas.yml").write_text(yaml.safe_dump(case_cfg), encoding="utf-8")
            except Exception:
                pass

        _git(work, "add", "-A")
        _git(work, "commit", "--allow-empty", "-qm", "after")
        head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()

        packet_path = Path(temp) / "packet.json"
        policy_path = Path(temp) / "policy.json"
        manifest_path = Path(temp) / "manifest.json"

        provider_arg = metadata.get("provider", "mock:clean")

        started = time.perf_counter()
        review_result = run_review(
            work,
            base=base_sha,
            head=head_sha,
            assemble_review_packet=True,
            review_provider=provider_arg,
            packet_output=packet_path,
            policy_output=policy_path,
            manifest_output=manifest_path,
        )
        duration_ms = (time.perf_counter() - started) * 1000

        manifest = review_result.manifest

        # Check packet construction
        packet_success = manifest.review_packet_id is not None and packet_path.is_file()

        # Check raw secret absence in serialized packet
        packet_bytes_val = manifest.packet_bytes or 0
        raw_secret_leak = False
        if packet_path.is_file():
            pkt_text = packet_path.read_text(encoding="utf-8")
            for pat in SECRET_PATTERNS:
                if pat.search(pkt_text):
                    raw_secret_leak = True
                    break

        redaction_safe = not raw_secret_leak

        # Check policy accuracy
        actual_policy = manifest.policy_decisions[0].get("decision") if manifest.policy_decisions else "unknown"
        expected_policy = metadata.get("expected_policy")
        policy_match = (actual_policy == expected_policy)

        # Check provider validation
        expected_provider_valid = metadata.get("expected_provider_valid")
        if expected_provider_valid is not None:
            provider_valid_match = (manifest.provider_output_valid == expected_provider_valid)
        else:
            provider_valid_match = True

        # Check truncation
        expected_truncated = metadata.get("expected_truncated", False)
        truncation_match = (manifest.packet_truncated == expected_truncated)

        # Check duplicate merge if expected
        expected_merged_count = metadata.get("expected_merged_count")
        if expected_merged_count is not None:
            merge_match = (
                len(manifest.merged_findings) == expected_merged_count
                and any(f.get("provenance", {}).get("origin") == "merged" for f in manifest.merged_findings)
            )
        else:
            merge_match = True

        # Context priority retention
        context_items = manifest.context_candidates or []
        prio_order_valid = True
        prio_map = {"changed_code": 1, "symbol": 2, "caller": 3, "callee": 3, "test": 4, "configuration": 5}
        last_prio = 0
        for ci in context_items:
            st = ci.get("source_type", "other")
            cur_prio = prio_map.get(st, 6)
            if cur_prio < last_prio:
                prio_order_valid = False
                break
            last_prio = cur_prio

        passed = (
            packet_success
            and redaction_safe
            and policy_match
            and provider_valid_match
            and truncation_match
            and merge_match
        )

        return {
            "case_id": metadata["case_id"],
            "scenario": metadata["scenario"],
            "packet_success": packet_success,
            "redaction_safe": redaction_safe,
            "policy_match": policy_match,
            "provider_valid_match": provider_valid_match,
            "truncation_match": truncation_match,
            "merge_match": merge_match,
            "prio_order_valid": prio_order_valid,
            "actual_policy": actual_policy,
            "expected_policy": expected_policy,
            "provider_output_valid": manifest.provider_output_valid,
            "expected_provider_valid": expected_provider_valid,
            "packet_bytes": packet_bytes_val,
            "packet_files": manifest.packet_files or 0,
            "packet_lines": manifest.packet_lines or 0,
            "duration_ms": duration_ms,
            "pass": passed,
        }


def run_review_packets_eval(cases_dir: Path) -> list[dict[str, Any]]:
    case_dirs = sorted([d for d in cases_dir.iterdir() if d.is_dir() and (d / "metadata.json").is_file()])
    results: list[dict[str, Any]] = []
    for cd in case_dirs:
        res = evaluate_review_packet_case(cd)
        results.append(res)
    return results


def summarize_review_packets(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    if n == 0:
        return {}

    successful_packets = sum(1 for r in results if r["packet_success"])
    packet_success_rate = successful_packets / n

    redaction_safe_count = sum(1 for r in results if r["redaction_safe"])
    redaction_safety_rate = redaction_safe_count / n

    policy_correct = sum(1 for r in results if r["policy_match"])
    policy_decision_accuracy = policy_correct / n

    # Provider validation rejection
    invalid_cases = [r for r in results if r["expected_provider_valid"] is False]
    rej_numerator = sum(1 for r in invalid_cases if r["provider_valid_match"])
    rej_denominator = len(invalid_cases)
    rejection_rate = rej_numerator / rej_denominator if rej_denominator > 0 else 1.0

    # Duplicate merge rate
    merge_cases = [r for r in results if r.get("merge_match") is not None and "duplicate" in r["scenario"]]
    merge_num = sum(1 for r in merge_cases if r["merge_match"])
    merge_den = len(merge_cases)
    duplicate_merge_rate = merge_num / merge_den if merge_den > 0 else 1.0

    # Truncation rate
    truncated_count = sum(1 for r in results if r.get("truncation_match") and "truncation" in r["scenario"])

    # Durations and sizes
    durations = sorted(r["duration_ms"] for r in results)
    sizes = sorted(r["packet_bytes"] for r in results)

    mean_duration = sum(durations) / n
    p95_idx = int(0.95 * n) - 1
    p95_duration = durations[max(0, p95_idx)]

    mean_bytes = sum(sizes) / n
    p95_bytes = sizes[max(0, p95_idx)]

    priority_valid_count = sum(1 for r in results if r["prio_order_valid"])
    priority_accuracy = priority_valid_count / n

    return {
        "total_cases": n,
        "packet_construction_success_rate": packet_success_rate,
        "packet_construction_numerator": successful_packets,
        "packet_construction_denominator": n,
        "redaction_safety": redaction_safety_rate,
        "redaction_safety_numerator": redaction_safe_count,
        "redaction_safety_denominator": n,
        "policy_decision_accuracy": policy_decision_accuracy,
        "policy_decision_numerator": policy_correct,
        "policy_decision_denominator": n,
        "provider_validation_rejection_rate": rejection_rate,
        "provider_validation_rejection_numerator": rej_numerator,
        "provider_validation_rejection_denominator": rej_denominator,
        "duplicate_merge_rate": duplicate_merge_rate,
        "duplicate_merge_numerator": merge_num,
        "duplicate_merge_denominator": merge_den,
        "truncation_rate": truncated_count / n,
        "context_retention_priority_rate": priority_accuracy,
        "mean_packet_bytes": mean_bytes,
        "p95_packet_bytes": p95_bytes,
        "min_packet_bytes": sizes[0] if sizes else 0,
        "max_packet_bytes": sizes[-1] if sizes else 0,
        "mean_packet_duration_ms": mean_duration,
        "p95_packet_duration_ms": p95_duration,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 5 Review Packet Evaluation Runner")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "review-packets")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_review_packets_eval(args.cases)
    summary = summarize_review_packets(results)

    print("\nPhase 5 Review Packet Evaluation Summary:", file=sys.stderr)
    print(f"  Total Cases: {summary.get('total_cases', 0)}", file=sys.stderr)
    print(
        f"  Packet Construction Success Rate: {summary.get('packet_construction_success_rate', 0.0):.4f} "
        f"({summary.get('packet_construction_numerator', 0)}/{summary.get('packet_construction_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Redaction Safety: {summary.get('redaction_safety', 0.0):.4f} "
        f"({summary.get('redaction_safety_numerator', 0)}/{summary.get('redaction_safety_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Policy Decision Accuracy: {summary.get('policy_decision_accuracy', 0.0):.4f} "
        f"({summary.get('policy_decision_numerator', 0)}/{summary.get('policy_decision_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Provider Validation Rejection Rate: {summary.get('provider_validation_rejection_rate', 0.0):.4f} "
        f"({summary.get('provider_validation_rejection_numerator', 0)}/{summary.get('provider_validation_rejection_denominator', 0)})",
        file=sys.stderr,
    )
    print(
        f"  Duplicate Merge Rate: {summary.get('duplicate_merge_rate', 0.0):.4f} "
        f"({summary.get('duplicate_merge_numerator', 0)}/{summary.get('duplicate_merge_denominator', 0)})",
        file=sys.stderr,
    )
    print(f"  Context Retention Priority Rate: {summary.get('context_retention_priority_rate', 0.0):.4f}", file=sys.stderr)
    print(f"  Mean Packet Bytes: {summary.get('mean_packet_bytes', 0.0):.1f} bytes", file=sys.stderr)
    print(f"  P95 Packet Bytes: {summary.get('p95_packet_bytes', 0.0):.1f} bytes", file=sys.stderr)
    print(f"  Mean Assembly Duration: {summary.get('mean_packet_duration_ms', 0.0):.2f} ms", file=sys.stderr)
    print(f"  P95 Assembly Duration: {summary.get('p95_packet_duration_ms', 0.0):.2f} ms", file=sys.stderr)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, sort_keys=True) + "\n")
            f.write(json.dumps({"_summary": summary}, sort_keys=True) + "\n")

    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
