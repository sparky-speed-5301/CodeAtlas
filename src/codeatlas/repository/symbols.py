"""Deterministic symbol extraction for Python and JavaScript/TypeScript."""

from __future__ import annotations

import ast
import re
from codeatlas.repository.models import IndexDiagnostic, Symbol


def make_symbol_id(
    file_path: str,
    kind: str,
    parent_scope: str | None,
    name: str,
) -> str:
    """Generate a deterministic, stable, secret-free symbol ID."""
    scope = parent_scope or "root"
    norm_file = file_path.replace("\\", "/").strip("/")
    return f"sym::{norm_file}::{kind}::{scope}::{name}"


def _find_matching_brace(source: str, start_index: int) -> int:
    """Find the index of the matching closing brace starting from start_index."""
    depth = 0
    in_string: str | None = None
    in_line_comment = False
    in_block_comment = False
    i = start_index
    length = len(source)

    while i < length:
        char = source[i]

        # Comments and string skipping
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if char == "*" and i + 1 < length and source[i + 1] == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if char == "\\" and i + 1 < length:
                i += 2
                continue
            if char == in_string:
                in_string = None
            i += 1
            continue

        if char == "/" and i + 1 < length:
            next_char = source[i + 1]
            if next_char == "/":
                in_line_comment = True
                i += 2
                continue
            elif next_char == "*":
                in_block_comment = True
                i += 2
                continue

        if char in ('"', "'", "`"):
            in_string = char
            i += 1
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i

        i += 1

    return -1


def extract_python_symbols(
    file_path: str,
    source: str,
) -> tuple[list[Symbol], list[IndexDiagnostic]]:
    """Extract Python symbols using the standard library ast module."""
    symbols: list[Symbol] = []
    diagnostics: list[IndexDiagnostic] = []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as err:
        diagnostics.append(
            IndexDiagnostic(
                file=file_path,
                line=err.lineno,
                level="error",
                message=f"SyntaxError parsing symbols: {err.msg}",
                reason="syntax_error",
            )
        )
        return symbols, diagnostics

    # Detect __all__ if present to guide exported status
    all_exports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                all_exports.add(elt.value)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            is_exported = node.name in all_exports if all_exports else not node.name.startswith("_")
            sym_id = make_symbol_id(file_path, kind, None, node.name)
            end_line = getattr(node, "end_lineno", node.lineno)
            symbols.append(
                Symbol(
                    id=sym_id,
                    file=file_path,
                    name=node.name,
                    kind=kind,
                    start_line=node.lineno,
                    end_line=end_line,
                    parent_id=None,
                    exported=is_exported,
                    parser_version="1.0.0",
                    parse_confidence=1.0,
                )
            )

        elif isinstance(node, ast.ClassDef):
            class_exported = node.name in all_exports if all_exports else not node.name.startswith("_")
            class_id = make_symbol_id(file_path, "class", None, node.name)
            class_end = getattr(node, "end_lineno", node.lineno)
            symbols.append(
                Symbol(
                    id=class_id,
                    file=file_path,
                    name=node.name,
                    kind="class",
                    start_line=node.lineno,
                    end_line=class_end,
                    parent_id=None,
                    exported=class_exported,
                    parser_version="1.0.0",
                    parse_confidence=1.0,
                )
            )

            # Class methods
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_kind = "method"
                    m_exported = not item.name.startswith("_")
                    m_id = make_symbol_id(file_path, m_kind, node.name, item.name)
                    m_end = getattr(item, "end_lineno", item.lineno)
                    symbols.append(
                        Symbol(
                            id=m_id,
                            file=file_path,
                            name=item.name,
                            kind=m_kind,
                            start_line=item.lineno,
                            end_line=m_end,
                            parent_id=class_id,
                            exported=m_exported,
                            parser_version="1.0.0",
                            parse_confidence=1.0,
                        )
                    )

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    if name.startswith("__") and name.endswith("__"):
                        continue
                    sym_id = make_symbol_id(file_path, "module_variable", None, name)
                    end_line = getattr(node, "end_lineno", node.lineno)
                    symbols.append(
                        Symbol(
                            id=sym_id,
                            file=file_path,
                            name=name,
                            kind="module_variable",
                            start_line=node.lineno,
                            end_line=end_line,
                            parent_id=None,
                            exported=name in all_exports if all_exports else not name.startswith("_"),
                            parser_version="1.0.0",
                            parse_confidence=0.9,
                        )
                    )
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                name = node.target.id
                sym_id = make_symbol_id(file_path, "module_variable", None, name)
                end_line = getattr(node, "end_lineno", node.lineno)
                symbols.append(
                    Symbol(
                        id=sym_id,
                        file=file_path,
                        name=name,
                        kind="module_variable",
                        start_line=node.lineno,
                        end_line=end_line,
                        parent_id=None,
                        exported=name in all_exports if all_exports else not name.startswith("_"),
                        parser_version="1.0.0",
                        parse_confidence=0.9,
                    )
                )

    return symbols, diagnostics


