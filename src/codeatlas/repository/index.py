"""Inspectable repository index with freshness, serialization, and fault tolerance."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from codeatlas.repository.imports import (
    extract_python_imports,
    extract_ts_js_imports,
    resolve_import_path,
)
from codeatlas.repository.models import (
    ImportReference,
    IndexDiagnostic,
    RepositoryFile,
    RepositoryIndex,
    Symbol,
    SymbolReference,
)
from codeatlas.repository.references import extract_references_for_file
from codeatlas.repository.scanner import scan_repository
from codeatlas.repository.symbols import (
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

    inventory = scan_repository(
        root,
        exclude_paths=exclude_paths,
        max_file_bytes=max_file_bytes,
    )

    all_symbols: list[Symbol] = []
    all_imports: list[ImportReference] = []
    all_references: list[SymbolReference] = []
    all_diagnostics: list[IndexDiagnostic] = []

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

    # Phase 2: Statically resolve imports
    available_files = set(inventory.keys())
    for imp in all_imports:
        imp.resolved_path = resolve_import_path(imp.file, imp.module, available_files)

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

    duration_ms = (time.perf_counter() - started) * 1000

    return RepositoryIndex(
        index_version="1.0.0",
        commit_sha=commit_sha,
        files=inventory,
        symbols=all_symbols,
        imports=all_imports,
        references=all_references,
        diagnostics=all_diagnostics,
        build_duration_ms=duration_ms,
    )


def is_index_fresh(index: RepositoryIndex, current_commit: str | None) -> bool:
    """Check if an index matches the current commit SHA."""
    if not index.commit_sha or not current_commit:
        return False
    return index.commit_sha.strip() == current_commit.strip()


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
