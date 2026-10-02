"""Deterministic, explainable context retrieval for changed code."""

from __future__ import annotations

import posixpath
from typing import Sequence

from codeatlas.git.models import Diff
from codeatlas.repository.graph import DependencyGraph
from codeatlas.repository.models import ContextCandidate, RepositoryIndex, Symbol


def identify_changed_symbols(
    index: RepositoryIndex,
    diff: Diff,
) -> list[Symbol]:
    """Map changed line ranges from the diff to containing symbols in the index."""
    changed_symbols: list[Symbol] = []
    seen_ids: set[str] = set()

    # Index symbols by file
    symbols_by_file: dict[str, list[Symbol]] = {}
    for sym in index.symbols:
        symbols_by_file.setdefault(sym.file, []).append(sym)

    for change in diff.files:
        path = change.path
        file_syms = symbols_by_file.get(path, [])
        if not file_syms:
            continue

        # Check newly added or modified line ranges
        for r in change.new_ranges:
            r_start = r.start
            r_end = r.start + max(r.count - 1, 0)
            for sym in file_syms:
                if sym.id in seen_ids:
                    continue
                # Overlap test: [sym.start_line, sym.end_line] overlaps with [r_start, r_end]
                if not (sym.end_line < r_start or sym.start_line > r_end):
                    changed_symbols.append(sym)
                    seen_ids.add(sym.id)

    return changed_symbols