_TS_FUNCTION_DECL = re.compile(
    r"""(?m)^[ \t]*(?P<export>export\s+(?:default\s+)?)?(?P<async>async\s+)?function\s*(?P<generator>\*)?\s*(?P<name>[A-Za-z_$][\w$]*)\s*\("""
)

_TS_CLASS_DECL = re.compile(
    r"""(?m)^[ \t]*(?P<export>export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)(?:<[^>]+>)?(?:\s+extends\s+[^{]+)?(?:\s+implements\s+[^{]+)?\s*\{"""
)

_TS_INTERFACE_DECL = re.compile(
    r"""(?m)^[ \t]*(?P<export>export\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)(?:<[^>]+>)?(?:\s+extends\s+[^{]+)?\s*\{"""
)

_TS_TYPE_DECL = re.compile(
    r"""(?m)^[ \t]*(?P<export>export\s+)?type\s+(?P<name>[A-Za-z_$][\w$]*)(?:<[^>]+>)?\s*="""
)

_TS_VAR_FUNCTION_DECL = re.compile(
    r"""(?m)^[ \t]*(?P<export>export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?\s*=\s*(?P<async>async\s+)?(?:\((?:[^()]*|\([^()]*\))*\)|[A-Za-z_$][\w$]*)\s*=>"""
)

_TS_METHOD_DECL = re.compile(
    r"""(?m)^[ \t]*(?:(?:public|private|protected|static|readonly|override)\s+)*(?P<async>async\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<params>[^)]*)\)\s*(?::\s*[^{]+)?\s*\{"""
)


