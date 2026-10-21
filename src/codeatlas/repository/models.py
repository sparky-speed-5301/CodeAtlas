"""Typed models for repository intelligence, graph edges, and context."""

from __future__ import annotations

import hashlib
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class RepositoryFile(BaseModel):
    """File inventory item describing repository path, language, kind, and parse status."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Repository-relative posix path")
    language: str = Field(default="unknown", description="Detected language")
    file_kind: str = Field(default="source", description="source, test, configuration, documentation, build, asset, or other")
    bytes: int = Field(ge=0, description="Size in bytes")
    line_count: int = Field(ge=0, description="Total line count")
    is_test: bool = Field(default=False)
    is_generated: bool = Field(default=False)
    is_configuration: bool = Field(default=False)
    is_binary: bool = Field(default=False)
    parse_status: str = Field(default="skipped", description="parsed, skipped, failed, or unsupported")


class Symbol(BaseModel):
    """Statically extracted code symbol."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="Deterministic stable symbol ID")
    file: str = Field(description="Repository-relative posix path")
    name: str = Field(description="Identifier name")
    kind: str = Field(description="function, async_function, class, method, module_variable, interface, type_alias, or exported_declaration")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    parent_id: str | None = Field(default=None, description="Enclosing parent symbol ID if nested")
    exported: bool = Field(default=False)
    parser_version: str = Field(default="1.0.0")
    parse_confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ImportReference(BaseModel):
    """Import statement extracted from source."""

    model_config = ConfigDict(extra="ignore")

    file: str = Field(description="Importing file path")
    module: str = Field(description="Module name or import path")
    names: list[str] = Field(default_factory=list, description="Imported symbol names")
    alias: str | None = Field(default=None)
    is_relative: bool = Field(default=False)
    is_dynamic: bool = Field(default=False)
    resolved_path: str | None = Field(default=None, description="Resolved repository-relative path if local")
    line: int = Field(default=1, ge=1)


class SymbolReference(BaseModel):
    """Occurrence/reference to a symbol within source code."""

    model_config = ConfigDict(extra="ignore")

    file: str = Field(description="Referencing file path")
    symbol_name: str = Field(description="Referenced symbol name")
    line: int = Field(ge=1)
    referencing_symbol_id: str | None = Field(default=None, description="Enclosing symbol ID")
    resolved_symbol_id: str | None = Field(default=None, description="Resolved target symbol ID")
    resolution_reason: str | None = Field(default=None)
    unresolved_reason: str | None = Field(default=None)


class GraphEdge(BaseModel):
    """Provenance-bearing relationship in the bounded repository graph."""

    model_config = ConfigDict(extra="ignore")

    edge_id: str = Field(pattern=r"^edge::[0-9a-f]{64}$")
    edge_type: Literal[
        "import",
        "reference",
        "call",
        "containment",
        "export",
        "inheritance",
        "route_handler",
        "configuration_key",
    ]
    source_file: str = Field(min_length=1, max_length=512)
    target_file: str | None = Field(default=None, max_length=512)
    source_symbol_id: str | None = Field(default=None, max_length=256)
    target_symbol_id: str | None = Field(default=None, max_length=256)
    source_line: int | None = Field(default=None, ge=1)
    target_line: int | None = Field(default=None, ge=1)
    parser_version: str = Field(default="1.0.0", max_length=64)
    parser_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_kind: Literal["import_statement", "symbol_reference", "symbol_parent"]
    resolution_state: Literal["resolved", "unresolved", "ambiguous", "unsupported"]
    resolution_reason: str | None = Field(default=None, max_length=128)

    @staticmethod
    def make_id(
        *,
        edge_type: str,
        source_file: str,
        target_file: str | None,
        source_symbol_id: str | None,
        target_symbol_id: str | None,
        source_line: int | None,
    ) -> str:
        payload = "|".join(
            (
                edge_type,
                source_file,
                target_file or "",
                source_symbol_id or "",
                target_symbol_id or "",
                str(source_line or 0),
            )
        )
        return f"edge::{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class GraphLimits(BaseModel):
    """Hard bounds for graph construction and future bounded traversals."""

    model_config = ConfigDict(extra="ignore")

    max_files: int = Field(default=10_000, ge=1, le=100_000)
    max_symbols: int = Field(default=50_000, ge=1, le=500_000)
    max_edges: int = Field(default=10_000, ge=1, le=500_000)
    max_graph_bytes: int = Field(default=4_000_000, ge=1_024, le=64_000_000)
    max_traversal_depth: int = Field(default=8, ge=1, le=32)
    max_flow_paths: int = Field(default=1_000, ge=1, le=50_000)
    max_unresolved_references: int = Field(default=10_000, ge=0, le=100_000)
    max_diagnostics: int = Field(default=10_000, ge=1, le=100_000)


class IndexDiagnostic(BaseModel):
    """Parse or index diagnostic message."""

    model_config = ConfigDict(extra="ignore")

    file: str
    line: int | None = None
    level: str = Field(default="info", description="info, warning, or error")
    message: str
    reason: str


class ContextCandidate(BaseModel):
    """A related file/symbol candidate retrieved for review context."""

    model_config = ConfigDict(extra="ignore")

    file: str
    reason: str
    score: float = Field(ge=0.0, le=1.0)
    signals: list[str] = Field(default_factory=list)
    line_range: list[int] = Field(default_factory=lambda: [1, 1], description="[start_line, end_line]")
    is_directly_changed: bool = Field(default=False)
    is_test: bool = Field(default=False)
    is_configuration: bool = Field(default=False)


class RepositoryIndex(BaseModel):
    """Inspectable repository index capturing files, symbols, imports, and diagnostics."""

    model_config = ConfigDict(extra="ignore")

    index_version: str = Field(default="1.0.0")
    commit_sha: str | None = Field(default=None)
    files: dict[str, RepositoryFile] = Field(default_factory=dict)
    symbols: list[Symbol] = Field(default_factory=list)
    imports: list[ImportReference] = Field(default_factory=list)
    references: list[SymbolReference] = Field(default_factory=list)
    graph_version: str = Field(default="1.0.0")
    parser_version: str = Field(default="1.0.0")
    source_inventory_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    graph_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    graph_limits: GraphLimits = Field(default_factory=GraphLimits)
    graph_edges: list[GraphEdge] = Field(default_factory=list)
    diagnostics: list[IndexDiagnostic] = Field(default_factory=list)
    build_duration_ms: float = Field(default=0.0, ge=0.0)


__all__ = [
    "RepositoryFile",
    "Symbol",
    "ImportReference",
    "SymbolReference",
    "GraphEdge",
    "GraphLimits",
    "IndexDiagnostic",
    "ContextCandidate",
    "RepositoryIndex",
]
