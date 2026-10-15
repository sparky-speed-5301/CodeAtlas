"""Measured quality collection with explicit provider attribution and no transcripts."""

from __future__ import annotations

import hashlib
import os
import platform
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.analyzers import AnalysisContext, default_registry
from codeatlas.git.models import ChangeStatus, Diff, FileChange, LineRange
from codeatlas.patching.proposal import audit_patch_redaction
from codeatlas.providers import (
    RepairOutput,
    RepairProviderFailure,
    RepairRequest,
    StructuredRepairProvider,
)
from codeatlas.review.packet import ReviewPacket
from codeatlas.review.provider import ReviewerProvider, ReviewerResult

from .provider_collection import comparison_control_from_request
from .repair import RepairBenchmarkCase, benchmark_candidate
from .repair_metrics import RepairObservation, score_repair_benchmark
from .repository import (
    ObservedFinding,
    RepositoryBenchmarkCase,
    RepositoryBenchmarkSuite,
    ReviewObservation,
    load_benchmark_suite,
    score_review_benchmark,
)

QualityMode = Literal["deterministic", "live", "fallback"]


class QualityAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    case_id: str
    selected_mode: QualityMode
    provider_mode_used: Literal["deterministic", "live", "fallback", "unavailable"]
    status: Literal["completed", "unavailable", "failed", "blocked", "unsupported"]
    reason: str | None = None
    provider_name: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    model_name: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.:/-]{1,128}$")
    context_hash: str | None = None
    request_hash: str | None = None
    response_hash: str | None = None
    duration_ms: float = Field(default=0, ge=0)
    fallback_trigger: str | None = None


def _fixture(suite_path: Path, case: RepositoryBenchmarkCase) -> Path:
    if not case.fixture_ref:
        raise ValueError("offline fixture required")
    root = suite_path.resolve().parent
    fixture = (root / case.fixture_ref).resolve()
    if not fixture.is_relative_to(root):
        raise ValueError("fixture outside root")
    return fixture


def collect_deterministic_reviews(suite_path: str | Path) -> tuple[ReviewObservation, ...]:
    """Run installed deterministic analyzers, not expected labels, on fixture changes."""
    path = Path(suite_path)
    suite = load_benchmark_suite(path)
    observations: list[ReviewObservation] = []
    for case in suite.cases:
        fixture = _fixture(path, case)
        files: list[FileChange] = []
        for target in case.changed_files:
            after = fixture / "after" / target
            if not after.resolve().is_relative_to(fixture) or after.is_symlink():
                raise ValueError("unsafe fixture target")
            count = len(after.read_text(encoding="utf-8").splitlines())
            files.append(FileChange(ChangeStatus.MODIFIED, target, new_ranges=(LineRange(1, count),)))
        started = time.perf_counter()
        found = default_registry().analyze(AnalysisContext(fixture / "after", Diff(case.base_sha, case.head_sha, tuple(files))))
        findings: list[ObservedFinding] = []
        for rank, finding in enumerate(found, 1):
            data = finding.model_dump(mode="json")
            location = data["location"]
            findings.append(ObservedFinding.model_validate({
                "finding_id": data["id"], "file": location["file"],
                "start_line": location["start_line"], "end_line": location["end_line"],
                "category": data["category"], "severity": data["severity"],
                "quality_decision": "report", "evidence": ("deterministic_analyzer_evidence",), "rank": rank,
            }))
        duration = (time.perf_counter() - started) * 1000
        observations.append(ReviewObservation(
            case_id=case.case_id, mode="deterministic", findings=tuple(findings),
            total_review_duration_ms=duration,
            time_to_first_finding_ms=duration if findings else None,
        ))
    return tuple(observations)


