"""Measured repair quality without executing repository code.

The benchmark distinguishes patch production from patch usefulness.  A
candidate is useful only when it is parseable, scoped, syntax-safe, and (when
the case provides one) produces the expected source.  Repository benchmarking
reads declared files from a real checkout but never invokes a provider or a
command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.adapters import select_language_adapter
from codeatlas.patching.parser import parse_unified_diff, unsafe_diff_path_reason
from codeatlas.patching.proposal import audit_patch_redaction, normalize_diff
from codeatlas.patching.suggestions import preview_patch
from codeatlas.providers import RepairOutput


class RepairBenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=128)
    language: Literal["python", "javascript", "typescript", "go", "java", "rust"]
    target_file: str = Field(min_length=1, max_length=512)
    source: str = Field(min_length=1, max_length=128_000)
    changed_line_range: tuple[int, int] = (1, 1)
    expected_source: str | None = Field(default=None, max_length=128_000)
    expected_behavior: str = Field(default="", max_length=2_000)


class RepairBenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    provider_output_valid: bool = False
    patch_parsed: bool = False
    scope_safe: bool = False
    secrets_safe: bool = False
    syntax_safe: bool = False
    expected_behavior_met: bool | None = None
    useful: bool = False
    reason: str = ""
    changed_lines: int = 0
    patch_bytes: int = 0
    limitations: tuple[str, ...] = ()


class RepairBenchmarkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    useful: int
    safe: int
    parseable: int
    abstained: int
    useful_rate: float
    safety_rate: float
    results: tuple[RepairBenchmarkResult, ...]


def benchmark_candidate(case: RepairBenchmarkCase, candidate: RepairOutput | dict[str, Any] | None) -> RepairBenchmarkResult:
    """Evaluate one candidate using only the same structural patch gates."""
    if candidate is None:
        return RepairBenchmarkResult(case_id=case.case_id, reason="abstained")
    try:
        output = RepairOutput.model_validate(candidate.model_dump(mode="json") if isinstance(candidate, RepairOutput) else candidate)
    except Exception:
        return RepairBenchmarkResult(case_id=case.case_id, reason="schema_failure")
    if output.target_files != [case.target_file]:
        return RepairBenchmarkResult(case_id=case.case_id, provider_output_valid=True, reason="target_scope_invalid")
    diff = normalize_diff(output.patch)
    redaction = audit_patch_redaction(diff + output.diagnosis + output.explanation)
    if not redaction.safe:
        return RepairBenchmarkResult(
            case_id=case.case_id,
            provider_output_valid=True,
            secrets_safe=False,
            patch_bytes=len(diff.encode("utf-8")),
            reason="secret_detected",
        )
    parsed, errors = parse_unified_diff(diff, max_files=1, max_changed_lines=150, max_patch_bytes=64_000)
    if errors or len(parsed) != 1 or parsed[0].path != case.target_file:
        return RepairBenchmarkResult(
            case_id=case.case_id,
            provider_output_valid=True,
            secrets_safe=True,
            patch_bytes=len(diff.encode("utf-8")),
            reason="patch_parse_failed",
        )
    if unsafe_diff_path_reason(diff) or parsed[0].operation != "modify":
        return RepairBenchmarkResult(
            case_id=case.case_id,
            provider_output_valid=True,
            patch_parsed=True,
            secrets_safe=True,
            patch_bytes=len(diff.encode("utf-8")),
            reason="scope_safe_failed",
        )
    start, end = case.changed_line_range
    old_start = min(hunk.old_start for hunk in parsed[0].hunks)
    old_end = max(hunk.old_start + max(hunk.old_lines - 1, 0) for hunk in parsed[0].hunks)
    scope = start <= old_start <= old_end <= end
    if not scope:
        return RepairBenchmarkResult(
            case_id=case.case_id,
            provider_output_valid=True,
            patch_parsed=True,
            scope_safe=False,
            secrets_safe=True,
            patch_bytes=len(diff.encode("utf-8")),
            reason="finding_anchor_missing",
        )
    try:
        preview = preview_patch(case.source, parsed[0])
    except ValueError:
        return RepairBenchmarkResult(
            case_id=case.case_id,
            provider_output_valid=True,
            patch_parsed=True,
            scope_safe=True,
            secrets_safe=True,
            patch_bytes=len(diff.encode("utf-8")),
            reason="patch_conflict",
        )
    adapter = select_language_adapter(case.target_file)
    if adapter is None or adapter.language != case.language:
        return RepairBenchmarkResult(case_id=case.case_id, reason="unsupported_language")
    syntax = adapter.parse_modified_file(case.target_file, preview)
    if syntax.errors or syntax.syntax_valid is False:
        return RepairBenchmarkResult(
            case_id=case.case_id,
            provider_output_valid=True,
            patch_parsed=True,
            scope_safe=True,
            secrets_safe=True,
            syntax_safe=False,
            patch_bytes=len(diff.encode("utf-8")),
            reason="syntax_validation_failed",
        )
    expected = None if case.expected_source is None else preview == case.expected_source
    useful = expected is not False
    return RepairBenchmarkResult(
        case_id=case.case_id,
        provider_output_valid=True,
        patch_parsed=True,
        scope_safe=True,
        secrets_safe=True,
        syntax_safe=True,
        expected_behavior_met=expected,
        useful=useful,
        reason="useful" if useful else "expected_behavior_not_met",
        changed_lines=sum(1 for hunk in parsed[0].hunks for line in hunk.lines if line.startswith(("+", "-"))),
        patch_bytes=len(diff.encode("utf-8")),
        limitations=syntax.limitations,
    )


def _summary(results: list[RepairBenchmarkResult]) -> RepairBenchmarkSummary:
    total = len(results)
    useful = sum(item.useful for item in results)
    safe = sum(item.scope_safe and item.secrets_safe and item.syntax_safe for item in results)
    parseable = sum(item.patch_parsed for item in results)
    abstained = sum(item.reason == "abstained" for item in results)
    return RepairBenchmarkSummary(
        total=total,
        useful=useful,
        safe=safe,
        parseable=parseable,
        abstained=abstained,
        useful_rate=round(useful / total, 4) if total else 0.0,
        safety_rate=round(safe / total, 4) if total else 0.0,
        results=tuple(results),
    )


def run_deterministic_benchmark(
    cases: list[RepairBenchmarkCase],
    candidates: dict[str, RepairOutput | dict[str, Any] | None],
) -> RepairBenchmarkSummary:
    """Run a repeatable fixture benchmark with no provider or filesystem access."""
    return _summary([benchmark_candidate(case, candidates.get(case.case_id)) for case in cases])


def _safe_target(root: Path, target: str) -> Path | None:
    normalized = target.replace("\\", "/")
    if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/")):
        return None
    path = root / normalized
    try:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            return None
    except OSError:
        return None
    return path


def load_repository_cases(repository: str | Path, case_file: str | Path) -> list[RepairBenchmarkCase]:
    """Load declared real-repository cases while reading only target files."""
    root = Path(repository).expanduser().resolve(strict=True)
    data = json.loads(Path(case_file).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("repair benchmark case file must contain a list")
    result: list[RepairBenchmarkCase] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("repair benchmark case must be an object")
        target = str(item.get("target_file", ""))
        path = _safe_target(root, target)
        if path is None:
            raise ValueError("repair benchmark target is outside the repository")
        case_data = dict(item)
        case_data["source"] = path.read_text(encoding="utf-8")
        result.append(RepairBenchmarkCase.model_validate(case_data))
    return result


def run_repository_benchmark(
    repository: str | Path,
    cases: list[RepairBenchmarkCase],
    candidates: dict[str, RepairOutput | dict[str, Any] | None],
) -> RepairBenchmarkSummary:
    """Benchmark candidates against a real checkout without mutating it."""
    root = Path(repository).expanduser().resolve(strict=True)
    checked: list[RepairBenchmarkResult] = []
    for case in cases:
        target = _safe_target(root, case.target_file)
        if target is None:
            checked.append(RepairBenchmarkResult(case_id=case.case_id, reason="target_outside_repository"))
            continue
        try:
            current_case = case.model_copy(update={"source": target.read_text(encoding="utf-8")})
        except (OSError, UnicodeError):
            checked.append(RepairBenchmarkResult(case_id=case.case_id, reason="target_unreadable"))
            continue
        checked.append(benchmark_candidate(current_case, candidates.get(case.case_id)))
    return _summary(checked)


__all__ = [
    "RepairBenchmarkCase",
    "RepairBenchmarkResult",
    "RepairBenchmarkSummary",
    "benchmark_candidate",
    "load_repository_cases",
    "run_deterministic_benchmark",
    "run_repository_benchmark",
]
