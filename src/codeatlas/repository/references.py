"""Safe, local symbol reference tracking and unresolved reference diagnostics."""

from __future__ import annotations

import re
from typing import Sequence

from codeatlas.repository.models import ImportReference, Symbol, SymbolReference

_IDENTIFIER_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")


def _find_enclosing_symbol(line: int, symbols: Sequence[Symbol]) -> Symbol | None:
    """Find the tightest symbol containing the given line number."""
    enclosing: Symbol | None = None
    for sym in symbols:
        if sym.start_line <= line <= sym.end_line:
            if enclosing is None or (sym.end_line - sym.start_line < enclosing.end_line - enclosing.start_line):
                enclosing = sym
    return enclosing


def extract_references_for_file(
    file_path: str,
    source: str,
    file_symbols: Sequence[Symbol],
    file_imports: Sequence[ImportReference],
    all_symbols_by_file: dict[str, list[Symbol]],
) -> list[SymbolReference]:
    """Extract statically identifiable references in a file and attempt safe local resolution."""
    references: list[SymbolReference] = []

    # Map of local symbols in this file
    local_symbols = {s.name: s for s in file_symbols}

    # Map of imported symbols to resolved target symbols
    imported_targets: dict[str, tuple[str, str]] = {}  # imported_name -> (target_file, target_sym_id)
    unresolved_imports: dict[str, str] = {}  # imported_name -> unresolved_reason

    for imp in file_imports:
        if imp.is_dynamic:
            for name in imp.names:
                unresolved_imports[name] = "dynamic_import"
            continue

        if not imp.resolved_path:
            for name in imp.names:
                unresolved_imports[name] = "external_package"
            continue

        target_file = imp.resolved_path
        target_file_syms = all_symbols_by_file.get(target_file, [])
        sym_map = {s.name: s for s in target_file_syms}

        for name in imp.names:
            if name in sym_map:
                imported_targets[name] = (target_file, sym_map[name].id)
            else:
                unresolved_imports[name] = "parser_limitation"

    # Scan lines for identifiers and calls
    lines = source.splitlines()
    for lineno, line_text in enumerate(lines, start=1):
        # Strip string literals and comments to reduce noise
        clean_line = re.sub(r'["\'][^"\']*["\']', "''", line_text)
        clean_line = clean_line.split("//", 1)[0].split("#", 1)[0]

        enclosing = _find_enclosing_symbol(lineno, file_symbols)
        enclosing_id = enclosing.id if enclosing else None

        for match in _IDENTIFIER_CALL.finditer(clean_line):
            ident = match.group(1)

            # Skip keywords
            if ident in {
                "if", "for", "while", "switch", "catch", "return", "function",
                "class", "interface", "type", "import", "export", "require",
                "const", "let", "var", "def", "async", "await", "print", "super",
            }:
                continue

            # Don't record a symbol referring to its own declaration line
            if enclosing and enclosing.name == ident and enclosing.start_line == lineno:
                continue

            resolved_id: str | None = None
            res_reason: str | None = None
            unres_reason: str | None = None

            # 1. Resolve to same-file definition
            if ident in local_symbols:
                resolved_id = local_symbols[ident].id
                res_reason = "same_file_definition"
            # 2. Resolve to statically resolved imported local symbol
            elif ident in imported_targets:
                _, resolved_id = imported_targets[ident]
                res_reason = "imported_local_symbol"
            # 3. Known unresolved imported symbol
            elif ident in unresolved_imports:
                unres_reason = unresolved_imports[ident]
            else:
                unres_reason = "ambiguous_name"

            references.append(
                SymbolReference(
                    file=file_path,
                    symbol_name=ident,
                    line=lineno,
                    referencing_symbol_id=enclosing_id,
                    resolved_symbol_id=resolved_id,
                    resolution_reason=res_reason,
                    unresolved_reason=unres_reason,
                )
            )

    return references


__all__ = ["extract_references_for_file"]
