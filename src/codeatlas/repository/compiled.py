"""Bounded grammar parsing for Go, Java, and Rust, without toolchain execution."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from tree_sitter import Node
from codeatlas.repository.models import Symbol

MAX_SOURCE_BYTES = 128_000
MAX_NODES = 50_000
MAX_SYMBOLS = 256

GRAMMARS = {"go": "tree_sitter_go", "java": "tree_sitter_java", "rust": "tree_sitter_rust"}
DECLARATIONS = {
    "go": {"function_declaration": "function", "method_declaration": "method", "type_spec": "type"},
    "java": {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
             "record_declaration": "record", "method_declaration": "method", "constructor_declaration": "constructor"},
    "rust": {"function_item": "function", "struct_item": "struct", "enum_item": "enum", "trait_item": "trait",
             "type_item": "type", "mod_item": "module"},
}


def new_parser(language: str):
    from tree_sitter import Language, Parser

    grammar = importlib.import_module(GRAMMARS[language])
    return Parser(Language(grammar.language()))


def parser_available(language: str) -> bool:
    try:
        new_parser(language)
        return True
    except (ImportError, AttributeError, TypeError, ValueError, KeyError):
        return False


@dataclass(frozen=True)
class GrammarResult:
    symbols: tuple[Symbol, ...] = ()
    valid: bool | None = None
    errors: tuple[str, ...] = ()
    reason: str = ""


def parse_source(path: str, source: str, language: str) -> GrammarResult:
    raw = source.encode("utf-8")
    if len(raw) > MAX_SOURCE_BYTES or b"\x00" in raw:
        return GrammarResult(reason="source_bounds_exceeded")
    try:
        parser = new_parser(language)
    except (ImportError, AttributeError, TypeError, ValueError, KeyError):
        return GrammarResult(reason="parser_unavailable")
    tree = parser.parse(raw)
    if tree.root_node.has_error:
        # Do not echo source-bearing parser diagnostics into public evidence.
        return GrammarResult(valid=False, errors=(f"{language} grammar rejected modified source",), reason="syntax_failed")
    symbols: list[Symbol] = []
    pending: list[tuple[Node, str | None]] = [(tree.root_node, None)]
    count = 0
    while pending:
        node, parent = pending.pop()
        count += 1
        if count > MAX_NODES or len(symbols) >= MAX_SYMBOLS:
            return GrammarResult(reason="parser_bounds_exceeded")
        kind = DECLARATIONS[language].get(node.type)
        if kind is None:
            pending.extend((child, parent) for child in reversed(node.named_children))
            continue
        name_node = node.child_by_field_name("name") if kind else None
        if name_node is not None:
            name = raw[name_node.start_byte:name_node.end_byte].decode("utf-8")
            if len(name) > 128:
                return GrammarResult(reason="symbol_bounds_exceeded")
            # Include coordinates to distinguish overloaded Java methods and Go receivers.
            symbol_id = f"sym::{path}::{kind}::{node.start_byte}::{name}"
            prefix = raw[node.start_byte:name_node.start_byte].decode("utf-8")
            exported = name[:1].isupper() if language == "go" else (
                "public" in prefix.split() if language == "java" else prefix.lstrip().startswith("pub ")
            )
            symbols.append(Symbol(
                id=symbol_id, file=path, name=name, kind=kind,
                start_line=node.start_point.row + 1, end_line=node.end_point.row + 1,
                parent_id=parent, exported=exported, parser_version=f"tree-sitter-{language}-1",
                parse_confidence=1.0,
            ))
            parent = symbol_id
        pending.extend((child, parent) for child in reversed(node.named_children))
    return GrammarResult(symbols=tuple(symbols), valid=True)
