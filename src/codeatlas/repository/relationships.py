"""Conservative declaration and framework relationship extraction."""

from __future__ import annotations

import re
from collections.abc import Sequence

from codeatlas.repository.models import GraphEdge, IndexDiagnostic, Symbol

_PY_CLASS = re.compile(r"^\s*class\s+(?P<child>[A-Za-z_]\w*)\s*(?:\((?P<bases>[^)]*)\))?\s*:", re.MULTILINE)
_TS_CLASS = re.compile(
    r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<child>[A-Za-z_$][\w$]*)"
    r"(?:\s+extends\s+(?P<extends>[A-Za-z_$][\w$]*))?"
    r"(?:\s+implements\s+(?P<implements>[^\{]+))?"
,
    re.MULTILINE,
)
_PY_ROUTE = re.compile(r"@(?:[A-Za-z_]\w*\.)?(?:route|get|post|put|patch|delete|options|head)\s*\(")
_JS_ROUTE = re.compile(
    r"\b(?:app|router|api)\.(?:get|post|put|patch|delete|options|head)\s*\(\s*['\"][^'\"]+['\"]\s*,\s*(?P<handler>[A-Za-z_$][\w$]*)"
)
_CONFIG_READ = re.compile(
    r"\b(?:os\.getenv|process\.env|getenv|config\.get)\s*\(\s*['\"][A-Za-z_][A-Za-z0-9_.-]*['\"]"
)


def _line_number(source: str, position: int) -> int:
    return source[:position].count("\n") + 1


def _enclosing_symbol(line: int, symbols: Sequence[Symbol]) -> Symbol | None:
    matches = [item for item in symbols if item.start_line <= line <= item.end_line]
    return min(matches, key=lambda item: (item.end_line - item.start_line, item.id), default=None)


def _relationship_edge(
    *,
    edge_type: str,
    file_path: str,
    line: int,
    source_symbol: Symbol | None = None,
    target_symbol: Symbol | None = None,
    resolution_state: str = "resolved",
    resolution_reason: str | None = None,
    evidence_kind: str = "declaration",
    parser_confidence: float = 0.8,
) -> GraphEdge:
    return GraphEdge(
        edge_id=GraphEdge.make_id(
            edge_type=edge_type,
            source_file=file_path,
            target_file=target_symbol.file if target_symbol else file_path,
            source_symbol_id=source_symbol.id if source_symbol else None,
            target_symbol_id=target_symbol.id if target_symbol else None,
            source_line=line,
        ),
        edge_type=edge_type,
        source_file=file_path,
        target_file=target_symbol.file if target_symbol else file_path,
        source_symbol_id=source_symbol.id if source_symbol else None,
        target_symbol_id=target_symbol.id if target_symbol else None,
        source_line=line,
        target_line=target_symbol.start_line if target_symbol else None,
        parser_confidence=parser_confidence,
        evidence_kind=evidence_kind,
        resolution_state=resolution_state,
        resolution_reason=resolution_reason,
    )


def _resolve_symbol(name: str, symbols: Sequence[Symbol]) -> tuple[Symbol | None, str, str | None]:
    candidates = sorted((item for item in symbols if item.name == name), key=lambda item: item.id)
    if len(candidates) == 1:
        return candidates[0], "resolved", None
    if len(candidates) > 1:
        return None, "ambiguous", "multiple_symbols_with_name"
    return None, "unresolved", "symbol_not_indexed"


def extract_relationship_edges(
    file_path: str,
    source: str,
    language: str,
    file_symbols: Sequence[Symbol],
    all_symbols: Sequence[Symbol],
) -> tuple[list[GraphEdge], list[IndexDiagnostic]]:
    """Extract bounded relationship edges without executing framework code."""
    edges: list[GraphEdge] = []
    diagnostics: list[IndexDiagnostic] = []
    local_by_name = {item.name: item for item in file_symbols}

    if language == "python":
        class_matches = _PY_CLASS.finditer(source)
        for match in class_matches:
            child = local_by_name.get(match.group("child"))
            if child is None:
                continue
            bases = [item.strip().split(".")[-1] for item in (match.group("bases") or "").split(",") if item.strip()]
            for base_name in bases:
                target, state, reason = _resolve_symbol(base_name, all_symbols)
                edges.append(
                    _relationship_edge(
                        edge_type="inheritance",
                        file_path=file_path,
                        line=_line_number(source, match.start()),
                        source_symbol=child,
                        target_symbol=target,
                        resolution_state=state,
                        resolution_reason=reason,
                        parser_confidence=0.95 if state == "resolved" else 0.7,
                    )
                )

        lines = source.splitlines()
        for index, line in enumerate(lines):
            if not _PY_ROUTE.search(line):
                continue
            handler: Symbol | None = None
            for candidate in lines[index + 1 : index + 5]:
                function_match = re.search(r"\bdef\s+(?P<name>[A-Za-z_]\w*)\s*\(", candidate)
                if function_match:
                    handler = local_by_name.get(function_match.group("name"))
                    break
            if handler:
                edges.append(
                    _relationship_edge(
                        edge_type="route_handler",
                        file_path=file_path,
                        line=index + 1,
                        source_symbol=handler,
                        evidence_kind="route_declaration",
                        parser_confidence=0.88,
                    )
                )

    elif language in {"javascript", "typescript"}:
        for match in _TS_CLASS.finditer(source):
            child = local_by_name.get(match.group("child"))
            if child is None:
                continue
            names = ([match.group("extends")] if match.group("extends") else [])
            names.extend(item.strip().split("<", 1)[0].strip() for item in (match.group("implements") or "").split(",") if item.strip())
            for base_name in names:
                target, state, reason = _resolve_symbol(base_name, all_symbols)
                edges.append(
                    _relationship_edge(
                        edge_type="inheritance",
                        file_path=file_path,
                        line=_line_number(source, match.start()),
                        source_symbol=child,
                        target_symbol=target,
                        resolution_state=state,
                        resolution_reason=reason,
                        parser_confidence=0.92 if state == "resolved" else 0.7,
                    )
                )

        for match in _JS_ROUTE.finditer(source):
            handler = local_by_name.get(match.group("handler"))
            if handler:
                edges.append(
                    _relationship_edge(
                        edge_type="route_handler",
                        file_path=file_path,
                        line=_line_number(source, match.start()),
                        source_symbol=handler,
                        evidence_kind="route_declaration",
                        parser_confidence=0.82,
                    )
                )

    for symbol in file_symbols:
        if symbol.exported:
            edges.append(
                _relationship_edge(
                    edge_type="export",
                    file_path=file_path,
                    line=symbol.start_line,
                    target_symbol=symbol,
                    parser_confidence=symbol.parse_confidence,
                )
            )

    for match in _CONFIG_READ.finditer(source):
        line = _line_number(source, match.start())
        edges.append(
            _relationship_edge(
                edge_type="configuration_key",
                file_path=file_path,
                line=line,
                source_symbol=_enclosing_symbol(line, file_symbols),
                evidence_kind="configuration_declaration",
                parser_confidence=0.8,
            )
        )

    return edges, diagnostics


__all__ = ["extract_relationship_edges"]
