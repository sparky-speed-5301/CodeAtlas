"""Local review orchestration and deterministic analyzers.

This module gathers repository and diff metadata and produces deterministic
findings.  It never executes repository code, runs tests, applies patches, or
merges anything.  Contacting an external LLM happens only through the
explicitly opt-in live provider path (``--review-provider live``), which is
read-only review mode: the provider receives a bounded, redacted packet and
its output passes the same validation and policy gates as any other reviewer.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codeatlas.core import detect_language
from codeatlas.evidence import EvidenceLogger
from codeatlas.findings import Finding, render_markdown
from codeatlas.git.diff import extract_diff
from codeatlas.git.models import Diff
from codeatlas.git.refs import resolve_base_head
from codeatlas.git.repository import validate_repository
from codeatlas.git.snapshot import temporary_snapshot
from codeatlas.orchestrator.manifest import RunManifest
from codeatlas.providers import (
    LiveReviewer,
    ProviderConfigError,
    ProviderTransport,
    load_provider_config,
)
from codeatlas.patching.models import PatchStatus
from codeatlas.patching.suggestions import materialize_patch_suggestions
from codeatlas.analyzers import AnalysisContext, default_registry
from codeatlas.analyzers.secrets import HardcodedSecretAnalyzer
from codeatlas.analyzers.sensitive import SensitiveDataExposureAnalyzer
from codeatlas.repository import (
    build_repository_index,
    identify_changed_symbols,
    retrieve_context,
)
from codeatlas.review import (
    MockReviewer,
    assemble_review_packet as _assemble_review_packet,
    evaluate_policy,
    merge_and_rank_findings,
    assess_finding,
    quality_summary,
    validate_provider_output,
)


@dataclass(frozen=True)
class ReviewResult:
    manifest: RunManifest
    diff: Diff
    evidence_path: Path

    @property
    def summary(self) -> str:
        status = "completed" if not self.manifest.errors else "completed with errors"
        return (
            f"CodeAtlas review {status}: {len(self.diff.files)} changed file(s), "
            f"{len(self.manifest.findings)} finding(s), "
            f"languages={', '.join(self.manifest.detected_languages) or 'none'}"
        )


def _write_text(path: str | Path, content: str) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="\n")


def _markdown(manifest: RunManifest, diff: Diff) -> str:
    lines = [
        "# CodeAtlas Review",
        "",
        f"- **Repository:** `{manifest.repository}`",
        f"- **Base:** `{manifest.base_ref}` ({manifest.base_commit or 'unresolved'})",
        f"- **Head:** `{manifest.head_ref}` ({manifest.head_commit or 'unresolved'})",
        f"- **Changed files:** {len(diff.files)}",
        f"- **Detected languages:** {', '.join(manifest.detected_languages) or 'none'}",
    ]
    if manifest.review_packet_id:
        lines.extend([
            f"- **Packet ID:** `{manifest.review_packet_id}` ({manifest.packet_bytes} bytes, {manifest.packet_lines} lines)",
            f"- **Packet Truncated:** `{manifest.packet_truncated}`",
        ])
    if manifest.policy_decisions:
        dec = manifest.policy_decisions[0].get("decision", "unknown")
        lines.append(f"- **Policy Decision:** `{dec}`")
    lines.extend([
        "",
        "## Changes",
        "",
    ])
    if not diff.files:
        lines.append("No changes detected between the selected commits.")
    else:
        for change in diff.files:
            old = f" (from `{change.old_path}`)" if change.old_path else ""
            lines.append(f"- `{change.status.value}` `{change.path}`{old}")
    lines.extend(["", "## Findings", "", render_markdown(manifest.findings).split("\n", 1)[-1]])
    if manifest.errors:
        lines.extend(["", "## Errors", "", *[f"- {error}" for error in manifest.errors]])
    return "\n".join(lines).rstrip() + "\n"


def _config(path: Path) -> dict:
    """Parse the small, intentionally limited .codeatlas.yml subset."""
    return load_config(path)


def load_config(path: Path) -> dict:
    """Parse the small, intentionally limited .codeatlas.yml subset."""
    if not path.is_file():
        return {}
    try:
        import yaml
        content = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    result: dict = {}
    section: dict | None = None
    current_list_key: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        text = line.strip()
        if indent == 0 and text.endswith(":"):
            section = {}
            result[text[:-1].strip()] = section
            current_list_key = None
            continue
        if section is not None:
            if text.startswith("- ") and current_list_key is not None:
                item_val = text[2:].strip().strip("'\"")
                section[current_list_key].append(item_val)
                continue
            if ":" in text:
                key, value = (part.strip() for part in text.split(":", 1))
                if not value:
                    section[key] = []
                    current_list_key = key
                    continue
                current_list_key = None
                if value.startswith("[") and value.endswith("]"):
                    value = [x.strip().strip("'\"") for x in value[1:-1].split(",") if x.strip()]
                elif value.lower() in {"true", "false"}:
                    value = value.lower() == "true"
                else:
                    value = value.strip("'\"")
                    try:
                        value = float(value) if "." in value else int(value)
                    except ValueError:
                        pass
                section[key] = value
    return result


def _live_review_banner(
    packet: Any,
    provider_name: str,
    model_name: str,
    allow_patch_suggestions: bool = False,
) -> str:
    """Build the fixed pre-request banner.  Contains only safe, bounded fields."""
    parts = [p for p in str(packet.repository).replace("\\", "/").split("/") if p]
    repo_id = parts[-1] if parts else "repository"
    patch_line = (
        "Patches: draft-suggestions-only (never applied)"
        if allow_patch_suggestions
        else "Patches: disabled"
    )
    return "\n".join([
        "CodeAtlas live review",
        f"Provider: {provider_name}",
        f"Model: {model_name}",
        f"Repository: {repo_id}",
        f"Changed files: {len(packet.changed_files)}",
        f"Packet bytes: {packet.packet_size_statistics.total_bytes}",
        "Execution: review-only",
        patch_line,
    ])


def _provider_overrides(
    review_opts: dict[str, Any],
    provider_model: str | None,
    provider_timeout: float | None,
    provider_max_output_tokens: int | None,
) -> dict[str, Any]:
    """Merge CLI flags over the optional non-secret ``review.provider`` config section."""
    section = review_opts.get("provider", {})
    if not isinstance(section, dict):
        raise ValueError("review.provider must be a mapping of provider settings")
    return {
        "model_name": provider_model if provider_model is not None else section.get("model_name"),
        "timeout_seconds": provider_timeout if provider_timeout is not None else section.get("timeout_seconds"),
        "max_output_tokens": (
            provider_max_output_tokens if provider_max_output_tokens is not None else section.get("max_output_tokens")
        ),
        "api_base_url": section.get("api_base_url"),
        "api_key_env_var": section.get("api_key_env_var"),
        "privacy_mode": section.get("privacy_mode"),
        "request_budget": section.get("request_budget"),
        "temperature": section.get("temperature"),
    }


def _run_live_provider(
    *,
    packet: Any,
    manifest: RunManifest,
    evidence: EvidenceLogger,
    cfg: dict[str, Any],
    review_opts: dict[str, Any],
    policy_decision: Any,
    provider_model: str | None,
    provider_config_path: str | Path | None,
    provider_timeout: float | None,
    provider_max_output_tokens: int | None,
    provider_dry_run: bool,
    provider_transport: ProviderTransport | None,
    banner_callback: Callable[[str], None] | None,
    allow_patch_suggestions: bool = False,
    snapshot_path: Path | None = None,
    run_id: str = "",
) -> None:
    """Execute the opt-in live provider path with all Phase 7A safety gates.

    Pre-provider: policy state, config validation, redaction audit, packet
    size, and truncation-abstention checks (the reviewer re-checks redaction,
    size, budget, and disabled state as defense in depth).  No request is
    made unless every gate passes.
    """
    # Recorded isolation assertions: Phase 7A never enables patching or execution.
    manifest.provider_safety_events.append("patches_disabled")
    manifest.provider_safety_events.append("repository_execution_disabled")

    if policy_decision.decision == "abstain":
        manifest.provider_safety_events.append("policy_abstention_no_request")
        evidence.emit("live_provider_blocked", provider="live", reason="policy_abstention", status="blocked")
        manifest.errors.append("Live provider skipped: policy decision is abstain (required context truncated)")
        return

    try:
        overrides = _provider_overrides(review_opts, provider_model, provider_timeout, provider_max_output_tokens)
        live_cfg = load_provider_config(
            provider_config_path,
            overrides=overrides,
            enabled=True,
            dry_run=provider_dry_run,
        )
    except (ProviderConfigError, ValueError) as err:
        manifest.live_provider_enabled = False
        manifest.errors.append(f"Live provider configuration error: {err}")
        evidence.emit("live_provider_blocked", provider="live", reason="config_invalid", status="blocked")
        return

    manifest.live_provider_enabled = True
    manifest.model_name = live_cfg.model_name
    manifest.configuration["live_provider"] = live_cfg.to_safe_dict()
    evidence.emit(
        "live_provider_configured",
        provider="live",
        model=live_cfg.model_name,
        dry_run=live_cfg.dry_run,
        budget=live_cfg.request_budget,
        status="configured",
    )

    if banner_callback is not None:
        banner_callback(
            _live_review_banner(packet, "live", live_cfg.model_name, allow_patch_suggestions)
        )

    evidence.emit(
        "live_provider_request_started",
        provider="live",
        model=live_cfg.model_name,
        packet_id=packet.packet_id,
        dry_run=live_cfg.dry_run,
        status="started",
    )

    reviewer = (
        LiveReviewer(
            config=live_cfg,
            transport=provider_transport,
            allow_patch_suggestions=allow_patch_suggestions,
        )
        if provider_transport is not None
        else LiveReviewer(config=live_cfg, allow_patch_suggestions=allow_patch_suggestions)
    )
    provider_res = reviewer.review(packet)

    manifest.provider_version = provider_res.provider_version
    manifest.provider_usage = dict(provider_res.usage_metadata)
    usage = provider_res.usage_metadata
    manifest.provider_cost = usage.get("estimated_cost")
    manifest.provider_request_id = usage.get("request_id")
    manifest.provider_latency_ms = usage.get("latency_ms")
    manifest.provider_retries = usage.get("retries")
    manifest.provider_abstentions = list(provider_res.abstentions)
    manifest.provider_safety_events.extend(reviewer.safety_events)

    request_failed = provider_res.validation_status in {"failed", "abstained", "not_requested"}
    evidence.emit(
        "live_provider_request_failed" if request_failed else "live_provider_request_completed",
        provider="live",
        model=live_cfg.model_name,
        status=provider_res.validation_status,
        dry_run=live_cfg.dry_run,
        latency_ms=usage.get("latency_ms"),
        retries=usage.get("retries"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        estimated_cost=usage.get("estimated_cost"),
        request_id=usage.get("request_id"),
    )

    # Post-provider gates: schema, paths, line ranges, diff anchoring, fabricated
    # tests, duplicate IDs, and raw secrets via the shared output validator.
    val_res = validate_provider_output(packet, provider_res.findings)
    if provider_res.validation_status in {"not_requested", "abstained"}:
        # No provider output was produced, so there is nothing to judge.
        manifest.provider_output_valid = None
    else:
        manifest.provider_output_valid = val_res.is_valid and provider_res.validation_status not in {"rejected", "failed"}
    manifest.provider_validation_errors = list(provider_res.validation_errors) + list(val_res.validation_errors)

    rejected_count = len(provider_res.validation_errors) + len(val_res.validation_errors)
    if rejected_count:
        evidence.emit("live_provider_output_rejected", count=rejected_count, provider="live", status="rejected")
    sanitized_count = sum(
        1
        for event in reviewer.safety_events
        if event.startswith("status_rewrite") or event in {"evidence_strength_clamped", "fixability_forced"}
    )
    if sanitized_count:
        evidence.emit("live_provider_output_sanitized", count=sanitized_count, provider="live", status="sanitized")

    if provider_res.validation_status in {"rejected", "failed"}:
        manifest.errors.append(
            f"Live provider output rejected: {'; '.join(provider_res.validation_errors) or provider_res.validation_status}"
        )
    elif provider_res.validation_status in {"abstained"}:
        manifest.errors.append(
            f"Live provider abstained before review: {'; '.join(provider_res.limitations) or provider_res.summary}"
        )
    else:
        # Invalid findings are excluded; valid ones may still merge.
        merged = merge_and_rank_findings(manifest.findings, val_res.valid_findings, packet=packet)
        manifest.merged_findings = [f.model_dump(mode="json", exclude_none=True) for f in merged]
        manifest.findings = manifest.merged_findings
        manifest.quality_summary = quality_summary(merged)
        manifest.quality_limitations = manifest.quality_summary.get("limitations", [])[:20]
        manifest.quality_decisions = [f.quality_decision for f in merged]
        evidence.emit("findings_merged", count=len(merged), provider="live", status="ok")

        # Phase 7B: opt-in materialization of provider patch suggestions.
        # The standing assertion: automatic application is never attempted.
        if allow_patch_suggestions:
            manifest.automatic_application_attempted = False
            manifest.automatic_application_blocked = True
            if provider_res.patch_suggestions:
                if snapshot_path is None:
                    manifest.errors.append("Patch suggestions requested without a repository snapshot; skipped")
                else:
                    _materialize_provider_patches(
                        packet=packet,
                        manifest=manifest,
                        evidence=evidence,
                        suggestions=provider_res.patch_suggestions,
                        snapshot_path=snapshot_path,
                        base_commit=manifest.head_commit or "",
                        provider_name="live",
                        provider_version=provider_res.provider_version,
                        model_name=live_cfg.model_name,
                        run_id=run_id,
                        cfg=cfg,
                    )

    evidence.emit(
        "live_review_completed",
        provider="live",
        status="failed" if provider_res.validation_status in {"rejected", "failed"} else "ok",
    )


def _materialize_provider_patches(
    *,
    packet: Any,
    manifest: RunManifest,
    evidence: EvidenceLogger,
    suggestions: list[dict[str, Any]],
    snapshot_path: Path,
    base_commit: str,
    provider_name: str,
    provider_version: str,
    model_name: str,
    run_id: str,
    cfg: dict[str, Any],
) -> None:
    """Run provider suggestions through the Phase 6 patch machinery (never applies)."""
    evidence.emit("patch_auto_apply_blocked", provider=provider_name, operation="none", status="blocked")

    for suggestion in suggestions:
        evidence.emit(
            "patch_suggestion_received",
            suggestion_id=str(suggestion.get("suggestion_id", "unknown")),
            finding_id=str(suggestion.get("finding_id", "")),
            risk_level=str(suggestion.get("risk_level", "")),
            provider=provider_name,
        )

    materialization = materialize_patch_suggestions(
        suggestions,
        packet=packet,
        findings=manifest.findings,
        snapshot_path=snapshot_path,
        base_commit=base_commit,
        provider_name=provider_name,
        provider_version=provider_version,
        model_name=model_name,
        run_id=run_id,
        config=cfg,
    )

    rejected_ids = {r.suggestion_id for r in materialization.rejections}
    accepted_ids = {p.provenance.get("suggestion_id") for p in materialization.proposals}

    for rejection in materialization.rejections:
        manifest.patch_rejection_reasons.append(
            f"{rejection.suggestion_id}: {rejection.reason}"
        )
        evidence.emit(
            "patch_suggestion_rejected",
            suggestion_id=rejection.suggestion_id,
            reason=rejection.reason,
            status="rejected",
        )
        if "secret" in rejection.reason.lower():
            evidence.emit(
                "patch_proposal_redaction_failed",
                suggestion_id=rejection.suggestion_id,
                reason=rejection.reason,
                status="redaction_failed",
            )

    for proposal, validation in zip(
        materialization.proposals, materialization.validation_summaries, strict=False
    ):
        manifest.patch_proposals.append(proposal.model_dump(mode="json", exclude_none=True))
        manifest.patch_proposal_ids.append(proposal.proposal_id)
        manifest.patch_proposal_statuses.append(proposal.status)
        decision = proposal.policy_decision or {}
        manifest.patch_policy_decisions.append(decision)
        evidence.emit(
            "patch_proposal_created",
            proposal_id=proposal.proposal_id,
            finding_id=proposal.finding_id,
            provider=provider_name,
            status=proposal.status,
        )
        evidence.emit(
            "patch_proposal_policy_evaluated",
            proposal_id=proposal.proposal_id,
            decision=str(decision.get("decision", "unknown")),
            valid=validation.get("valid", False),
        )
        if proposal.status == PatchStatus.REQUIRES_HUMAN_APPROVAL:
            evidence.emit(
                "patch_proposal_approval_required",
                proposal_id=proposal.proposal_id,
                status="requires_human_approval",
            )

    manifest.patch_suggestions_received = len(suggestions)
    manifest.patch_suggestions_accepted = materialization.accepted_count
    manifest.patch_suggestions_rejected = materialization.rejected_count
    manifest.patch_suggestion_redaction_audit = {
        "safe": materialization.redaction_failures == 0,
        "redaction_failures": materialization.redaction_failures,
        "rejected_suggestions": sorted(rejected_ids - accepted_ids),
    }
    manifest.patch_validation = {"proposals": materialization.validation_summaries}


def run_review(
    repo: str | Path,
    base: str,
    head: str,
    *,
    evidence_output: str | Path | None = None,
    json_output: str | Path | None = None,
    markdown_output: str | Path | None = None,
    manifest_output: str | Path | None = None,
    context_output: str | Path | None = None,
    packet_output: str | Path | None = None,
    policy_output: str | Path | None = None,
    analyzers: list[str] | tuple[str, ...] | None = None,
    no_analyzers: bool = False,
    index_repository: bool | None = None,
    assemble_review_packet: bool | None = None,
    review_provider: str | None = None,
    provider_model: str | None = None,
    provider_config_path: str | Path | None = None,
    provider_timeout: float | None = None,
    provider_max_output_tokens: int | None = None,
    provider_dry_run: bool = False,
    provider_transport: ProviderTransport | None = None,
    banner_callback: Callable[[str], None] | None = None,
    allow_patch_suggestions: bool = False,
) -> ReviewResult:
    """Run a read-only metadata review and write requested artifacts.

    ``provider_transport`` exists solely for deterministic tests and offline
    evaluations; the CLI never supplies it, so live runs always use the real
    HTTP transport.  ``allow_patch_suggestions`` opts in to materializing
    provider draft PatchProposals; they are validated and stored but never
    applied.
    """
    started = time.perf_counter()
    run_id = f"run-{uuid.uuid4().hex}"
    if evidence_output is None:
        temporary = tempfile.NamedTemporaryFile(prefix=f"codeatlas-{run_id}-", suffix=".jsonl", delete=False)
        evidence_path = Path(temporary.name)
        temporary.close()
    else:
        evidence_path = Path(evidence_output).expanduser()

    requested = [item.strip() for item in (analyzers or ["secrets", "sensitive-data-exposure"]) for item in item.split(",") if item.strip()]
    manifest = RunManifest(
        run_id=run_id,
        repository=str(Path(repo).expanduser().resolve()),
        base_ref=base,
        head_ref=head,
        configuration={"deep_analysis": False, "provider": "fake", "analyzers": [] if no_analyzers else requested},
    )
    # Standing assertion (Phase 7C): the review command never performs
    # isolated validation; that requires the explicit operator command.
    manifest.isolated_validation_attempted = False
    manifest.isolated_validation_status = "not_allowed_in_review_command"
    manifest.execution_allowed = False
    diff = Diff(base, head)

    try:
        with EvidenceLogger(evidence_path) as evidence:
            evidence.run_started(run_id=run_id, repository=manifest.repository, base_ref=base, head_ref=head)
            repository = validate_repository(repo)
            evidence.repository_validated(run_id=run_id, repository=str(repository.root), status="ok")

            base_ref, head_ref = resolve_base_head(repository.root, base, head)
            manifest.base_commit = base_ref.commit
            manifest.head_commit = head_ref.commit
            evidence.commits_resolved(
                run_id=run_id,
                base_ref=base_ref.name,
                head_ref=head_ref.name,
                base_commit=base_ref.commit,
                head_commit=head_ref.commit,
            )

            diff = extract_diff(repository.root, base_ref.commit, head_ref.commit)
            manifest.tools_run = ["git rev-parse", "git diff", "git worktree"]
            manifest.changes = [
                {
                    "path": change.path,
                    "old_path": change.old_path,
                    "status": change.status.value,
                    "old_ranges": [{"start": item.start, "count": item.count} for item in change.old_ranges],
                    "new_ranges": [{"start": item.start, "count": item.count} for item in change.new_ranges],
                }
                for change in diff.files
            ]
            evidence.diff_extracted(run_id=run_id, count=len(diff.files), status="ok")

            languages = sorted({detect_language(change.path).value for change in diff.files})
            manifest.detected_languages = languages
            manifest.analyzed_files = [change.path for change in diff.files if detect_language(change.path).value != "unknown"]
            manifest.skipped_files = [change.path for change in diff.files if detect_language(change.path).value == "unknown"]
            evidence.languages_detected(run_id=run_id, languages=languages, count=len(languages))

            with temporary_snapshot(repository.root, head_ref.commit) as snapshot:
                evidence.snapshot_created(run_id=run_id, status="ok")
                cfg = _config(snapshot.path / ".codeatlas.yml") or _config(repository.root / ".codeatlas.yml")

                raw_repo_opts = cfg.get("repository", {})
                repo_opts = raw_repo_opts if isinstance(raw_repo_opts, dict) else {}
                if "index" in repo_opts and not isinstance(repo_opts["index"], bool):
                    raise ValueError("repository.index must be boolean")
                if "exclude_paths" in repo_opts and not isinstance(repo_opts["exclude_paths"], list):
                    raise ValueError("repository.exclude_paths must be a list")
                if "max_file_bytes" in repo_opts and (not isinstance(repo_opts["max_file_bytes"], int) or repo_opts["max_file_bytes"] <= 0):
                    raise ValueError("repository.max_file_bytes must be a positive integer")
                if "max_context_candidates" in repo_opts and (not isinstance(repo_opts["max_context_candidates"], int) or repo_opts["max_context_candidates"] <= 0):
                    raise ValueError("repository.max_context_candidates must be a positive integer")
                if "include_tests" in repo_opts and not isinstance(repo_opts["include_tests"], bool):
                    raise ValueError("repository.include_tests must be boolean")
                if "include_configuration" in repo_opts and not isinstance(repo_opts["include_configuration"], bool):
                    raise ValueError("repository.include_configuration must be boolean")

                raw_review_opts = cfg.get("review", {})
                review_opts = raw_review_opts if isinstance(raw_review_opts, dict) else {}
                raw_policy_opts = cfg.get("policy", {})
                policy_opts = raw_policy_opts if isinstance(raw_policy_opts, dict) else {}

                for int_key in ("max_context_files", "max_lines_per_file", "max_total_context_lines", "max_packet_bytes", "max_findings"):
                    if int_key in review_opts and (not isinstance(review_opts[int_key], int) or review_opts[int_key] <= 0):
                        raise ValueError(f"review.{int_key} must be a positive integer")
                for bool_key in ("include_tests", "include_configuration"):
                    if bool_key in review_opts and not isinstance(review_opts[bool_key], bool):
                        raise ValueError(f"review.{bool_key} must be boolean")

                if "min_inline_confidence" in policy_opts and not isinstance(policy_opts["min_inline_confidence"], (int, float)):
                    raise ValueError("policy.min_inline_confidence must be numeric")
                for bool_key in ("require_evidence_for_high", "require_human_for_blocker", "require_human_for_security", "abstain_on_truncated_changed_code", "allow_unverified_findings"):
                    if bool_key in policy_opts and not isinstance(policy_opts[bool_key], bool):
                        raise ValueError(f"policy.{bool_key} must be boolean")

                should_assemble = assemble_review_packet if assemble_review_packet is not None else bool(packet_output or policy_output or review_provider)
                indexing_enabled = index_repository if index_repository is not None else (repo_opts.get("index", False) or should_assemble)

                repo_index = None
                candidates = []
                if indexing_enabled:
                    idx_start = time.perf_counter()
                    repo_index = build_repository_index(
                        snapshot.path,
                        commit_sha=head_ref.commit,
                        config=repo_opts,
                    )
                    idx_duration = (time.perf_counter() - idx_start) * 1000

                    ret_start = time.perf_counter()
                    changed_syms = identify_changed_symbols(repo_index, diff)
                    max_cand = int(repo_opts.get("max_context_candidates", 20))
                    changed_paths = [c.path for c in diff.files]
                    candidates = retrieve_context(
                        repo_index,
                        changed_paths,
                        changed_syms,
                        max_candidates=max_cand,
                    )
                    ret_duration = (time.perf_counter() - ret_start) * 1000

                    manifest.index_version = repo_index.index_version
                    manifest.indexed_files = len(repo_index.files)
                    manifest.indexed_symbols = len(repo_index.symbols)
                    manifest.indexed_imports = len(repo_index.imports)
                    manifest.index_diagnostics = [d.model_dump(mode="json") for d in repo_index.diagnostics]
                    manifest.changed_symbols = [s.model_dump(mode="json") for s in changed_syms]
                    manifest.context_candidates = [c.model_dump(mode="json") for c in candidates]
                    manifest.index_duration_ms = idx_duration
                    manifest.retrieval_duration_ms = ret_duration

                    evidence.emit(
                        "repository_indexed",
                        count=manifest.indexed_files,
                        duration_ms=idx_duration,
                        status="ok",
                    )

                    if context_output is not None:
                        try:
                            cand_payload = [c.model_dump(mode="json") for c in candidates]
                            _write_text(context_output, json.dumps(cand_payload, indent=2, sort_keys=True) + "\n")
                        except (OSError, ValueError) as error:
                            manifest.errors.append(f"Context output failed: {error}")
                elif context_output is not None:
                    try:
                        _write_text(context_output, json.dumps([], indent=2) + "\n")
                    except (OSError, ValueError) as error:
                        manifest.errors.append(f"Context output failed: {error}")

                if not no_analyzers:
                    opts = cfg.get("analyzers", {}) if isinstance(cfg.get("analyzers", {}), dict) else {}
                    enabled = opts.get("enabled")
                    names = requested if enabled is None else ([enabled] if isinstance(enabled, str) else [str(x) for x in enabled])
                    raw_secret_opts = cfg.get("secrets", {})
                    secret_opts = raw_secret_opts if isinstance(raw_secret_opts, dict) else {}
                    allow = tuple(secret_opts.get("allow_patterns", [])) if isinstance(secret_opts.get("allow_patterns", []), list) else ()
                    ignore = bool(secret_opts.get("ignore_placeholders", True))
                    minimum = float(secret_opts.get("min_confidence", 0.0))
                    raw_sensitive_opts = cfg.get("sensitive_data", {})
                    sensitive_opts = raw_sensitive_opts if isinstance(raw_sensitive_opts, dict) else {}
                    if "enabled" in sensitive_opts and not isinstance(sensitive_opts["enabled"], bool):
                        raise ValueError("sensitive_data.enabled must be boolean")
                    sensitive_enabled = sensitive_opts.get("enabled", True)
                    custom_names = sensitive_opts.get("custom_sensitive_names", [])
                    safe_wrappers = sensitive_opts.get("safe_wrappers", [])
                    safe_loggers = sensitive_opts.get("safe_loggers", [])
                    if not isinstance(custom_names, list) or not isinstance(safe_wrappers, list) or not isinstance(safe_loggers, list):
                        raise ValueError("sensitive_data custom_sensitive_names, safe_wrappers, and safe_loggers must be lists")
                    sensitive_minimum = float(sensitive_opts.get("min_confidence", 0.0))
                    sensitive_allow = tuple(sensitive_opts.get("ignore_paths", [])) if isinstance(sensitive_opts.get("ignore_paths", []), list) else ()
                    max_alias_depth = sensitive_opts.get("max_alias_depth", 5)
                    track_local_aliases = sensitive_opts.get("track_local_aliases", True)
                    track_object_access = sensitive_opts.get("track_object_access", True)
                    track_destructuring = sensitive_opts.get("track_destructuring", False)
                    track_containers = sensitive_opts.get("track_containers", False)
                    if not isinstance(max_alias_depth, int) or max_alias_depth < 0:
                        raise ValueError("sensitive_data.max_alias_depth must be a non-negative integer")
                    if not all(isinstance(value, bool) for value in (track_local_aliases, track_object_access, track_destructuring, track_containers)):
                        raise ValueError("sensitive_data tracking options must be boolean")
                    registry = default_registry()
                    for name in names:
                        lookup = "hardcoded-secrets" if name == "secrets" else ("sensitive-data-exposure" if name == "sensitive-data" else name)
                        try:
                            registered = registry.get(lookup)
                            if lookup == "hardcoded-secrets":
                                registered = HardcodedSecretAnalyzer(allow_patterns=allow, ignore_placeholders=ignore, min_confidence=minimum)
                            elif lookup == "sensitive-data-exposure":
                                if not sensitive_enabled:
                                    continue
                                registered = SensitiveDataExposureAnalyzer(
                                    custom_identifiers=custom_names,
                                    allow_patterns=tuple(allow) + tuple(sensitive_allow),
                                    safe_wrappers=safe_wrappers,
                                    safe_loggers=safe_loggers,
                                    min_confidence=sensitive_minimum,
                                    max_alias_depth=max_alias_depth,
                                    track_local_aliases=track_local_aliases,
                                    track_object_access=track_object_access,
                                    track_destructuring=track_destructuring,
                                    track_containers=track_containers,
                                )
                            result = registered.analyze(AnalysisContext(snapshot, diff, allow, ignore, minimum))
                            manifest.findings.extend(item.model_dump(mode="json", exclude_none=True) for item in result)
                            manifest.analyzers.append(name)
                        except Exception as error:
                            message = f"{name}: {type(error).__name__}: {error}"
                            manifest.analyzer_failures.append(message)
                            manifest.errors.append(f"Analyzer failed: {message}")

                if should_assemble:
                    evidence.emit("packet_started", status="started")
                    packet = _assemble_review_packet(
                        snapshot.path,
                        diff,
                        repository_name=manifest.repository,
                        repo_index=repo_index,
                        context_candidates=candidates,
                        deterministic_findings=manifest.findings,
                        config=cfg,
                        base_commit=manifest.base_commit,
                        head_commit=manifest.head_commit,
                    )
                    deterministic_quality = [
                        assess_finding(
                            Finding.model_validate(item),
                            packet=packet,
                            deterministic_support=1.0,
                        )
                        for item in manifest.findings
                    ]
                    manifest.findings = [item.model_dump(mode="json", exclude_none=True) for item in deterministic_quality]
                    packet = packet.model_copy(
                        update={
                            "deterministic_findings": list(manifest.findings),
                            "quality_summary": quality_summary(deterministic_quality),
                            "quality_limitations": quality_summary(deterministic_quality).get("limitations", [])[:20],
                        }
                    )
                    manifest.quality_summary = quality_summary(deterministic_quality)
                    manifest.quality_limitations = list(packet.quality_limitations)
                    manifest.quality_decisions = [item.quality_decision for item in deterministic_quality]
                    manifest.review_packet_id = packet.packet_id
                    manifest.packet_bytes = packet.packet_size_statistics.total_bytes
                    manifest.packet_files = packet.packet_size_statistics.total_files
                    manifest.packet_lines = packet.packet_size_statistics.total_lines
                    manifest.packet_truncated = packet.truncated
                    manifest.packet_limitations = list(packet.limitations)
                    manifest.redaction_audit = packet.redaction_status.model_dump(mode="json")

                    evidence.emit("context_selected", count=len(packet.context_candidates), status="ok")
                    if packet.excluded_candidates:
                        evidence.emit("context_excluded", count=len(packet.excluded_candidates), status="ok")
                    if packet.truncated:
                        evidence.emit("packet_truncated", count=len(packet.limitations), status="ok")
                    evidence.emit(
                        "packet_redacted",
                        status="ok" if packet.redaction_status.redacted else "failed",
                        count=packet.redaction_status.raw_value_matches,
                    )

                    policy_decision = evaluate_policy(packet, manifest.findings, config=cfg)
                    manifest.policy_decisions = [policy_decision.model_dump(mode="json")]
                    evidence.emit(
                        "policy_evaluated",
                        decision=policy_decision.decision,
                        status=policy_decision.decision,
                    )

                    is_blocked = not policy_decision.allowed or policy_decision.decision == "blocked"
                    if is_blocked:
                        manifest.errors.append(
                            f"Review blocked by policy: {'; '.join(policy_decision.blocking_reasons or policy_decision.reasons)}"
                        )
                        evidence.emit(
                            "review_blocked",
                            decision=policy_decision.decision,
                            reason="; ".join(policy_decision.blocking_reasons or policy_decision.reasons),
                        )
                    else:
                        evidence.emit("review_completed", decision=policy_decision.decision, status="ok")

                    if packet_output is not None:
                        try:
                            _write_text(packet_output, json.dumps(packet.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
                        except (OSError, ValueError) as error:
                            manifest.errors.append(f"Packet output failed: {error}")

                    if policy_output is not None:
                        try:
                            _write_text(policy_output, json.dumps(policy_decision.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
                        except (OSError, ValueError) as error:
                            manifest.errors.append(f"Policy output failed: {error}")

                    if review_provider:
                        manifest.provider_name = review_provider.split(":", 1)[0]
                        if is_blocked:
                            manifest.errors.append(f"Provider {review_provider} skipped because review is blocked by policy")
                        else:
                            evidence.emit("provider_started", provider=manifest.provider_name, status="started")
                            provider_key = review_provider.split(":", 1)[0]
                            provider_mode = review_provider.split(":", 1)[1] if ":" in review_provider else str(review_opts.get("mock_mode", "clean"))
                            if provider_key == "mock":
                                provider = MockReviewer(mode=provider_mode)
                                provider_res = provider.review(packet)
                                manifest.provider_version = provider_res.provider_version
                                evidence.emit("provider_completed", provider=manifest.provider_name, status="ok")

                                val_res = validate_provider_output(packet, provider_res.findings)
                                manifest.provider_output_valid = val_res.is_valid
                                manifest.provider_validation_errors = val_res.validation_errors

                                if not val_res.is_valid:
                                    evidence.emit("provider_output_rejected", count=len(val_res.validation_errors), status="rejected")
                                    manifest.errors.append(
                                        f"Provider output validation failed: {'; '.join(val_res.validation_errors)}"
                                    )
                                else:
                                    merged = merge_and_rank_findings(
                                        manifest.findings,
                                        val_res.valid_findings,
                                        packet=packet,
                                    )
                                    manifest.merged_findings = [f.model_dump(mode="json", exclude_none=True) for f in merged]
                                    manifest.findings = manifest.merged_findings
                                    manifest.quality_summary = quality_summary(merged)
                                    manifest.quality_limitations = manifest.quality_summary.get("limitations", [])[:20]
                                    manifest.quality_decisions = [f.quality_decision for f in merged]
                                    evidence.emit("findings_merged", count=len(merged), status="ok")
                            elif provider_key == "live":
                                _run_live_provider(
                                    packet=packet,
                                    manifest=manifest,
                                    evidence=evidence,
                                    cfg=cfg,
                                    review_opts=review_opts,
                                    policy_decision=policy_decision,
                                    provider_model=provider_model,
                                    provider_config_path=provider_config_path,
                                    provider_timeout=provider_timeout,
                                    provider_max_output_tokens=provider_max_output_tokens,
                                    provider_dry_run=provider_dry_run,
                                    provider_transport=provider_transport,
                                    banner_callback=banner_callback,
                                    allow_patch_suggestions=allow_patch_suggestions,
                                    snapshot_path=snapshot.path,
                                    run_id=run_id,
                                )
                            else:
                                manifest.errors.append(f"Unknown review provider: {review_provider}")
                    else:
                        ranked = merge_and_rank_findings(manifest.findings, [], packet=packet)
                        manifest.merged_findings = [f.model_dump(mode="json", exclude_none=True) for f in ranked]
                        manifest.findings = manifest.merged_findings
                        manifest.quality_summary = quality_summary(ranked)
                        manifest.quality_limitations = manifest.quality_summary.get("limitations", [])[:20]
                        manifest.quality_decisions = [f.quality_decision for f in ranked]

                    # Re-evaluate the deterministic policy against the merged
                    # finding set, but use the bounded pre-quality confidence
                    # observation.  Quality display clamping must not silently
                    # rewrite the legacy policy contract after provider output
                    # is merged; provider output still cannot supply policy.
                    policy_findings = []
                    for payload in manifest.findings:
                        policy_finding = Finding.model_validate(payload)
                        observed_confidence = policy_finding.provenance.get("observed_confidence")
                        if observed_confidence is not None:
                            policy_finding = policy_finding.model_copy(
                                update={"confidence": max(0.0, min(1.0, float(observed_confidence)))}
                            )
                        policy_findings.append(policy_finding)
                    final_policy = evaluate_policy(packet, policy_findings, config=cfg)
                    manifest.policy_decisions = [final_policy.model_dump(mode="json")]
                    evidence.emit(
                        "quality_evaluated",
                        quality_version=manifest.quality_version,
                        count=len(manifest.findings),
                        status="ok",
                    )
                    for finding in manifest.findings[:200]:
                        evidence.emit(
                            "finding_quality_evaluated",
                            finding_id=str(finding.get("id", "")),
                            quality_version=str(finding.get("quality_version", manifest.quality_version)),
                            quality_decision=str(finding.get("quality_decision", "review_only")),
                            quality_score=float(finding.get("quality_score", 0.0) or 0.0),
                            suppressed_count=len(finding.get("suppressed_finding_ids", []) or []),
                            status="ok",
                        )
                    if policy_output is not None:
                        try:
                            _write_text(policy_output, json.dumps(final_policy.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
                        except (OSError, ValueError) as error:
                            manifest.errors.append(f"Policy output failed: {error}")

                if not manifest.quality_summary and manifest.findings:
                    quality_findings = merge_and_rank_findings(manifest.findings, [])
                    manifest.findings = [item.model_dump(mode="json", exclude_none=True) for item in quality_findings]
                    manifest.merged_findings = list(manifest.findings)
                    manifest.quality_summary = quality_summary(quality_findings)
                    manifest.quality_limitations = manifest.quality_summary.get("limitations", [])[:20]
                    manifest.quality_decisions = [item.quality_decision for item in quality_findings]
                manifest.duration_ms = (time.perf_counter() - started) * 1000
            evidence.run_completed(run_id=run_id, status="ok", duration_ms=manifest.duration_ms)
    except Exception as error:  # preserve the failure in the manifest
        manifest.errors.append(f"{type(error).__name__}: {error}")
        manifest.duration_ms = (time.perf_counter() - started) * 1000
        try:
            with EvidenceLogger(evidence_path) as evidence:
                evidence.run_failed(run_id=run_id, error_type=type(error).__name__, message=str(error))
        except OSError:
            manifest.errors.append("Evidence logging failed after the run error")

    payload = manifest.model_dump(mode="json")
    if json_output is not None:
        try:
            _write_text(json_output, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except (OSError, ValueError) as error:
            manifest.errors.append(f"JSON output failed: {error}")
    if markdown_output is not None:
        try:
            _write_text(markdown_output, _markdown(manifest, diff))
        except (OSError, ValueError) as error:
            manifest.errors.append(f"Markdown output failed: {error}")
    if manifest_output is not None:
        try:
            _write_text(manifest_output, json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
        except (OSError, ValueError) as error:
            manifest.errors.append(f"Manifest output failed: {error}")
    return ReviewResult(manifest, diff, evidence_path)


__all__ = ["ReviewResult", "run_review"]
