"""Phase 9B inline-comment evaluation runner.

Reuses the Phase 9A fixture/runner helpers.  E2E cases drive
``run_github_pr_review(inline=True)`` against a deterministic
FakeGitHubTransport and isolated local Git repositories; probe cases exercise
the pure line-mapping/classification function.  No network calls and no real
GitHub writes occur.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from codeatlas.github import FakeGitHubTransport, GitHubError, SHAMismatchError, parse_marker, run_github_pr_review
from codeatlas.github.adapter import build_diff_line_map, classify_inline_target
from codeatlas.review.packet import SECRET_PATTERNS

from eval_github import _build_repo, _tree_hash  # noqa: E402  (same eval directory)


def evaluate_inline_case(case_path: Path) -> dict[str, Any]:
    case = json.loads(case_path.read_text(encoding="utf-8"))
    expected = case["expected"]
    checks: dict[str, bool] = {}

    if case["kind"] == "probe":
        line_map = build_diff_line_map(case["diff"])
        decision, details = classify_inline_target(case["finding"], line_map)
        checks["decision"] = decision == expected["decision"]
        if expected.get("reason"):
            checks["reason"] = details.get("reason") == expected["reason"]
        if expected.get("inline_details") is not None:
            checks["inline_details"] = details == expected["inline_details"]
        return {
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "checks": checks,
            "pass": all(checks.values()),
        }

    import tempfile

    with tempfile.TemporaryDirectory(prefix="codeatlas-gh-inline-eval-") as temp:
        work = Path(temp) / "repo"
        base_sha, head_sha, diff_text = _build_repo(work, case["base_tree"], case["head_tree"])
        tree_before = _tree_hash(work)

        metadata: dict[str, Any] = {
            "number": 42, "title": "eval", "base_sha": base_sha, "head_sha": head_sha,
        }
        kwargs: dict[str, Any] = {}
        if case.get("transport_fault") == "sha_changed_write_check":
            kwargs["head_sha_on_write_check"] = "d" * 40

        existing_bodies: list[str] = []
        for body in case.get("existing_comments", []):
            if body == "__POSTED_MARKER__":
                probe_transport = FakeGitHubTransport(
                    metadata=metadata, files=case["files"], diff_text=diff_text, comments=[]
                )
                probe_report = run_github_pr_review(
                    "o/r", 42, local_repo=work, transport=probe_transport,
                    post=True, inline=False,
                )
                existing_bodies = list(probe_transport.posted_bodies)
                checks["issue_probe_posted"] = probe_report.comments_posted == 1
            else:
                existing_bodies.append(body)
        existing_inline_bodies: list[str] = []
        for body in case.get("existing_inline_comments", []):
            if body == "__POSTED_MARKER__":
                probe_transport = FakeGitHubTransport(
                    metadata=metadata, files=case["files"], diff_text=diff_text,
                    comments=[], inline_comments=[],
                )
                run_github_pr_review(
                    "o/r", 42, local_repo=work, transport=probe_transport,
                    post=True, inline=True,
                )
                existing_inline_bodies = list(probe_transport.posted_bodies)
            else:
                existing_inline_bodies.append(body)

        transport = FakeGitHubTransport(
            metadata=metadata,
            files=case["files"],
            diff_text=diff_text,
            comments=[{"comment_id": i, "body": b} for i, b in enumerate(existing_bodies)],
            inline_comments=[{"comment_id": 100 + i, "body": b} for i, b in enumerate(existing_inline_bodies)],
            **kwargs,
        )

        raised = None
        report = None
        try:
            report = run_github_pr_review(
                "o/r", 42, local_repo=work, transport=transport,
                post=bool(case.get("post")), inline=bool(case.get("inline")),
            )
        except GitHubError as err:
            raised = type(err).__name__

        expected_raises = expected.get("raises")
        checks["raises"] = raised == expected_raises
        checks["write_calls"] = transport.write_calls == expected.get("write_calls")
        if report is not None:
            checks["comments_planned"] = report.comments_planned == expected.get("comments_planned", 1)
            checks["comments_posted"] = report.comments_posted == expected.get("comments_posted", 0)
            checks["duplicate_suppressed"] = (
                report.comments_skipped_duplicate == expected.get("duplicate_suppressed", 0)
            )
        else:
            checks["comments_planned"] = expected.get("comments_planned", 1) == 0
            checks["comments_posted"] = expected.get("comments_posted", 0) == 0
            checks["duplicate_suppressed"] = expected.get("duplicate_suppressed", 0) == 0

        checks["inline_endpoint"] = len(transport.posted_inline) == expected.get("inline_endpoint", 0)
        issue_posts = sum(1 for m, _ in transport.calls if m == "post_comment")
        checks["issue_endpoint"] = issue_posts == expected.get("issue_endpoint", 0)
        checks["worktree_unchanged"] = _tree_hash(work) == tree_before
        bodies_text = "\n".join(transport.posted_bodies)
        checks["body_secret_free"] = (
            not any(pat.search(bodies_text) for pat in SECRET_PATTERNS)
            and "AKIAIOSFODNN7EXAMPLE" not in bodies_text
        )
        if transport.posted_inline:
            checks["markers_inline"] = all(
                (parsed := parse_marker(d["body"])) is not None
                and parsed["mode"] == "inline"
                and parsed["head"] == head_sha
                for d in transport.posted_inline
            )
        else:
            checks["markers_inline"] = True

        return {
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "checks": checks,
            "pass": all(checks.values()),
        }


def run_inline_eval(cases_dir: Path) -> list[dict[str, Any]]:
    return [evaluate_inline_case(p) for p in sorted(cases_dir.glob("case-*.json"))]


def _rate(n: int, d: int) -> float:
    return n / d if d else 1.0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    e2e = [r for r in results if "worktree_unchanged" in r["checks"]]
    probes = [r for r in results if r["checks"].get("decision") is not None]
    duplicates = [r for r in results if "duplicate" in r["scenario"]]
    return {
        "total_cases": n,
        "pass_rate": _rate(sum(1 for r in results if r["pass"]), n),
        "inline_metrics": {
            "label": "inline comment tests (fake transport)",
            "inline_posting_success_rate": _rate(
                sum(1 for r in e2e if r["checks"].get("inline_endpoint")), len(e2e)),
            "issue_endpoint_accuracy": _rate(
                sum(1 for r in e2e if r["checks"].get("issue_endpoint")), len(e2e)),
            "original_worktree_safety": _rate(
                sum(1 for r in e2e if r["checks"].get("worktree_unchanged")), len(e2e)),
            "body_secret_free_rate": _rate(
                sum(1 for r in e2e if r["checks"].get("body_secret_free")), len(e2e)),
            "duplicate_suppression_rate": _rate(
                sum(1 for r in duplicates if r["checks"].get("duplicate_suppressed")), len(duplicates)),
        },
        "mapping_metrics": {
            "label": "line mapping classification tests (pure functions)",
            "classification_accuracy": _rate(
                sum(1 for r in probes if r["pass"]), len(probes)),
        },
        "disclaimer": (
            "All cases run against a deterministic fake GitHub transport; no network calls and "
            "no real GitHub writes occur. Line mapping is derived from the local diff between "
            "the verified SHAs; only added lines are inline-eligible."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 9B Inline Comment Evaluation Runner")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "github-inline")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_inline_eval(args.cases)
    summary = summarize(results)

    print("\nPhase 9B Inline Comment Evaluation Summary:", file=sys.stderr)
    print(f"  Total Cases: {summary['total_cases']}  (pass rate {summary['pass_rate']:.4f})", file=sys.stderr)
    for group in ("inline_metrics", "mapping_metrics"):
        m = summary[group]
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