def collect_repair_quality(
    case: RepositoryBenchmarkCase,
    request: RepairRequest,
    *,
    mode: QualityMode,
    provider: StructuredRepairProvider,
    allow_live: bool = False,
    network_allowed: bool = False,
    credential_env: str | None = None,
    fallback_provider: StructuredRepairProvider | None = None,
) -> tuple[QualityAttribution, RepairObservation | None]:
    """Generate and statically measure a proposal; never execute, apply, or approve."""
    control = comparison_control_from_request(request, finding_anchor_id=request.finding.id, validation_policy="static-only-v1")
    identity = {
        "case_id": case.case_id, "selected_mode": mode,
        "context_hash": control.context_hash, "request_hash": control.request_hash,
    }

    def unavailable(reason: str) -> tuple[QualityAttribution, None]:
        return QualityAttribution.model_validate({**identity, "provider_mode_used": "unavailable", "status": "unavailable", "reason": reason}), None

    if (case.repository_identity, case.base_sha, case.head_sha) != (
        control.repository_identity, control.base_sha, control.head_sha,
    ) or request.context.changed_file not in case.changed_files:
        return unavailable("context_identity_mismatch")
    if mode == "deterministic" and provider.network_access:
        return unavailable("provider_identity_mismatch")
    if mode != "deterministic":
        if not allow_live:
            return unavailable("live_not_selected")
        if not provider.network_access:
            return unavailable("provider_identity_mismatch")
        if not network_allowed:
            return unavailable("network_disabled")
        if not credential_env or not os.environ.get(credential_env, "").strip():
            return unavailable("missing_credentials")
        config = getattr(provider, "config", None)
        if config is None:
            return unavailable("provider_unavailable")
        timeout = getattr(config, "timeout_seconds", None)
        maximum = getattr(config, "max_response_bytes", None)
        if timeout is None or maximum is None:
            return unavailable("provider_bounds_unavailable")
        if not 0 < timeout <= 300 or not 0 < maximum <= 256_000:
            return unavailable("provider_budget_exceeded")
        if len(request.model_dump_json().encode()) > 128_000:
            return unavailable("request_too_large")
    if mode == "fallback":
        if fallback_provider is None or fallback_provider.network_access:
            return unavailable("fallback_unavailable")
    elif fallback_provider is not None:
        return unavailable("fallback_not_selected")
    effective = provider
    used = "deterministic" if mode == "deterministic" else "live"
    trigger = None
    started = time.perf_counter()
    try:
        try:
            output = RepairOutput.model_validate(provider.generate(request).model_dump(mode="json"))
        except RepairProviderFailure as error:
            if mode != "fallback" or fallback_provider is None:
                return unavailable(error.code)
            trigger = error.code
            effective = fallback_provider
            used = "fallback"
            output = RepairOutput.model_validate(fallback_provider.generate(request).model_dump(mode="json"))
    except RepairProviderFailure as error:
        return unavailable(error.code)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return unavailable("malformed_response")
    if len(output.model_dump_json().encode()) > 256_000:
        return unavailable("response_too_large")
    if not audit_patch_redaction(output.model_dump_json()).safe:
        return unavailable("secret_response")
    measured = benchmark_candidate(RepairBenchmarkCase(
        case_id=case.case_id, language=request.context.language, target_file=request.context.changed_file,
        source=request.context.code_context, changed_line_range=request.context.changed_line_range,
    ), output)
    elapsed = (time.perf_counter() - started) * 1000
    observation = RepairObservation(
        case_id=case.case_id, mode="deterministic" if used in {"deterministic", "fallback"} else "live_provider",
        proposal_eligible=True, proposal_generated=True,
        structured_output_accepted=measured.provider_output_valid, patch_parse_succeeded=measured.patch_parsed,
        target_files=tuple(output.target_files) if measured.patch_parsed else (),
        changed_lines_total=measured.changed_lines,
        changed_lines_in_scope=measured.changed_lines if measured.scope_safe else 0,
        secret_safe=measured.secrets_safe, policy_safe=measured.scope_safe,
        syntax="passed" if measured.syntax_safe else ("failed" if measured.patch_parsed else "not_run"),
        formatter="not_run", linter="not_run", type_check="not_run", targeted_tests="not_run", full_suite="not_run",
        repair_attempts=2 if used == "fallback" else 1, latency_ms=elapsed,
    )
    attribution = QualityAttribution.model_validate({
        **identity, "provider_mode_used": used, "status": "completed", "provider_name": effective.name,
        "model_name": getattr(getattr(effective, "config", None), "model_name", None),
        "response_hash": hashlib.sha256(output.model_dump_json().encode()).hexdigest(),
        "duration_ms": elapsed, "fallback_trigger": trigger,
    })
    return attribution, observation