def extract_ts_js_symbols(
    file_path: str,
    source: str,
) -> tuple[list[Symbol], list[IndexDiagnostic]]:
    """Extract TypeScript/JavaScript symbols using structured parsing and brace matching."""
    symbols: list[Symbol] = []
    diagnostics: list[IndexDiagnostic] = []

    def get_line_no(char_pos: int) -> int:
        return source[:char_pos].count("\n") + 1

    # 1. Functions
    for match in _TS_FUNCTION_DECL.finditer(source):
        name = match.group("name")
        exported = bool(match.group("export"))
        is_async = bool(match.group("async"))
        kind = "async_function" if is_async else "function"
        start_line = get_line_no(match.start())

        # Find body start brace
        brace_pos = source.find("{", match.end())
        end_line = start_line
        if brace_pos != -1:
            close_pos = _find_matching_brace(source, brace_pos)
            if close_pos != -1:
                end_line = get_line_no(close_pos)
            else:
                diagnostics.append(
                    IndexDiagnostic(
                        file=file_path,
                        line=start_line,
                        level="error",
                        message=f"SyntaxError: unclosed block in {kind} {name}",
                        reason="syntax_error",
                    )
                )

        sym_id = make_symbol_id(file_path, kind, None, name)
        symbols.append(
            Symbol(
                id=sym_id,
                file=file_path,
                name=name,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                parent_id=None,
                exported=exported,
                parser_version="1.0.0",
                parse_confidence=0.95,
            )
        )

    # 2. Variable-assigned arrow / function expressions
    for match in _TS_VAR_FUNCTION_DECL.finditer(source):
        name = match.group("name")
        exported = bool(match.group("export"))
        start_line = get_line_no(match.start())

        # Check if block body with { ... } or expression
        arrow_end = match.end()
        after_arrow = source[arrow_end:].lstrip()
        end_line = start_line
        if after_arrow.startswith("{"):
            brace_pos = source.find("{", arrow_end)
            close_pos = _find_matching_brace(source, brace_pos)
            if close_pos != -1:
                end_line = get_line_no(close_pos)
        else:
            semi_pos = source.find(";", arrow_end)
            newline_pos = source.find("\n", arrow_end)
            pos = min(x for x in (semi_pos, newline_pos) if x != -1) if (semi_pos != -1 or newline_pos != -1) else -1
            if pos != -1:
                end_line = get_line_no(pos)

        sym_id = make_symbol_id(file_path, "function", None, name)
        symbols.append(
            Symbol(
                id=sym_id,
                file=file_path,
                name=name,
                kind="function",
                start_line=start_line,
                end_line=end_line,
                parent_id=None,
                exported=exported,
                parser_version="1.0.0",
                parse_confidence=0.95,
            )
        )

    # 3. Interfaces
    for match in _TS_INTERFACE_DECL.finditer(source):
        name = match.group("name")
        exported = bool(match.group("export"))
        start_line = get_line_no(match.start())
        brace_pos = match.end() - 1  # includes the {
        close_pos = _find_matching_brace(source, brace_pos)
        end_line = get_line_no(close_pos) if close_pos != -1 else start_line

        sym_id = make_symbol_id(file_path, "interface", None, name)
        symbols.append(
            Symbol(
                id=sym_id,
                file=file_path,
                name=name,
                kind="interface",
                start_line=start_line,
                end_line=end_line,
                parent_id=None,
                exported=exported,
                parser_version="1.0.0",
                parse_confidence=0.95,
            )
        )

    # 4. Type aliases
    for match in _TS_TYPE_DECL.finditer(source):
        name = match.group("name")
        exported = bool(match.group("export"))
        start_line = get_line_no(match.start())
        semi = source.find(";", match.end())
        end_line = get_line_no(semi) if semi != -1 else start_line

        sym_id = make_symbol_id(file_path, "type_alias", None, name)
        symbols.append(
            Symbol(
                id=sym_id,
                file=file_path,
                name=name,
                kind="type_alias",
                start_line=start_line,
                end_line=end_line,
                parent_id=None,
                exported=exported,
                parser_version="1.0.0",
                parse_confidence=0.95,
            )
        )

    # 5. Classes and methods
    for match in _TS_CLASS_DECL.finditer(source):
        name = match.group("name")
        exported = bool(match.group("export"))
        start_line = get_line_no(match.start())
        brace_pos = match.end() - 1
        close_pos = _find_matching_brace(source, brace_pos)
        end_line = get_line_no(close_pos) if close_pos != -1 else start_line
        if close_pos == -1:
            diagnostics.append(
                IndexDiagnostic(
                    file=file_path,
                    line=start_line,
                    level="error",
                    message=f"SyntaxError: unclosed block in class {name}",
                    reason="syntax_error",
                )
            )

        class_id = make_symbol_id(file_path, "class", None, name)
        symbols.append(
            Symbol(
                id=class_id,
                file=file_path,
                name=name,
                kind="class",
                start_line=start_line,
                end_line=end_line,
                parent_id=None,
                exported=exported,
                parser_version="1.0.0",
                parse_confidence=0.95,
            )
        )

        if close_pos != -1:
            class_body = source[brace_pos + 1:close_pos]
            for m_match in _TS_METHOD_DECL.finditer(class_body):
                m_name = m_match.group("name")
                if m_name in {"if", "for", "while", "switch", "catch"}:
                    continue
                m_start_pos = brace_pos + 1 + m_match.start()
                m_start_line = get_line_no(m_start_pos)
                m_brace_pos = brace_pos + 1 + m_match.end() - 1
                m_close = _find_matching_brace(source, m_brace_pos)
                m_end_line = get_line_no(m_close) if m_close != -1 else m_start_line

                m_id = make_symbol_id(file_path, "method", name, m_name)
                symbols.append(
                    Symbol(
                        id=m_id,
                        file=file_path,
                        name=m_name,
                        kind="method",
                        start_line=m_start_line,
                        end_line=m_end_line,
                        parent_id=class_id,
                        exported=not m_name.startswith("_") and not m_name.startswith("#"),
                        parser_version="1.0.0",
                        parse_confidence=0.92,
                    )
                )

    return symbols, diagnostics


__all__ = [
    "make_symbol_id",
    "extract_python_symbols",
    "extract_ts_js_symbols",
]
