"""Phase 9C (summary comment) and 9D (check run) evaluation runner.

E2E cases drive ``run_github_pr_review`` with summary/check/inline flags
against a deterministic FakeGitHubTransport and isolated local Git
repositories; probe cases exercise the pure renderers/mappers.  No network
calls and no real GitHub writes occur.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from codeatlas.github import (
    FakeGitHubTransport,
    GitHubAuthError,
    GitHubError,
    GitHubNetworkError,
    GitHubRateLimitError,
    GitHubResponseError,
    SHAMismatchError,
    map_check_conclusion,
    render_summary_comment,
    run_github_pr_review,
)
from codeatlas.github.checks import check_external_id
from codeatlas.github.summary import summary_marker
from codeatlas.review.packet import SECRET_PATTERNS

from eval_github import _build_repo, _tree_hash  # same eval directory

IMPORT_PATH = __import__("codeatlas.github.adapter", fromlist=["adapter"])


def evaluate_9cd_case(case_path: Path) -> dict[str, Any]:
    case = json.loads(case_path.read_text(encoding="utf-8"))
    expected = case["expected"]
    checks: dict[str, bool] = {}

    if case.get("kind") == "probe":
        fn = {"render_summary_comment": render_summary_comment,
              "map_check_conclusion": map_check_conclusion}[case["fn"]]
        result = fn(**case["args"])
        if case["fn"] == "map_check_conclusion":
            checks["conclusion"] = result == expected["conclusion"]
        else:
            body = result[0] if isinstance(result, tuple) else result
            for needle in expected.get("contains", []):
                checks[f"contains:{needle[:24]}"] = needle in body
        return {
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "checks": checks,
            "pass": all(checks.values()),
        }

    import tempfile

    with tempfile.TemporaryDirectory(prefix="codeatlas-gh-9cd-eval-") as temp:
        work = Path(temp) / "repo"
        base_sha, head_sha, diff_text = _build_repo(work, case["base_tree"], case["head_tree"])

        config = case.get("config") or {}
        if config:
            import yaml

            (work / ".codeatlas.yml").write_text(yaml.safe_dump(config), encoding="utf-8")

        tree_before = _tree_hash(work)

        fault = case.get("transport_fault")
        metadata: dict[str, Any] = {
            "number": 42, "title": "eval", "base_sha": base_sha, "head_sha": head_sha,
        }
        kwargs: dict[str, Any] = {}
        if fault == "metadata_auth":
            kwargs["metadata_error"] = GitHubAuthError("simulated auth failure")
        elif fault == "metadata_malformed":
            kwargs["metadata_error"] = GitHubResponseError("simulated malformed response")
        elif fault == "network":
            kwargs["metadata_error"] = GitHubNetworkError("simulated API outage")
        elif fault == "write_rate_limit":
            kwargs["post_error"] = GitHubRateLimitError("simulated rate limit")
            kwargs["check_run_error"] = GitHubRateLimitError("simulated rate limit")
        elif fault == "sha_changed_write_check":
            kwargs["head_sha_on_write_check"] = "d" * 40

        existing_comments: list[str] = []
        for body in case.get("existing_comments", []):
            if body == "__SUMMARY_MARKER__":
                existing_comments.append("prior run\n" + summary_marker(42, head_sha))
            elif body == "__OTHER_HEAD_SUMMARY__":
                existing_comments.append("old head\n" + summary_marker(42, "c" * 40))
            else:
                existing_comments.append(body)

        existing_check_runs = [
            {**c, "head_sha": c["head_sha"].replace("__HEAD__", head_sha),
             "external_id": c.get("external_id", "").replace("__EXTERNAL_ID__", check_external_id(42, head_sha))}
            for c in case.get("existing_check_runs", [])
        ]

        transport = FakeGitHubTransport(
            metadata=metadata,
            files=case["files"],
            diff_text=diff_text,
            comments=[{"comment_id": i, "body": b} for i, b in enumerate(existing_comments)],
            inline_comments=[],
            check_runs=list(existing_check_runs),
            **kwargs,
        )

        raised = None
        report = None
        redaction_fault = fault in {"summary_redaction", "check_redaction"}
        saved_predicate = IMPORT_PATH._contains_raw_secret
        if redaction_fault:
            IMPORT_PATH._contains_raw_secret = lambda text: True
        try:
            report = run_github_pr_review(
                "o/r", 42, local_repo=work, transport=transport,
                post=bool(case.get("post")),
                inline=bool(case.get("inline")),
                summary_comment=bool(case.get("summary")),
                check_run=bool(case.get("check")),
            )
        except GitHubError as err:
            raised = type(err).__name__
        finally:
            if redaction_fault:
                IMPORT_PATH._contains_raw_secret = saved_predicate

        expected_raises = expected.get("raises")
        checks["raises"] = raised == expected_raises
        checks["write_calls"] = transport.write_calls == expected.get("write_calls")
        if report is not None:
            checks["comments_planned"] = report.comments_planned == expected.get("comments_planned", 1)
            checks["summary_posted"] = (1 if report.summary_comment_posted else 0) == expected.get("summary_posted", 0)
            checks["summary_updated"] = (1 if report.summary_comment_updated else 0) == expected.get("summary_updated", 0)
            checks["inline_endpoint"] = report.inline_comments_posted == expected.get("inline_endpoint", 0)
            checks["check_created"] = (1 if report.check_run_created else 0) == expected.get("check_created", 0)
            checks["check_updated"] = (1 if report.check_run_updated else 0) == expected.get("check_updated", 0)
            checks["partial_write"] = report.partial_write == expected.get("partial_write", False)
            if expected.get("conclusion"):
                checks["conclusion"] = report.check_run_conclusion == expected["conclusion"]
        else:
            checks["summary_posted"] = expected.get("summary_posted", 0) == 0
            checks["summary_updated"] = expected.get("summary_updated", 0) == 0
            checks["inline_endpoint"] = expected.get("inline_endpoint", 0) == 0
            checks["check_created"] = expected.get("check_created", 0) == 0
            checks["check_updated"] = expected.get("check_updated", 0) == 0

        checks["worktree_unchanged"] = _tree_hash(work) == tree_before
        bodies_text = "\n".join(transport.posted_bodies)
        checks["body_secret_free"] = (
            not any(pat.search(bodies_text) for pat in SECRET_PATTERNS)
            and "AKIAIOSFODNN7EXAMPLE" not in bodies_text
        )

        return {
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "checks": checks,
            "pass": all(checks.values()),
        }


def run_9cd_eval(cases_dir: Path) -> list[dict[str, Any]]:
    return [evaluate_9cd_case(p) for p in sorted(cases_dir.glob("case-*.json"))]


def _rate(n: int, d: int) -> float:
    return n / d if d else 1.0


def summarize(results: list[dict[str, Any]], label: str) -> dict[str, Any]:
    n = len(results)
    e2e = [r for r in results if "worktree_unchanged" in r["checks"]]
    probes = [r for r in results if r["checks"].get("conclusion") is not None or any(
        k.startswith("contains:") for k in r["checks"]
    )]
    fail_closed = [r for r in e2e if r["scenario"].endswith(
        ("aborts", "blocks_write", "fails_closed", "never_writes", "zero_writes"))]
    return {
        "total_cases": n,
        "pass_rate": _rate(sum(1 for r in results if r["pass"]), n),
        "metrics": {
            "label": label,
            "render_success_rate": _rate(
                sum(1 for r in probes if r["pass"]), len(probes)),
            "original_worktree_safety": _rate(
                sum(1 for r in e2e if r["checks"].get("worktree_unchanged")), len(e2e)),
            "body_secret_free_rate": _rate(
                sum(1 for r in e2e if r["checks"].get("body_secret_free")), len(e2e)),
            "fail_closed_rate": _rate(
                sum(1 for r in fail_closed if r["pass"]), len(fail_closed)),
            "sha_change_prevention_rate": _rate(
                sum(1 for r in e2e if r["scenario"].endswith(("aborts",))
                    and r["checks"].get("raises", False)), max(1, len([
                        r for r in e2e if r["scenario"].endswith("aborts")]))),
        },
        "disclaimer": (
            "All cases run against a deterministic fake GitHub transport; no network calls and "
            "no real GitHub writes occur."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 9C/9D GitHub Summary and Check-Run Evaluation")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--label", default="github 9C/9D tests (fake transport)")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_9cd_eval(args.cases)
    summary = summarize(results, args.label)

    print(f"\nEvaluation Summary ({args.cases.name}):", file=sys.stderr)
    print(f"  Total Cases: {summary['total_cases']}  (pass rate {summary['pass_rate']:.4f})", file=sys.stderr)
    m = summary["metrics"]
    print(f"  [{m['label']}]", file=sys.stderr)
    for key, value in m.items():
        if key != "label":
            print(f"    {key.replace('_', ' ').title()}: {value:.4f}", file=sys.stderr)
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