def collect_live_review_quality(
    case: RepositoryBenchmarkCase,
    packet: ReviewPacket,
    *,
    provider: ReviewerProvider,
    allow_live: bool = False,
    network_allowed: bool = False,
    credential_env: str | None = None,
) -> tuple[QualityAttribution, ReviewObservation | None]:
    """Collect a selected bounded live reviewer result without retaining response text."""
    identity = {"case_id": case.case_id, "selected_mode": "live"}

    def unavailable(reason: str) -> tuple[QualityAttribution, None]:
        return QualityAttribution.model_validate({**identity, "provider_mode_used": "unavailable", "status": "unavailable", "reason": reason}), None

    if not allow_live:
        return unavailable("live_not_selected")
    if not network_allowed:
        return unavailable("network_disabled")
    if not credential_env or not os.environ.get(credential_env, "").strip():
        return unavailable("missing_credentials")
    config = getattr(provider, "config", None)
    if config is None or not getattr(config, "enabled", False) or getattr(config, "dry_run", True):
        return unavailable("provider_unavailable")
    if not 0 < getattr(config, "timeout_seconds", 0) <= 300 or not 0 < getattr(config, "max_response_bytes", 0) <= 256_000:
        return unavailable("provider_budget_exceeded")
    if packet.packet_size_statistics.total_bytes > min(getattr(config, "max_packet_bytes", 0), 128_000):
        return unavailable("request_too_large")
    if (packet.repository, packet.base_commit, packet.head_commit) != (case.repository_identity, case.base_sha, case.head_sha):
        return unavailable("context_identity_mismatch")
    if not set(packet.changed_files).issubset(case.changed_files):
        return unavailable("context_identity_mismatch")
    started = time.perf_counter()
    try:
        result = ReviewerResult.model_validate(provider.review(packet).model_dump(mode="python"))
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return unavailable("provider_unavailable")
    if result.provider_name != provider.name:
        return unavailable("provider_identity_mismatch")
    if result.validation_status in {"failed", "rejected"}:
        known = {"timeout", "rate_limited", "transport_error", "auth_error", "malformed_json_response"}
        reason = next((item for item in (*result.abstentions, *result.limitations) if item in known), "provider_unavailable")
        return unavailable(reason)
    if not audit_patch_redaction(result.model_dump_json()).safe:
        return unavailable("secret_response")
    if result.abstentions and result.findings:
        return unavailable("malformed_response")
    findings: list[ObservedFinding] = []
    try:
        for rank, data in enumerate(result.findings, 1):
            location = data.get("location", data)
            findings.append(ObservedFinding.model_validate({
                "finding_id": data.get("id", f"live-{rank}"), "file": location["file"],
                "start_line": location["start_line"], "end_line": location["end_line"],
                "category": data["category"], "severity": data["severity"],
                "quality_decision": data.get("quality_decision", "review_only"),
                "evidence": ("provider_evidence_present",) if data.get("evidence") else (), "rank": rank,
            }))
    except (KeyError, TypeError, ValueError):
        return unavailable("malformed_response")
    elapsed = (time.perf_counter() - started) * 1000
    usage = result.usage_metadata
    observation = ReviewObservation(
        case_id=case.case_id, mode="live_provider", findings=tuple(findings),
        abstained=bool(result.abstentions), abstention_reason="provider_abstained" if result.abstentions else None,
        total_review_duration_ms=elapsed, time_to_first_finding_ms=elapsed if findings else None,
        provider_attempts=1 + int(usage.get("retries", 0)), retries=int(usage.get("retries", 0)),
        input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
        cost=None,
    )
    return QualityAttribution.model_validate({
        **identity, "provider_mode_used": "live", "status": "completed", "duration_ms": elapsed,
        "provider_name": provider.name, "model_name": config.model_name,
    }), observation


def quality_report(suite: RepositoryBenchmarkSuite, reviews: tuple[ReviewObservation, ...], repairs: tuple[RepairObservation, ...] = ()) -> dict[str, object]:
    return {
        "benchmark_version": suite.benchmark_version, "suite_version": suite.benchmark_version,
        "review_reports": [item.model_dump(mode="json") for item in score_review_benchmark(suite, reviews)],
        "repair_reports": [item.model_dump(mode="json") for item in score_repair_benchmark(suite, repairs)],
        "environment": {"python": platform.python_version(), "platform": platform.system()},
        "tool_availability": {"deterministic_analyzers": True, "test_execution": False},
        "limitations": ["Static collection does not execute tests or establish real live-provider quality.",
                         "Fixture line ranges cover full labelled files; ranking latency is collection-level."],
        "case_population": {population: sum(case.population == population for case in suite.cases) for population in ("positive", "negative", "ambiguous")},
    }


__all__ = ["QualityAttribution", "collect_deterministic_reviews", "collect_live_review_quality", "collect_repair_quality", "quality_report"]
