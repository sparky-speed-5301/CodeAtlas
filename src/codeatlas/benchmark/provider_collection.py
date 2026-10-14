"""Explicit provider execution for comparisons; retains safe metadata only."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Literal

from codeatlas.orchestrator.repair_models import compute_context_hash
from codeatlas.patching.proposal import normalize_diff
from codeatlas.providers import (
    ProviderError,
    RepairOutput,
    RepairProviderFailure,
    RepairRequest,
    StructuredRepairProvider,
)

from .provider_comparison import (
    ProviderComparisonControl,
    ProviderComparisonObservation,
    ProviderPatchLimits,
    ProviderRunMode,
)


def comparison_control_from_request(
    request: RepairRequest,
    *,
    finding_anchor_id: str,
    validation_policy: str,
) -> ProviderComparisonControl:
    """Project validated request identity without retaining its source or prompt."""
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return ProviderComparisonControl(
        repository_identity=request.context.repository_identity,
        base_sha=request.context.base_commit,
        head_sha=request.context.head_commit,
        finding_anchor_id=finding_anchor_id,
        context_hash=request.context.context_hash or compute_context_hash(request.context),
        request_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        policy_version=request.policy.policy_version,
        patch_limits=ProviderPatchLimits(
            max_target_files=request.policy.max_target_files,
            max_patch_lines=request.policy.max_patch_lines,
            max_patch_bytes=request.policy.max_patch_bytes,
        ),
        validation_policy=validation_policy,
        language=(request.context.language,),
        target_files=(request.context.changed_file,),
    )


def _generate_safe(provider: StructuredRepairProvider, request: RepairRequest) -> RepairOutput:
    try:
        value = provider.generate(request)
    except RepairProviderFailure:
        raise
    except (ProviderError, RuntimeError, OSError, ValueError, TypeError, AttributeError):
        raise RepairProviderFailure("provider_unavailable") from None
    try:
        return RepairOutput.model_validate(value.model_dump(mode="json"))
    except (AttributeError, TypeError, ValueError):
        raise RepairProviderFailure("schema_failure") from None


def collect_provider_comparison(
    request: RepairRequest,
    *,
    comparison_id: str,
    case_id: str,
    finding_anchor_id: str,
    validation_policy: str,
    run_mode: ProviderRunMode,
    provider: StructuredRepairProvider,
    fallback_provider: StructuredRepairProvider | None = None,
    allow_live: bool = False,
) -> ProviderComparisonObservation:
    """Generate through an explicit provider boundary, without apply or execution.

    Responses are revalidated and only normalized patch hashes and byte counts
    survive. Validation and test statuses remain not_run; generation proves no fix.
    """
    request_data = request.model_dump(mode="python")
    if not request.context.context_hash:
        request_data["context"]["context_hash"] = compute_context_hash(request.context)
    try:
        request = RepairRequest.model_validate(request_data)
    except (TypeError, ValueError):
        raise ValueError("provider comparison request failed safety validation") from None
    control = comparison_control_from_request(
        request, finding_anchor_id=finding_anchor_id, validation_policy=validation_policy,
    )
    if run_mode not in {"offline", "live", "fallback", "dry_run"}:
        raise ValueError("unsupported provider comparison run mode")
    network = bool(provider.network_access)
    if run_mode == "offline" and network:
        raise ValueError("offline comparison requires a non-network provider")
    if run_mode in {"live", "fallback"} and not network:
        raise ValueError("live and fallback comparisons require an online primary provider")
    if run_mode in {"live", "fallback"} and not allow_live:
        raise ValueError("live provider collection requires explicit allow_live")
    if run_mode == "fallback" and (fallback_provider is None or fallback_provider.network_access):
        raise ValueError("fallback comparison requires an explicit offline fallback provider")
    if run_mode != "fallback" and fallback_provider is not None:
        raise ValueError("fallback provider is permitted only in fallback mode")

    if fallback_provider is not None:
        ProviderComparisonObservation.model_validate({
            "comparison_id": comparison_id, "case_id": case_id,
            "run_mode": "dry_run", "provider_name": fallback_provider.name,
            "model_name": getattr(getattr(fallback_provider, "config", None), "model_name", None),
            "provider_mode": "offline", "provider_contacted": False, "control": control,
            "request_size_bytes": 0, "response_size_bytes": 0,
            "duration_ms": 0, "outcome": "not_run",
        })
    initial_config = getattr(provider, "config", None)
    ProviderComparisonObservation.model_validate({
        "comparison_id": comparison_id, "case_id": case_id,
        "run_mode": "dry_run", "provider_name": provider.name,
        "model_name": getattr(initial_config, "model_name", None),
        "provider_mode": "online" if network else "offline",
        "provider_contacted": False, "control": control,
        "request_size_bytes": 0, "response_size_bytes": 0,
        "duration_ms": 0, "outcome": "not_run",
    })
    started = time.perf_counter()
    effective = provider
    provider_contacted = False
    fallback_used = False
    fallback_trigger: str | None = None
    failure_code: str | None = None
    output: RepairOutput | None = None
    outcome: Literal["completed", "failed", "abstained", "not_run"] = "not_run"
    if run_mode != "dry_run":
        provider_contacted = network
        try:
            output = _generate_safe(provider, request)
        except RepairProviderFailure as error:
            primary_code = error.code
            if run_mode == "fallback" and fallback_provider is not None:
                effective = fallback_provider
                fallback_used = True
                fallback_trigger = primary_code
                try:
                    output = _generate_safe(fallback_provider, request)
                except RepairProviderFailure as fallback_error:
                    failure_code = fallback_error.code
            else:
                failure_code = primary_code
        if output is not None:
            try:
                output = RepairOutput.model_validate(output.model_dump(mode="json"))
                if output.target_files != list(control.target_files):
                    raise RepairProviderFailure("schema_failure")
            except (AttributeError, TypeError, ValueError, RepairProviderFailure):
                output = None
                failure_code = "schema_failure"
        outcome = "completed" if output is not None else "failed"

    # Fallback mode is an observed route, not an instruction to fabricate failure.
    effective_mode: ProviderRunMode = "fallback" if fallback_used else ("live" if run_mode == "fallback" else run_mode)
    config = getattr(effective, "config", None)
    model_name = getattr(config, "model_name", None)
    metadata = {
        "comparison_id": comparison_id,
        "case_id": case_id,
        "run_mode": effective_mode,
        "provider_name": effective.name,
        "model_name": model_name,
        "provider_mode": "online" if effective.network_access else "offline",
        "provider_contacted": provider_contacted,
        "fallback_used": fallback_used,
        "fallback_trigger_code": fallback_trigger,
        "control": control,
        "request_size_bytes": len(request.model_dump_json().encode("utf-8")),
        "response_size_bytes": len(output.model_dump_json().encode("utf-8")) if output is not None else 0,
        "duration_ms": (time.perf_counter() - started) * 1000,
        "outcome": outcome,
        "failure_code": failure_code,
        "patch_hash": hashlib.sha256(normalize_diff(output.patch).encode("utf-8")).hexdigest() if output is not None else None,
    }
    return ProviderComparisonObservation.model_validate(metadata)


__all__ = ["collect_provider_comparison", "comparison_control_from_request"]
