"""Phase 9A GitHub-integration evaluation runner.

Drives the read-only GitHub PR review flow end to end with a deterministic
FakeGitHubTransport and isolated local Git repositories.  No network calls
and no real GitHub writes occur.

Reported groups (separately labelled): transport/contract, safety (fail-closed
and redaction), and idempotency.  No claim about GitHub-side behaviour beyond
the typed adapter contract is made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from codeatlas.github import (
    FakeGitHubTransport,
    GitHubAuthError,
    GitHubError,
    GitHubNetworkError,
    GitHubResponseError,
    SHAMismatchError,
    parse_marker,
    parse_pr_metadata,
    run_github_pr_review,
)
from codeatlas.github.adapter import _is_anchored
from codeatlas.review.packet import SECRET_PATTERNS

ERROR_TYPES = {
    "SHAMismatchError": SHAMismatchError,
    "GitHubNetworkError": GitHubNetworkError,
    "GitHubResponseError": GitHubResponseError,
    "GitHubAuthError": GitHubAuthError,
    "GitHubError": GitHubError,
}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _tree_hash(work: Path) -> str:
    digest = hashlib.sha256()
    for p in sorted(work.rglob("*")):
        if ".git" in p.relative_to(work).parts or "__pycache__" in p.parts:
            continue
        if p.is_file():
            digest.update(str(p.relative_to(work)).encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


def _build_repo(root: Path, base_tree: dict, head_tree: dict) -> tuple[str, str, str]:
    (root / "src").mkdir(parents=True, exist_ok=True)
    for rel, content in base_tree.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    def commit_all(msg: str) -> str:
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", msg)
        return _git(root, "rev-parse", "HEAD")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "eval@example.invalid")
    _git(root, "config", "user.name", "eval")
    base_sha = commit_all("base")
    for rel, content in head_tree.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    head_sha = commit_all("head")
    return base_sha, head_sha, _git(root, "diff", base_sha, head_sha)


def evaluate_github_case(case_path: Path) -> dict[str, Any]:
    case = json.loads(case_path.read_text(encoding="utf-8"))
    expected = case["expected"]
    checks: dict[str, bool] = {}

    # Parse-level case: malformed response rejection.
    if case.get("parse_raw") is not None:
        raised = None
        try:
            parse_pr_metadata(case["parse_raw"])
        except GitHubResponseError:
            raised = "GitHubResponseError"
        checks["raises"] = raised == expected.get("raises")
        return {
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "checks": checks,
            "pass": all(checks.values()),
        }

    # Anchor-probe case: line/file anchoring gate.
    if case.get("anchor_probe"):
        probe = case["anchor_probe"]
        anchored = _is_anchored(probe, {probe["ranges_file"]: probe["ranges"]})
        checks["anchor_gate"] = anchored == (expected.get("comments_planned", 0) > 0)
        return {
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "checks": checks,
            "pass": all(checks.values()),
        }

    with tempfile.TemporaryDirectory(prefix="codeatlas-gh-eval-") as temp:
        work = Path(temp) / "repo"
        base_sha, head_sha, diff_text = _build_repo(
            work, case["base_tree"], case["head_tree"]
        )
        tree_before = _tree_hash(work)

        fault = case.get("transport_fault")
        metadata: dict[str, Any] = {
            "number": 42,
            "title": "eval",
            "state": "OPEN",
            "base_ref": "main",
            "head_ref": "feature",
            "base_sha": base_sha,
            "head_sha": head_sha,
        }
        files = list(case.get("files", []))
        kwargs: dict[str, Any] = {}
        if fault == "sha_mismatch":
            metadata["head_sha"] = "f" * 40
        elif fault == "network":
            kwargs["metadata_error"] = GitHubNetworkError("simulated API outage")
        elif fault == "diff":
            kwargs["diff_error"] = GitHubNetworkError("simulated diff failure")
        elif fault == "sha_changed":
            kwargs["head_sha_on_refetch"] = "e" * 40
        elif fault == "missing_auth":
            pass  # handled below with the real gh transport

        if fault == "missing_auth":
            import codeatlas.github.transport as transport_module

            original_which = shutil.which
            shutil.which = lambda _: None  # type: ignore[assignment]
            try:
                raised = None
                try:
                    transport_module.GhCliTransport().get_pr_metadata("o/r", 42)
                except GitHubAuthError:
                    raised = "GitHubAuthError"
                checks["raises"] = raised == expected.get("raises")
                checks["write_calls"] = True
                checks["worktree_unchanged"] = _tree_hash(work) == tree_before
                checks["body_secret_free"] = True
                checks["diff_fetched"] = True
                checks["metadata_fetched"] = True
                checks["comments_planned"] = True
                checks["comments_posted"] = True
                checks["duplicate_suppressed"] = True
            finally:
                shutil.which = original_which  # type: ignore[assignment]
            return {
                "case_id": case["case_id"],
                "scenario": case["scenario"],
                "checks": checks,
                "pass": all(checks.values()),
            }

        existing_bodies = []
        for body in case.get("existing_comments", []):
            if body == "__POSTED_MARKER__":
                # Run once in post mode to produce a genuine marker comment.
                probe_transport = FakeGitHubTransport(
                    metadata=metadata, files=files, diff_text=diff_text, comments=[]
                )
                probe_report = run_github_pr_review(
                    "o/r", 42, local_repo=work, transport=probe_transport, post=True
                )
                existing_bodies = list(probe_transport.posted_bodies)
                checks["first_run_posted"] = probe_report.comments_posted == 1
            else:
                existing_bodies.append(body)

        transport = FakeGitHubTransport(
            metadata=metadata,
            files=files,
            diff_text=diff_text,
            comments=[{"comment_id": i, "body": b} for i, b in enumerate(existing_bodies)],
            **kwargs,
        )

        raised = None
        report = None
        try:
            report = run_github_pr_review(
                "o/r", 42, local_repo=work, transport=transport, post=bool(case.get("post"))
            )
        except GitHubError as err:
            raised = type(err).__name__

        expected_raises = expected.get("raises")
        checks["raises"] = raised == expected_raises
        if report is not None:
            checks["write_calls"] = transport.write_calls == expected.get("write_calls")
            checks["comments_planned"] = report.comments_planned == expected.get("comments_planned")
            checks["comments_posted"] = report.comments_posted == expected.get("comments_posted")
            checks["duplicate_suppressed"] = (
                report.comments_skipped_duplicate == expected.get("duplicate_suppressed")
            )
        else:
            checks["write_calls"] = transport.write_calls == expected.get("write_calls", 0)
            checks["comments_planned"] = expected.get("comments_planned", 0) == 0
            checks["comments_posted"] = expected.get("comments_posted", 0) == 0
            checks["duplicate_suppressed"] = expected.get("duplicate_suppressed", 0) == 0

        if expected.get("metadata_fetched"):
            checks["metadata_fetched"] = any(m == "get_pr_metadata" for m, _ in transport.calls)
        else:
            checks["metadata_fetched"] = True
        if expected.get("diff_fetched"):
            checks["diff_fetched"] = any(m == "get_pr_diff" for m, _ in transport.calls)
        else:
            checks["diff_fetched"] = True

        checks["worktree_unchanged"] = _tree_hash(work) == tree_before
        bodies_text = "\n".join(transport.posted_bodies)
        checks["body_secret_free"] = not any(
            pat.search(bodies_text) for pat in SECRET_PATTERNS
        ) and "AKIAIOSFODNN7EXAMPLE" not in bodies_text
        if transport.posted_bodies:
            checks["markers_valid"] = all(
                parse_marker(b) is not None and parse_marker(b)["head"] == head_sha
                for b in transport.posted_bodies
            )
        else:
            checks["markers_valid"] = True

        return {
            "case_id": case["case_id"],
            "scenario": case["scenario"],
            "checks": checks,
            "pass": all(checks.values()),
        }


def run_github_eval(cases_dir: Path) -> list[dict[str, Any]]:
    results = []
    for case_path in sorted(cases_dir.glob("case-*.json")):
        results.append(evaluate_github_case(case_path))
    return results


def _rate(n: int, d: int) -> float:
    return n / d if d else 1.0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    full_runs = [r for r in results if "worktree_unchanged" in r["checks"]]
    fail_closed = [
        r for r in results
        if r["scenario"].endswith(("fails_closed", "rejected", "suppressed", "not_anchored"))
    ]
    fail_closed_ok = sum(1 for r in fail_closed if r["pass"])
    posted = [r for r in results if r["scenario"] in {
        "valid_anchored_finding_posted", "comment_bodies_redacted", "original_repository_unchanged",
    }]
    duplicates = [r for r in results if r["scenario"] == "duplicate_comment_suppressed"]

    return {
        "total_cases": n,
        "pass_rate": _rate(sum(1 for r in results if r["pass"]), n),
        "contract_metrics": {
            "label": "transport/contract tests (fake transport)",
            "metadata_and_diff_fetch_rate": _rate(
                sum(1 for r in full_runs if r["checks"].get("metadata_fetched") and r["checks"].get("diff_fetched")),
                len(full_runs),
            ),
        },
        "safety_metrics": {
            "label": "fail-closed and redaction tests (fake transport)",
            "fail_closed_rate": _rate(fail_closed_ok, len(fail_closed)),
            "posted_body_secret_free_rate": _rate(
                sum(1 for r in posted if r["checks"].get("body_secret_free")), len(posted)),
            "original_worktree_safety": _rate(
                sum(1 for r in full_runs if r["checks"].get("worktree_unchanged")), len(full_runs)),
            "marker_integrity_rate": _rate(
                sum(1 for r in results if r["checks"].get("markers_valid", True)), n),
        },
        "idempotency_metrics": {
            "label": "idempotency tests (fake transport)",
            "duplicate_suppression_rate": _rate(
                sum(1 for r in duplicates if r["checks"].get("duplicate_suppressed")), len(duplicates)),
        },
        "disclaimer": (
            "All cases run against a deterministic fake GitHub transport; no network calls and "
            "no real GitHub writes occur. The adapter contract is verified, not GitHub itself."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 9A GitHub Integration Evaluation Runner")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "github")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    results = run_github_eval(args.cases)
    summary = summarize(results)

    print("\nPhase 9A GitHub Integration Evaluation Summary:", file=sys.stderr)
    print(f"  Total Cases: {summary['total_cases']}  (pass rate {summary['pass_rate']:.4f})", file=sys.stderr)
    for group in ("contract_metrics", "safety_metrics", "idempotency_metrics"):
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
