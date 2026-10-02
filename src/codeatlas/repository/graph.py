"""Local dependency and reference graph building."""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from codeatlas.repository.models import ImportReference, Symbol, SymbolReference


class DependencyGraph:
    """Graph of file imports and symbol references."""

    def __init__(
        self,
        imports: Sequence[ImportReference],
        references: Sequence[SymbolReference],
        symbols: Sequence[Symbol],
    ) -> None:
        self.imports = list(imports)
        self.references = list(references)
        self.symbols_by_id = {s.id: s for s in symbols}

        # File -> list of files it imports
        self.file_imports: dict[str, set[str]] = defaultdict(set)
        # File -> list of files importing it
        self.file_imported_by: dict[str, set[str]] = defaultdict(set)

        for imp in self.imports:
            if imp.resolved_path:
                self.file_imports[imp.file].add(imp.resolved_path)
                self.file_imported_by[imp.resolved_path].add(imp.file)

        # Symbol ID -> files referencing it
        self.symbol_referencing_files: dict[str, set[str]] = defaultdict(set)
        # Symbol ID -> referencing symbol IDs
        self.symbol_callers: dict[str, set[str]] = defaultdict(set)
        # Symbol ID -> called symbol IDs
        self.symbol_callees: dict[str, set[str]] = defaultdict(set)

        for ref in self.references:
            if ref.resolved_symbol_id:
                self.symbol_referencing_files[ref.resolved_symbol_id].add(ref.file)
                if ref.referencing_symbol_id:
                    self.symbol_callers[ref.resolved_symbol_id].add(ref.referencing_symbol_id)
                    self.symbol_callees[ref.referencing_symbol_id].add(ref.resolved_symbol_id)

    def get_imported_files(self, file_path: str) -> list[str]:
        return sorted(self.file_imports.get(file_path, set()))

    def get_importing_files(self, file_path: str) -> list[str]:
        return sorted(self.file_imported_by.get(file_path, set()))

    def get_referencing_files(self, symbol_id: str) -> list[str]:
        return sorted(self.symbol_referencing_files.get(symbol_id, set()))

    def get_callers(self, symbol_id: str) -> list[str]:
        return sorted(self.symbol_callers.get(symbol_id, set()))

    def get_callees(self, symbol_id: str) -> list[str]:
        return sorted(self.symbol_callees.get(symbol_id, set()))


__all__ = ["DependencyGraph"]
