"""Bounded, deterministic context construction for one repair request."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from codeatlas.adapters import select_language_adapter
from codeatlas.findings.models import Finding
from codeatlas.git.snapshot import Snapshot
from codeatlas.review.packet import ReviewPacket, redact_text

from .manifest import RunManifest
from .repair_models import (
    RepairContext,
    RepairLanguage,
    RepairLimits,
    RepairRepositoryState,
    RepairSymbol,
    compute_context_hash,
    repair_payload_is_safe,
)


class RepairContextBuildError(ValueError):
    """Stable context-build failure without source or provider text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RepairContextBuildResult(BaseModel):
    """Bounded builder outcome useful to service and benchmark callers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context: RepairContext | None = None
    limitations: tuple[str, ...] = Field(default=(), max_length=20)
    truncated_sections: tuple[str, ...] = Field(default=(), max_length=20)


def _safe_path(path: str) -> bool:
    return (
        bool(path)
        and len(path) <= 512
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
        and not any(ord(char) < 32 for char in path)
    )


def _read_source(snapshot: Snapshot, path: str, budget: int) -> str:
    if snapshot.path.is_symlink():
        raise RepairContextBuildError("invalid_snapshot")
    root = snapshot.path.resolve(strict=True)
    target = root
    for part in path.split("/"):
        target = target / part
        if target.is_symlink():
            raise RepairContextBuildError("symlink_target")
    try:
        if not target.is_file():
            raise RepairContextBuildError("invalid_location")
        data = target.read_bytes()
    except OSError as error:
        del error
        raise RepairContextBuildError("unreadable_or_unbounded_target") from None
    if len(data) > budget:
        raise RepairContextBuildError("source_budget_exceeded")
    if b"\x00" in data:
        raise RepairContextBuildError("binary_target")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise RepairContextBuildError("unreadable_or_unbounded_target") from None


def _bounded_text(value: Any, limit: int = 512) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    clean, _ = redact_text(value)
    clean = clean[:limit]
    return clean if clean else None


class RepairContextBuilder:
    """Build the smallest provenance-labelled context for one finding.

    The builder reads only the detached head snapshot and only paths already
    present in the bounded review packet.  It never walks the repository or
    consumes the workspace, environment, provider transcripts, or approval
    state.
    """

    def __init__(self, limits: RepairLimits | None = None) -> None:
        self.limits = limits or RepairLimits()

    def build_result(
        self,
        finding: Finding,
        *,
        packet: ReviewPacket,
        manifest: RunManifest | None = None,
        state: RepairRepositoryState | None = None,
        snapshot: Snapshot,
    ) -> RepairContextBuildResult:
        limitations: list[str] = []
        truncated: list[str] = []
        try:
            finding = Finding.model_validate(finding.model_dump(mode="json"))
            packet = ReviewPacket.model_validate(packet.model_dump(mode="json"))
            limits = RepairLimits.model_validate(self.limits.model_dump())
            if state is not None:
                state = RepairRepositoryState.model_validate(state.model_dump(mode="json"))
            if manifest is not None:
                manifest = RunManifest.model_validate(manifest.model_dump(mode="json"))
            if not isinstance(snapshot, Snapshot) or not isinstance(snapshot.path, Path):
                raise ValueError("invalid snapshot")
        except Exception as error:
            del error
            return RepairContextBuildResult(limitations=("invalid_input",))
        # Scan the original finding before redacting individual context leaves.
        # Redaction is for transmission; it must not turn an unsafe source
        # observation into an apparently safe deterministic evidence record.
        if not repair_payload_is_safe(finding.model_dump(mode="json")):
            return RepairContextBuildResult(limitations=("secret_detected",))
        if state is None:
            state = RepairRepositoryState(
                run_id=str(finding.provenance.get("run_id", "repair")),
                repository=packet.repository,
                base_commit=packet.base_commit or "base",
                head_commit=packet.head_commit or snapshot.commit,
            )
        if manifest is not None and (
            manifest.run_id != state.run_id
            or manifest.review_packet_id != packet.packet_id
            or manifest.base_commit != state.base_commit
            or manifest.head_commit != state.head_commit
        ):
            return RepairContextBuildResult(limitations=("incompatible_repository_state",))
        if not _safe_path(finding.file) or finding.file not in packet.changed_files:
            return RepairContextBuildResult(limitations=("invalid_location",))
        adapter = select_language_adapter(finding.file)
        if adapter is None:
            return RepairContextBuildResult(limitations=("unsupported_language",))
        if any(finding.file == pattern or finding.file.startswith(pattern.rstrip("*").rstrip("/")) for pattern in limits.prohibited_paths):
            return RepairContextBuildResult(limitations=("protected_or_generated_target",))
        try:
            source = _read_source(snapshot, finding.file, limits.max_source_bytes)
        except RepairContextBuildError as error:
            return RepairContextBuildResult(limitations=(error.reason,))
        source_lines = source.splitlines()
        if finding.end_line > len(source_lines):
            return RepairContextBuildResult(limitations=("invalid_location",))

        parsed = adapter.parse_modified_file(finding.file, source)
        if parsed.errors:
            return RepairContextBuildResult(limitations=("source_parse_failed",))
        symbols = [
            item for item in parsed.symbols
            if item.start_line <= finding.start_line <= finding.end_line <= item.end_line
        ]
        symbol = min(symbols, key=lambda item: item.end_line - item.start_line) if symbols else None
        start = symbol.start_line if symbol else max(1, finding.start_line - 10)
        end = symbol.end_line if symbol else min(len(source_lines), finding.end_line + 10)
        if end - start + 1 > limits.max_context_lines or end - start + 1 > limits.max_lines_per_file:
            return RepairContextBuildResult(limitations=("required_context_exceeds_bounds",))
        code, _ = redact_text("\n".join(source_lines[start - 1:end]))
        if len(code.encode("utf-8")) > limits.max_context_bytes:
            return RepairContextBuildResult(limitations=("required_context_exceeds_bounds",))

        # The packet is the source of repository relationships.  The builder
        # never searches for callers/callees or opens unrelated files.
        imports: list[str] = []
        for item in packet.relevant_imports:
            if item.get("file") == finding.file:
                module = _bounded_text(item.get("module"))
                if module:
                    imports.append(module)
        if len(imports) > 10:
            imports = imports[:10]
            truncated.append("repository_context")
            limitations.append("Relevant imports were truncated to the context limit.")

        symbols_out: list[str] = []
        for item in packet.changed_symbols:
            if item.get("file") != finding.file:
                continue
            name = _bounded_text(item.get("name"), 128)
            kind = _bounded_text(item.get("kind"), 64)
            if name and kind:
                symbols_out.append(f"{kind} {name}")
        if len(symbols_out) > limits.max_symbols:
            symbols_out = symbols_out[: limits.max_symbols]
            truncated.append("relevant_symbol_definitions")
            limitations.append("Relevant symbol definitions were truncated to the context limit.")

        callers: list[str] = []
        callees: list[str] = []
        for item in packet.relevant_references:
            if not isinstance(item, dict):
                continue
            relation = str(item.get("relation", item.get("kind", ""))).lower()
            text = _bounded_text(
                ":".join(str(value) for value in (item.get("symbol_name"), item.get("file"), item.get("line")) if value)
            )
            if not text:
                continue
            if "caller" in relation:
                callers.append(text)
            elif "callee" in relation:
                callees.append(text)
        budget = limits.max_callers_callees
        callers = callers[: min(len(callers), budget)]
        callees = callees[: max(0, budget - len(callers))]
        if len(callers) + len(callees) < len(packet.relevant_references):
            truncated.append("callers_callees")
            limitations.append("Caller and callee context was bounded to the configured limit.")

        related = adapter.discover_targeted_tests(
            finding.file,
            packet.relevant_tests[: limits.max_tests],
            finding.tests_consulted,
        )[: limits.max_tests]
        if len(packet.relevant_tests) > limits.max_tests:
            truncated.append("related_tests")
            limitations.append("Related test paths were truncated to the context limit.")

        evidence: list[str] = []
        for evidence_item in finding.evidence[:10]:
            clean = _bounded_text(evidence_item)
            if clean:
                evidence.append(clean)
        if not evidence or not repair_payload_is_safe(evidence):
            return RepairContextBuildResult(limitations=("secret_detected",))
        diagnostic: list[str] = []
        for diagnostic_item in finding.limitations[:10]:
            clean = _bounded_text(diagnostic_item)
            if clean:
                diagnostic.append(clean)
        while sum(len(item.encode("utf-8")) for item in diagnostic) > limits.max_diagnostic_bytes:
            diagnostic.pop()
            truncated.append("diagnostic_evidence")
        if diagnostic and "diagnostic_evidence" in truncated:
            limitations.append("Diagnostic evidence was truncated to the context limit.")

        context_truncated = bool(truncated)
        context_confidence = min(finding.confidence, 0.60) if context_truncated else finding.confidence
        if context_truncated:
            limitations.append("Context truncation lowered the trusted repair confidence bound.")
        provenance_labels = (
            "finding",
            "quality_decision",
            "deterministic_evidence",
            "changed_file",
            "changed_line_range",
            "containing_symbol",
            "repository_conventions",
            "related_tests",
            "language_capabilities",
            "policy_limits",
        )
        try:
            context = RepairContext(
                finding_id=finding.id,
                run_id=state.run_id,
                repository_identity=(state.repository.replace("\\", "/").rstrip("/").split("/")[-1] or "repository")[:128],
                base_commit=state.base_commit,
                head_commit=state.head_commit,
                language=cast(RepairLanguage, adapter.language),
                changed_file=finding.file,
                changed_line_range=(finding.start_line, finding.end_line),
                containing_symbol=(
                    RepairSymbol(name=symbol.name, kind=symbol.kind, start_line=symbol.start_line, end_line=symbol.end_line)
                    if symbol else None
                ),
                code_line_range=(start, end),
                code_context=code,
                category=finding.category,
                claim=finding.claim,
                impact=finding.impact,
                deterministic_evidence=tuple(evidence),
                quality_decision=finding.quality_decision,
                confidence=context_confidence,
                evidence_strength=finding.evidence_strength,
                repository_context=tuple(imports),
                related_tests=tuple(related),
                relevant_symbol_definitions=tuple(symbols_out),
                bounded_callers=tuple(callers),
                bounded_callees=tuple(callees),
                diagnostic_evidence=tuple(diagnostic),
                provenance_labels=provenance_labels,
                truncated_sections=tuple(dict.fromkeys(truncated)),
                limits=limits,
                target_file_limits=limits.max_target_files,
                maximum_patch_line_limits=limits.max_patch_lines,
                prohibited_paths=limits.prohibited_paths,
                context_truncated=context_truncated,
            )
            context = context.model_copy(update={"context_hash": compute_context_hash(context)})
            context = RepairContext.model_validate_json(context.model_dump_json())
        except Exception as error:
            del error
            return RepairContextBuildResult(limitations=("unsafe_or_unbounded_context",))
        return RepairContextBuildResult(
            context=context,
            limitations=tuple(dict.fromkeys(limitations)),
            truncated_sections=tuple(dict.fromkeys(truncated)),
        )

    def build(self, finding: Finding, **kwargs: Any) -> RepairContext:
        result = self.build_result(finding, **kwargs)
        if result.context is None:
            raise RepairContextBuildError(result.limitations[0] if result.limitations else "context_build_failed")
        return result.context


__all__ = ["RepairContextBuildError", "RepairContextBuildResult", "RepairContextBuilder"]
