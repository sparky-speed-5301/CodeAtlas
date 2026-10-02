"""Repository intelligence and symbol context package."""

from codeatlas.repository.files import (
    classify_file,
    is_binary_file,
    is_configuration_file,
    is_generated_file,
    is_test_file,
)
from codeatlas.repository.graph import DependencyGraph
from codeatlas.repository.imports import (
    extract_python_imports,
    extract_ts_js_imports,
    resolve_import_path,
)
from codeatlas.repository.index import (
    build_repository_index,
    deserialize_index_from_json,
    is_index_fresh,
    serialize_index_to_json,
)
from codeatlas.repository.models import (
    ContextCandidate,
    ImportReference,
    IndexDiagnostic,
    RepositoryFile,
    RepositoryIndex,
    Symbol,
    SymbolReference,
)
from codeatlas.repository.references import extract_references_for_file
from codeatlas.repository.retrieval import identify_changed_symbols, retrieve_context
from codeatlas.repository.scanner import DEFAULT_EXCLUSIONS, scan_repository
from codeatlas.repository.symbols import (
    extract_python_symbols,
    extract_ts_js_symbols,
    make_symbol_id,
)

__all__ = [
    "RepositoryFile",
    "Symbol",
    "ImportReference",
    "SymbolReference",
    "IndexDiagnostic",
    "ContextCandidate",
    "RepositoryIndex",
    "DEFAULT_EXCLUSIONS",
    "scan_repository",
    "classify_file",
    "is_binary_file",
    "is_generated_file",
    "is_test_file",
    "is_configuration_file",
    "extract_python_imports",
    "extract_ts_js_imports",
    "resolve_import_path",
    "make_symbol_id",
    "extract_python_symbols",
    "extract_ts_js_symbols",
    "extract_references_for_file",
    "DependencyGraph",
    "build_repository_index",
    "is_index_fresh",
    "serialize_index_to_json",
    "deserialize_index_from_json",
    "identify_changed_symbols",
    "retrieve_context",
]
