"""Regenerate the sanitized offline repository benchmark contract fixture."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from codeatlas.benchmark import (
    ExpectedAnchor,
    ObservedFinding,
    PatchScopeExpectation,
    ProviderComparisonControl,
    ProviderComparisonObservation,
    ProviderPatchLimits,
    RepairObservation,
    RepositoryBenchmarkCase,
    RepositoryBenchmarkSuite,
    ReviewObservation,
    TestCommandPolicy,
)

ROOT = Path(__file__).parent / "cases" / "real-repositories" / "v1"
VERSION = "real-repositories-v1"

BASE_FILES = {
    "clean/readme.py": "def label(value: str) -> str:\n    return value.strip()\n",
    "bugs/page.py": "def page(offset: int, size: int) -> int:\n    return offset + size\n",
    "security/path.py": "from pathlib import Path\n\ndef load(root: Path, name: str) -> str:\n    return (root / name).read_text()\n",
    "sensitive/log.py": "def audit(logger, email: str) -> None:\n    logger.info('request accepted')\n",
    "auth/guard.py": "def allowed(user) -> bool:\n    return user.is_admin\n",
    "types/name.py": "def display(name: str | None) -> str:\n    return name or ''\n",
    "tests/math.py": "def total(values: list[int]) -> int:\n    return sum(values)\n",
    "performance/search.py": "def present(items, target):\n    return target in set(items)\n",
    "ambiguous/policy.py": "def threshold(config):\n    return config['limit']\n",
    "abstain/intent.py": "def mode(config):\n    return config.get('mode', 'safe')\n",
    "ai/generated.py": "def ratio(total: int, count: int) -> float:\n    return total / count\n",
    "multi/service.py": "def encode(value: str) -> str:\n    return value\n",
    "multi/client.ts": "export const decode = (value: string): string => value;\n",
}

HEAD_FILES = {
    **BASE_FILES,
    "clean/readme.py": "def label(value: str) -> str:\n    return value.strip().lower()\n",
    "bugs/page.py": "def page(offset: int, size: int) -> int:\n    return offset * size\n",
    "security/path.py": "from pathlib import Path\n\ndef load(root: Path, name: str) -> str:\n    return Path(name).read_text()\n",
    "sensitive/log.py": "def audit(logger, email: str) -> None:\n    logger.info('request for %s', email)\n",
    "auth/guard.py": "def allowed(user) -> bool:\n    return bool(user)\n",
    "types/name.py": "def display(name: str | None) -> str:\n    return name.upper()\n",
    "tests/math.py": "def total(values: list[int]) -> int:\n    return sum(values[1:])\n",
    "performance/search.py": "def present(items, target):\n    return target in list(items)\n",
    "ambiguous/policy.py": "def threshold(config):\n    return config.get('limit', 0)\n",
    "abstain/intent.py": "def mode(config):\n    return config.get('mode', 'fast')\n",
    "ai/generated.py": "def ratio(total: int, count: int) -> float:\n    return total / (count - count)\n",
    "multi/service.py": "def encode(value: str) -> str:\n    return value.encode().hex()\n",
    "multi/client.ts": "export const decode = (value: string): string => value;\n",
}

CASE_DATA = (
    ("clean-change", "clean_changes", "negative", ("python",), ("clean/readme.py",), None, "not_fixable", "forbidden"),
    ("real-bug", "real_bugs", "positive", ("python",), ("bugs/page.py",), ("CORRECTNESS", "high", 2), "fixable", "forbidden"),
    ("security-issue", "security_issues", "positive", ("python",), ("security/path.py",), ("PATH_TRAVERSAL", "high", 4), "fixable", "forbidden"),
    ("sensitive-data", "sensitive_data_exposure", "positive", ("python",), ("sensitive/log.py",), ("SENSITIVE_DATA_EXPOSURE", "high", 2), "fixable", "forbidden"),
    ("authorization", "authorization_authentication", "positive", ("python",), ("auth/guard.py",), ("AUTHORIZATION", "blocker", 2), "fixable", "forbidden"),
    ("nullability", "type_nullability", "positive", ("python",), ("types/name.py",), ("NULLABILITY", "medium", 2), "fixable", "forbidden"),
    ("test-regression", "test_regressions", "positive", ("python",), ("tests/math.py",), ("TEST_REGRESSION", "high", 2), "fixable", "forbidden"),
    ("performance", "performance_regressions", "positive", ("python",), ("performance/search.py",), ("PERFORMANCE", "medium", 2), "fixable", "forbidden"),
    ("ambiguous", "ambiguous_changes", "ambiguous", ("python",), ("ambiguous/policy.py",), None, "ambiguous", "allowed"),
    ("correct-abstention", "correct_abstention", "ambiguous", ("python",), ("abstain/intent.py",), None, "ambiguous", "required"),
    ("ai-defect", "ai_generated_defects", "positive", ("python",), ("ai/generated.py",), ("CORRECTNESS", "high", 2), "fixable", "forbidden"),
    ("multi-language", "multi_language_changes", "positive", ("python", "typescript"), ("multi/service.py", "multi/client.ts"), ("CONTRACT_MISMATCH", "high", 2), "review_required", "forbidden"),
)


def _git(repository: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "CodeAtlas Benchmark",
        "GIT_AUTHOR_EMAIL": "benchmark@example.invalid",
        "GIT_COMMITTER_NAME": "CodeAtlas Benchmark",
        "GIT_COMMITTER_EMAIL": "benchmark@example.invalid",
        "GIT_AUTHOR_DATE": "2025-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2025-01-01T00:00:00Z",
    }
    return subprocess.check_output(["git", *args], cwd=repository, env=env, text=True).strip()


def _write_snapshot(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def generate() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    fixture = ROOT / "fixtures" / "sanitized-offline-mirror"
    with tempfile.TemporaryDirectory(prefix="codeatlas-real-benchmark-") as temporary:
        repository = Path(temporary) / "repository"
        repository.mkdir()
        _git(repository, "init", "-q")
        _git(repository, "config", "core.autocrlf", "false")
        _write_snapshot(repository, BASE_FILES)
        _git(repository, "add", ".")
        _git(repository, "commit", "-qm", "sanitized benchmark base")
        base_sha = _git(repository, "rev-parse", "HEAD")
        for path in repository.iterdir():
            if path.name != ".git":
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        _write_snapshot(repository, HEAD_FILES)
        _git(repository, "add", "-A")
        _git(repository, "commit", "-qm", "sanitized benchmark head")
        head_sha = _git(repository, "rev-parse", "HEAD")
        replacement = Path(temporary) / "fixture"
        _write_snapshot(replacement / "before", BASE_FILES)
        _write_snapshot(replacement / "after", HEAD_FILES)
        _git(repository, "bundle", "create", str(replacement / "mirror.bundle"), "--all")
        (replacement / "revisions.json").write_text(
            json.dumps({"base_sha": base_sha, "head_sha": head_sha}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
        if fixture.exists():
            shutil.rmtree(fixture)
        fixture.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(replacement, fixture)

    cases = []
    for case_id, category, population, languages, files, expected, fixability, abstention in CASE_DATA:
        anchors = ()
        if expected:
            finding_category, severity, line = expected
            anchors = (ExpectedAnchor(
                anchor_id=f"{case_id}-1", file=files[0], start_line=line, end_line=line,
                category=finding_category, severity=severity, fixability=fixability,
            ),)
        cases.append(RepositoryBenchmarkCase(
            case_id=case_id,
            fixture_ref="fixtures/sanitized-offline-mirror",
            repository_identity="codeatlas/sanitized-offline-benchmark-fixture",
            base_sha=base_sha,
            head_sha=head_sha,
            language=languages,
            changed_files=files,
            benchmark_category=category,
            population=population,
            expected_finding_categories=tuple(anchor.category for anchor in anchors),
            expected_finding_anchors=anchors,
            expected_severity=tuple(anchor.severity for anchor in anchors),
            expected_abstention=abstention,
            expected_fixability=fixability,
            expected_proposal_eligible=fixability in {"fixable", "review_required"},
            expected_patch_scope=PatchScopeExpectation(allowed_files=files, max_files=len(files)),
            expected_test_command_policy=TestCommandPolicy(policy="targeted_only", allowed_commands=("pytest -q",)),
            known_limitations=("Sanitized contract fixture; it is not evidence of live-provider quality.",),
            benchmark_version=VERSION,
        ))
    suite = RepositoryBenchmarkSuite(benchmark_version=VERSION, cases=tuple(cases))
    (ROOT / "suite.json").write_text(suite.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    review_observations = []
    repair_observations = []
    for case in suite.cases:
        findings = tuple(
            ObservedFinding(
                finding_id=f"CA-BENCH-{index}", file=anchor.file,
                start_line=anchor.start_line, end_line=anchor.end_line,
                category=anchor.category, severity=anchor.severity,
                quality_decision=anchor.quality_decision,
                evidence=("Sanitized expected observation.",), rank=index,
            )
            for index, anchor in enumerate(case.expected_finding_anchors, start=1)
        )
        review_observations.append(ReviewObservation(
            case_id=case.case_id, mode="deterministic", findings=findings,
            abstained=case.expected_abstention == "required",
            abstention_reason="Repository evidence does not establish intent." if case.expected_abstention == "required" else None,
            time_to_first_finding_ms=0 if findings else None,
            total_review_duration_ms=0,
        ))
        repair_observations.append(RepairObservation(
            case_id=case.case_id, mode="deterministic",
            proposal_eligible=case.expected_proposal_eligible,
            proposal_generated=False, structured_output_accepted=False,
            patch_parse_succeeded=False, secret_safe=True, policy_safe=True,
            syntax="not_run", formatter="not_run", linter="not_run",
            type_check="not_run", targeted_tests="not_run", full_suite="not_run",
        ))
    (ROOT / "example-review-observations.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in review_observations], indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    (ROOT / "example-repair-observations.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in repair_observations], indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    comparison_case = next(case for case in suite.cases if case.case_id == "real-bug")
    comparison_control = ProviderComparisonControl(
        repository_identity=comparison_case.repository_identity,
        base_sha=comparison_case.base_sha,
        head_sha=comparison_case.head_sha,
        finding_anchor_id=comparison_case.expected_finding_anchors[0].anchor_id,
        context_hash="a" * 64,
        request_hash="b" * 64,
        policy_version="11C-A.1",
        patch_limits=ProviderPatchLimits(max_target_files=1, max_patch_lines=150, max_patch_bytes=64_000),
        validation_policy="static-only-v1",
        test_command_policy=comparison_case.expected_test_command_policy.policy,
        language=comparison_case.language,
        target_files=comparison_case.expected_patch_scope.allowed_files,
    )
    comparison_rows = []
    for mode in ("offline", "live", "fallback", "dry_run"):
        is_dry = mode == "dry_run"
        is_fallback = mode == "fallback"
        comparison_rows.append(ProviderComparisonObservation(
            comparison_id="real-bug-provider-matrix", case_id=comparison_case.case_id,
            run_mode=mode, provider_name="offline" if mode in {"offline", "fallback"} else "openai_compatible",
            model_name=None if mode in {"offline", "fallback"} else "safe-model-v1",
            provider_mode="offline" if mode in {"offline", "fallback"} else "online",
            provider_contacted=mode in {"live", "fallback"}, fallback_used=is_fallback,
            fallback_trigger_code="timeout" if is_fallback else None, control=comparison_control,
            request_size_bytes=0 if is_dry else 1000, response_size_bytes=0 if is_dry else 500,
            duration_ms=0 if is_dry else 20, outcome="not_run" if is_dry else "completed",
            patch_hash=None if is_dry else "c" * 64,
            validation_status="not_run" if is_dry else "static_validation_passed",
            syntax="not_run" if is_dry else "passed",
        ))
    (ROOT / "example-provider-comparisons.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in comparison_rows], indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )


if __name__ == "__main__":
    generate()