def retrieve_context(
    index: RepositoryIndex,
    changed_files: Sequence[str],
    changed_symbols: Sequence[Symbol],
    max_candidates: int = 20,
) -> list[ContextCandidate]:
    """Retrieve and rank relevant context candidates using explainable signals."""
    graph = DependencyGraph(index.imports, index.references, index.symbols)
    candidates: list[ContextCandidate] = []
    seen_candidates: dict[tuple[str, int, int], ContextCandidate] = {}

    changed_files_set = set(changed_files)
    changed_symbol_ids = {s.id for s in changed_symbols}
    changed_symbol_names = {s.name for s in changed_symbols}

    # 1. Changed symbols themselves
    for sym in changed_symbols:
        key = (sym.file, sym.start_line, sym.end_line)
        repo_file = index.files.get(sym.file)
        cand = ContextCandidate(
            file=sym.file,
            reason=f"contains changed symbol {sym.name}",
            score=1.0,
            signals=["changed_symbol"],
            line_range=[sym.start_line, sym.end_line],
            is_directly_changed=True,
            is_test=repo_file.is_test if repo_file else False,
            is_configuration=repo_file.is_configuration if repo_file else False,
        )
        seen_candidates[key] = cand

    # 2. Directly changed files without specific symbol anchors
    for f in changed_files:
        if any(c.file == f and "changed_symbol" in c.signals for c in seen_candidates.values()):
            continue
        key = (f, 1, max(index.files[f].line_count if f in index.files else 1, 1))
        repo_file = index.files.get(f)
        cand = ContextCandidate(
            file=f,
            reason=f"directly changed file {f}",
            score=0.95,
            signals=["changed_file"],
            line_range=[key[1], key[2]],
            is_directly_changed=True,
            is_test=repo_file.is_test if repo_file else False,
            is_configuration=repo_file.is_configuration if repo_file else False,
        )
        seen_candidates[key] = cand

    # 3. References to changed symbols (callers and test references)
    for ref in index.references:
        if ref.resolved_symbol_id in changed_symbol_ids or ref.symbol_name in changed_symbol_names:
            repo_file = index.files.get(ref.file)
            is_test = repo_file.is_test if repo_file else False
            is_directly_changed = ref.file in changed_files_set

            score = 0.88 if is_test else 0.82
            signal = "test_reference" if is_test else "caller"
            reason = (
                f"test references changed symbol {ref.symbol_name}"
                if is_test
                else f"references changed symbol {ref.symbol_name}"
            )

            # Locate the referencing symbol or reference line
            enclosing = graph.symbols_by_id.get(ref.referencing_symbol_id) if ref.referencing_symbol_id else None
            line_range = [enclosing.start_line, enclosing.end_line] if enclosing else [ref.line, ref.line]
            key = (ref.file, line_range[0], line_range[1])

            if key not in seen_candidates or seen_candidates[key].score < score:
                seen_candidates[key] = ContextCandidate(
                    file=ref.file,
                    reason=reason,
                    score=score,
                    signals=[signal],
                    line_range=line_range,
                    is_directly_changed=is_directly_changed,
                    is_test=is_test,
                    is_configuration=repo_file.is_configuration if repo_file else False,
                )

    # 4. Callees of changed symbols
    for sym in changed_symbols:
        callee_ids = graph.get_callees(sym.id)
        for callee_id in callee_ids:
            callee_sym = graph.symbols_by_id.get(callee_id)
            if not callee_sym:
                continue
            key = (callee_sym.file, callee_sym.start_line, callee_sym.end_line)
            repo_file = index.files.get(callee_sym.file)
            if key not in seen_candidates or seen_candidates[key].score < 0.78:
                seen_candidates[key] = ContextCandidate(
                    file=callee_sym.file,
                    reason=f"callee {callee_sym.name} called by changed symbol {sym.name}",
                    score=0.78,
                    signals=["callee"],
                    line_range=[callee_sym.start_line, callee_sym.end_line],
                    is_directly_changed=callee_sym.file in changed_files_set,
                    is_test=repo_file.is_test if repo_file else False,
                    is_configuration=repo_file.is_configuration if repo_file else False,
                )

    # 5. File import relations (files importing changed files, or imported by changed files)
    for f in changed_files:
        # Files that import f
        for importer in graph.get_importing_files(f):
            repo_file = index.files.get(importer)
            is_test = repo_file.is_test if repo_file else False
            score = 0.75 if is_test else 0.70
            signal = "test_file" if is_test else "imported_by"
            key = (importer, 1, max(repo_file.line_count if repo_file else 1, 1))
            if key not in seen_candidates or seen_candidates[key].score < score:
                seen_candidates[key] = ContextCandidate(
                    file=importer,
                    reason=f"imports changed file {f}",
                    score=score,
                    signals=[signal],
                    line_range=[key[1], key[2]],
                    is_directly_changed=importer in changed_files_set,
                    is_test=is_test,
                    is_configuration=repo_file.is_configuration if repo_file else False,
                )

        # Files imported by f
        for imported in graph.get_imported_files(f):
            repo_file = index.files.get(imported)
            key = (imported, 1, max(repo_file.line_count if repo_file else 1, 1))
            if key not in seen_candidates or seen_candidates[key].score < 0.68:
                seen_candidates[key] = ContextCandidate(
                    file=imported,
                    reason=f"imported by changed file {f}",
                    score=0.68,
                    signals=["imports"],
                    line_range=[key[1], key[2]],
                    is_directly_changed=imported in changed_files_set,
                    is_test=repo_file.is_test if repo_file else False,
                    is_configuration=repo_file.is_configuration if repo_file else False,
                )

    # 6. Configuration / dependency manifest changes or relevance
    for f, repo_file in index.files.items():
        if repo_file.is_configuration:
            key = (f, 1, max(repo_file.line_count, 1))
            if f in changed_files_set:
                score = 0.90
                reason = f"changed configuration or dependency manifest {f}"
            else:
                score = 0.55
                reason = f"repository configuration file {f}"
            if key not in seen_candidates or seen_candidates[key].score < score:
                seen_candidates[key] = ContextCandidate(
                    file=f,
                    reason=reason,
                    score=score,
                    signals=["configuration"],
                    line_range=[key[1], key[2]],
                    is_directly_changed=f in changed_files_set,
                    is_test=False,
                    is_configuration=True,
                )

    # 7. Path proximity (files sharing the exact same directory)
    changed_dirs = {posixpath.dirname(f) for f in changed_files if posixpath.dirname(f)}
    for f, repo_file in index.files.items():
        if f not in changed_files_set and posixpath.dirname(f) in changed_dirs:
            key = (f, 1, max(repo_file.line_count, 1))
            if key not in seen_candidates:
                seen_candidates[key] = ContextCandidate(
                    file=f,
                    reason=f"shares directory with changed file ({posixpath.dirname(f)})",
                    score=0.45,
                    signals=["path_proximity"],
                    line_range=[key[1], key[2]],
                    is_directly_changed=False,
                    is_test=repo_file.is_test,
                    is_configuration=repo_file.is_configuration,
                )

    candidates = list(seen_candidates.values())
    # Sort deterministically: descending score, then ascending file, then start_line
    candidates.sort(key=lambda c: (-c.score, c.file, c.line_range[0]))

    return candidates[:max_candidates]


__all__ = [
    "identify_changed_symbols",
    "retrieve_context",
]
