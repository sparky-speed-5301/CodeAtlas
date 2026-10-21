"""Inspectable repository index with freshness, serialization, and fault tolerance."""

from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any, Mapping

from codeatlas.repository.imports import (
    extract_compiled_imports,
    extract_python_imports,
    extract_ts_js_imports,
    resolve_import,
)
from codeatlas.repository.models import (
    GraphEdge,
    GraphLimits,
    ImportReference,
    IndexDiagnostic,
    RepositoryIndex,
    Symbol,
    SymbolReference,
)
from codeatlas.repository.references import extract_references_for_file
from codeatlas.repository.relationships import extract_relationship_edges
from codeatlas.repository.scanner import scan_repository
from codeatlas.repository.symbols import (
    extract_compiled_symbols,
    extract_python_symbols,
    extract_ts_js_symbols,
)


def build_repository_index(
    repo_root: Path | str,
    *,
    commit_sha: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> RepositoryIndex:
    """Build a deterministic repository index from an isolated snapshot.

    Never executes repository code or tests. Gracefully absorbs parse errors.
    """
    started = time.perf_counter()
    root = Path(repo_root).resolve()

    cfg = config or {}
    exclude_paths = cfg.get("exclude_paths")
    if exclude_paths is not None and not isinstance(exclude_paths, (list, tuple)):
        raise ValueError("repository.exclude_paths must be a list of paths/globs")
    max_file_bytes = cfg.get("max_file_bytes", 1_000_000)
    if not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
        raise ValueError("repository.max_file_bytes must be a positive integer")

    graph_config = cfg.get("graph_limits", {})
    if not isinstance(graph_config, Mapping):
        raise ValueError("repository.graph_limits must be an object")
    graph_limits = GraphLimits.model_validate(graph_config)
    if "max_graph_edges" in cfg:
        graph_limits = graph_limits.model_copy(update={"max_edges": cfg["max_graph_edges"]})

    inventory = scan_repository(
        root,
        exclude_paths=exclude_paths,
        max_file_bytes=max_file_bytes,
    )

    graph_diagnostics: list[IndexDiagnostic] = []
    if len(inventory) > graph_limits.max_files:
        retained_paths = sorted(inventory)[: graph_limits.max_files]
        inventory = {path: inventory[path] for path in retained_paths}
        graph_diagnostics.append(
            IndexDiagnostic(
                file="<repository>",
                level="warning",
                message="Repository file limit exceeded; index was bounded deterministically.",
                reason="graph_bounds_exceeded",
            )
        )

    all_symbols: list[Symbol] = []
    all_imports: list[ImportReference] = []
    all_references: list[SymbolReference] = []
    all_graph_edges: list[GraphEdge] = []
    all_diagnostics: list[IndexDiagnostic] = []
    all_diagnostics.extend(graph_diagnostics)

    file_sources: dict[str, str] = {}

    # Phase 1: Parse symbols and imports
    for rel_path, repo_file in inventory.items():
        if repo_file.is_binary or repo_file.bytes > max_file_bytes:
            continue

        full_path = root / rel_path
        try:
            source = full_path.read_text(encoding="utf-8", errors="replace")
            file_sources[rel_path] = source
        except OSError as err:
            all_diagnostics.append(
                IndexDiagnostic(
                    file=rel_path,
                    level="error",
                    message=f"Failed to read file: {err}",
                    reason="read_error",
                )
            )
            repo_file.parse_status = "failed"
            continue

        if repo_file.language == "python":
            file_imports, imp_diags = extract_python_imports(rel_path, source)
            file_symbols, sym_diags = extract_python_symbols(rel_path, source)
            all_imports.extend(file_imports)
            all_symbols.extend(file_symbols)
            all_diagnostics.extend(imp_diags)
            all_diagnostics.extend(sym_diags)
            has_errors = any(d.level == "error" for d in (imp_diags + sym_diags))
            repo_file.parse_status = "failed" if has_errors else "parsed"

        elif repo_file.language in {"typescript", "javascript"}:
            file_imports, imp_diags = extract_ts_js_imports(rel_path, source)
            file_symbols, sym_diags = extract_ts_js_symbols(rel_path, source)
            all_imports.extend(file_imports)
            all_symbols.extend(file_symbols)
            all_diagnostics.extend(imp_diags)
            all_diagnostics.extend(sym_diags)
            has_errors = any(d.level == "error" for d in (imp_diags + sym_diags))
            repo_file.parse_status = "failed" if has_errors else "parsed"

        elif repo_file.language in {"go", "java", "rust"}:
            file_imports, imp_diags = extract_compiled_imports(rel_path, source, repo_file.language)
            file_symbols, sym_diags = extract_compiled_symbols(rel_path, source, repo_file.language)
            all_imports.extend(file_imports)
            all_symbols.extend(file_symbols)
            all_diagnostics.extend(imp_diags)
            all_diagnostics.extend(sym_diags)
            repo_file.parse_status = "parsed" if not any(d.level == "error" for d in (imp_diags + sym_diags)) else "failed"

    # Phase 2: Statically resolve imports
    available_files = set(inventory.keys())
    for imp in all_imports:
        language = inventory.get(imp.file).language if inventory.get(imp.file) else None
        imp.resolved_path, imp.resolution_state, imp.resolution_reason = resolve_import(
            imp.file,
            imp.module,
            available_files,
            language=language,
            is_dynamic=imp.is_dynamic,
        )

    # Phase 3: Track safe local references
    symbols_by_file: dict[str, list[Symbol]] = {}
    for sym in all_symbols:
        symbols_by_file.setdefault(sym.file, []).append(sym)

    imports_by_file: dict[str, list[ImportReference]] = {}
    for imp in all_imports:
        imports_by_file.setdefault(imp.file, []).append(imp)

    for rel_path, source in file_sources.items():
        file_syms = symbols_by_file.get(rel_path, [])
        file_imps = imports_by_file.get(rel_path, [])
        refs = extract_references_for_file(
            rel_path,
            source,
            file_syms,
            file_imps,
            symbols_by_file,
        )
        all_references.extend(refs)

    if len(all_symbols) > graph_limits.max_symbols:
        all_symbols = all_symbols[: graph_limits.max_symbols]
        all_diagnostics.append(
            IndexDiagnostic(
                file="<repository>",
                level="warning",
                message="Repository symbol limit exceeded; symbols were bounded deterministically.",
                reason="graph_bounds_exceeded",
            )
        )
    unresolved_references = [item for item in all_references if item.resolved_symbol_id is None]
    if len(unresolved_references) > graph_limits.max_unresolved_references:
        unresolved_ids = {id(item) for item in unresolved_references[graph_limits.max_unresolved_references :]}
        all_references = [item for item in all_references if id(item) not in unresolved_ids]
        all_diagnostics.append(
            IndexDiagnostic(
                file="<repository>",
                level="warning",
                message="Unresolved reference limit exceeded; unresolved references were bounded.",
                reason="graph_bounds_exceeded",
            )
        )

    # Phase 4: Build a bounded provenance-bearing graph projection.  The
    # existing import/reference lists remain the compatibility source of truth.
    symbol_by_id = {symbol.id: symbol for symbol in all_symbols}
    for rel_path, source in file_sources.items():
        repo_file = inventory.get(rel_path)
        if repo_file is None:
            continue
        relationship_edges, relationship_diagnostics = extract_relationship_edges(
            rel_path,
            source,
            repo_file.language,
            symbols_by_file.get(rel_path, []),
            all_symbols,
        )
        all_graph_edges.extend(relationship_edges)
        all_diagnostics.extend(relationship_diagnostics)
    for imp in all_imports:
        resolved = imp.resolved_path is not None
        target_symbol_id = None
        edge = GraphEdge(
            edge_id=GraphEdge.make_id(
                edge_type="import",
                source_file=imp.file,
                target_file=imp.resolved_path,
                source_symbol_id=None,
                target_symbol_id=target_symbol_id,
                source_line=imp.line,
            ),
            edge_type="import",
            source_file=imp.file,
            target_file=imp.resolved_path,
            source_line=imp.line,
            parser_confidence=0.5 if imp.is_dynamic else 1.0,
            evidence_kind="import_statement",
            resolution_state=imp.resolution_state,
            resolution_reason=imp.resolution_reason,
        )
        all_graph_edges.append(edge)
    for ref in all_references:
        target = symbol_by_id.get(ref.resolved_symbol_id or "")
        edge_type = "call" if ref.symbol_name.endswith("()") else "reference"
        target_file = target.file if target is not None else None
        edge = GraphEdge(
            edge_id=GraphEdge.make_id(
                edge_type=edge_type,
                source_file=ref.file,
                target_file=target_file,
                source_symbol_id=ref.referencing_symbol_id,
                target_symbol_id=ref.resolved_symbol_id,
                source_line=ref.line,
            ),
            edge_type=edge_type,
            source_file=ref.file,
            target_file=target_file,
            source_symbol_id=ref.referencing_symbol_id,
            target_symbol_id=ref.resolved_symbol_id,
            source_line=ref.line,
            target_line=target.start_line if target is not None else None,
            parser_confidence=1.0 if target is not None else 0.5,
            evidence_kind="symbol_reference",
            resolution_state="resolved" if target is not None else "ambiguous" if ref.unresolved_reason == "ambiguous_name" else "unresolved",
            resolution_reason=ref.unresolved_reason,
        )
        all_graph_edges.append(edge)
    for symbol in all_symbols:
        if symbol.parent_id is None:
            continue
        parent = symbol_by_id.get(symbol.parent_id)
        edge = GraphEdge(
            edge_id=GraphEdge.make_id(
                edge_type="containment",
                source_file=symbol.file,
                target_file=parent.file if parent is not None else symbol.file,
                source_symbol_id=symbol.id,
                target_symbol_id=symbol.parent_id,
                source_line=symbol.start_line,
            ),
            edge_type="containment",
            source_file=symbol.file,
            target_file=parent.file if parent is not None else symbol.file,
            source_symbol_id=symbol.id,
            target_symbol_id=symbol.parent_id,
            source_line=symbol.start_line,
            target_line=parent.start_line if parent is not None else None,
            evidence_kind="symbol_parent",
            resolution_state="resolved" if parent is not None else "unresolved",
            resolution_reason=None if parent is not None else "parent_symbol_missing",
        )
        all_graph_edges.append(edge)

    if len(all_graph_edges) > graph_limits.max_edges:
        all_diagnostics.append(
            IndexDiagnostic(
                file="<repository>",
                level="warning",
                message="Repository graph edge limit exceeded; graph was truncated.",
                reason="graph_bounds_exceeded",
            )
        )
        all_graph_edges = all_graph_edges[: graph_limits.max_edges]

    graph_bytes_exceeded = len(
        json.dumps([item.model_dump(mode="json") for item in all_graph_edges], sort_keys=True).encode("utf-8")
    ) > graph_limits.max_graph_bytes
    while all_graph_edges and len(
        json.dumps([item.model_dump(mode="json") for item in all_graph_edges], sort_keys=True).encode("utf-8")
    ) > graph_limits.max_graph_bytes:
        all_graph_edges.pop()
    if graph_bytes_exceeded:
        all_diagnostics.append(
            IndexDiagnostic(
                file="<repository>",
                level="warning",
                message="Repository graph byte limit exceeded; graph was bounded.",
                reason="graph_bounds_exceeded",
            )
        )

    parser_version = "1.0.0"
    inventory_payload = [
        (path, item.bytes, item.line_count, item.language, item.parse_status)
        for path, item in sorted(inventory.items())
    ]
    source_inventory_sha256 = hashlib.sha256(
        json.dumps(inventory_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    graph_config_sha256 = hashlib.sha256(
        json.dumps(graph_limits.model_dump(mode="json"), separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    if len(all_diagnostics) > graph_limits.max_diagnostics:
        all_diagnostics = all_diagnostics[: graph_limits.max_diagnostics]

    duration_ms = (time.perf_counter() - started) * 1000

    return RepositoryIndex(
        index_version="1.0.0",
        commit_sha=commit_sha,
        files=inventory,
        symbols=all_symbols,
        imports=all_imports,
        references=all_references,
        graph_version="1.0.0",
        parser_version=parser_version,
        source_inventory_sha256=source_inventory_sha256,
        graph_config_sha256=graph_config_sha256,
        graph_limits=graph_limits,
        graph_edges=all_graph_edges,
        diagnostics=all_diagnostics,
        build_duration_ms=duration_ms,
    )


def is_index_fresh(
    index: RepositoryIndex,
    current_commit: str | None,
    *,
    source_inventory_sha256: str | None = None,
    graph_config_sha256: str | None = None,
    parser_version: str | None = None,
) -> bool:
    """Check commit freshness and optional parser/source/config identities."""
    if not index.commit_sha or not current_commit:
        return False
    if index.commit_sha.strip() != current_commit.strip():
        return False
    if source_inventory_sha256 is not None and index.source_inventory_sha256 != source_inventory_sha256:
        return False
    if graph_config_sha256 is not None and index.graph_config_sha256 != graph_config_sha256:
        return False
    if parser_version is not None and index.parser_version != parser_version:
        return False
    return True


def serialize_index_to_json(index: RepositoryIndex) -> str:
    """Serialize the repository index to a deterministic JSON string."""
    payload = index.model_dump(mode="json")
    return json.dumps(payload, indent=2, sort_keys=True)


def deserialize_index_from_json(data: str) -> RepositoryIndex:
    """Load a repository index from a JSON string."""
    payload = json.loads(data)
    return RepositoryIndex.model_validate(payload)


__all__ = [
    "build_repository_index",
    "is_index_fresh",
    "serialize_index_to_json",
    "deserialize_index_from_json",
]
