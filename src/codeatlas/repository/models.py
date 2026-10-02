"""Typed models for repository intelligence, symbols, imports, and context."""

from __future__ import annotations

from typing import Any
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
    diagnostics: list[IndexDiagnostic] = Field(default_factory=list)
    build_duration_ms: float = Field(default=0.0, ge=0.0)


__all__ = [
    "RepositoryFile",
    "Symbol",
    "ImportReference",
    "SymbolReference",
    "IndexDiagnostic",
    "ContextCandidate",
    "RepositoryIndex",
]
