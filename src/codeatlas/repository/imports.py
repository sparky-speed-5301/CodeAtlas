"""Import extraction for Python and JavaScript/TypeScript without code execution."""

from __future__ import annotations

import ast
import posixpath
import re
from pathlib import Path
from typing import Sequence

from codeatlas.repository.models import ImportReference, IndexDiagnostic

_TS_IMPORT_STATIC = re.compile(
    r"""(?m)^\s*(?:export\s+)?import\s+(?:(?P<clause>[^"';]+?)\s+from\s+)?["'](?P<module>[^"']+)["']\s*;?"""
)

_TS_EXPORT_FROM = re.compile(
    r"""(?m)^\s*export\s+(?P<clause>\*|\{[^}]*\})\s+from\s+["'](?P<module>[^"']+)["']\s*;?"""
)

_TS_REQUIRE = re.compile(
    r"""(?m)(?:(?:const|let|var)\s+(?P<clause>[A-Za-z0-9_{}\s,$]+)\s*=\s*)?require\s*\(\s*["'](?P<module>[^"']+)["']\s*\)"""
)

_TS_DYNAMIC_IMPORT = re.compile(
    r"""(?m)\bimport\s*\(\s*(?P<arg>[^)]+)\s*\)"""
)

_TS_DYNAMIC_REQUIRE = re.compile(
    r"""(?m)\brequire\s*\(\s*(?P<arg>[^"'\s)][^)]*)\s*\)"""
)


def _clean_names(clause: str | None) -> list[str]:
    """Parse symbol names out of an import/export clause string."""
    if not clause:
        return []
    clause = clause.strip()
    names: list[str] = []

    # Handle { a, b as c }
    braced = re.findall(r"\{([^}]+)\}", clause)
    for group in braced:
        for item in group.split(","):
            part = item.strip()
            if not part:
                continue
            if " as " in part:
                target = part.split(" as ", 1)[0].strip()
                names.append(target)
            else:
                names.append(part)

    # Handle default import: "foo" or "foo, { bar }"
    clean = re.sub(r"\{[^}]*\}", "", clause).strip().strip(",")
    for item in clean.split(","):
        part = item.strip()
        if not part:
            continue
        if part.startswith("* as "):
            names.append(part.split("* as ", 1)[1].strip())
        elif "*" not in part and not part.startswith("type "):
            names.append(part.replace("type ", "").strip())

    return [n for n in names if n]


def extract_python_imports(
    file_path: str,
    source: str,
) -> tuple[list[ImportReference], list[IndexDiagnostic]]:
    """Extract Python imports using the standard library ast module safely."""
    imports: list[ImportReference] = []
    diagnostics: list[IndexDiagnostic] = []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as err:
        diagnostics.append(
            IndexDiagnostic(
                file=file_path,
                line=err.lineno,
                level="error",
                message=f"SyntaxError parsing imports: {err.msg}",
                reason="syntax_error",
            )
        )
        return imports, diagnostics

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    ImportReference(
                        file=file_path,
                        module=alias.name,
                        names=[alias.name] if not alias.asname else [alias.asname],
                        alias=alias.asname,
                        is_relative=False,
                        is_dynamic=False,
                        line=node.lineno,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            is_relative = level > 0
            mod_prefix = "." * level if is_relative else ""
            module_name = f"{mod_prefix}{node.module or ''}"
            imported_names = [alias.name for alias in node.names]
            imports.append(
                ImportReference(
                    file=file_path,
                    module=module_name,
                    names=imported_names,
                    alias=node.names[0].asname if len(node.names) == 1 else None,
                    is_relative=is_relative,
                    is_dynamic=False,
                    line=node.lineno,
                )
            )

    return imports, diagnostics


def extract_ts_js_imports(
    file_path: str,
    source: str,
) -> tuple[list[ImportReference], list[IndexDiagnostic]]:
    """Extract JS/TS imports and requires using robust pattern matching."""
    imports: list[ImportReference] = []
    diagnostics: list[IndexDiagnostic] = []

    lines = source.splitlines()

    def get_line_no(char_pos: int) -> int:
        return source[:char_pos].count("\n") + 1

    # Static imports
    for match in _TS_IMPORT_STATIC.finditer(source):
        module = match.group("module")
        clause = match.group("clause")
        names = _clean_names(clause)
        line = get_line_no(match.start())
        imports.append(
            ImportReference(
                file=file_path,
                module=module,
                names=names,
                is_relative=module.startswith("."),
                is_dynamic=False,
                line=line,
            )
        )

    # Export ... from "..."
    for match in _TS_EXPORT_FROM.finditer(source):
        module = match.group("module")
        clause = match.group("clause")
        names = _clean_names(clause)
        line = get_line_no(match.start())
        imports.append(
            ImportReference(
                file=file_path,
                module=module,
                names=names,
                is_relative=module.startswith("."),
                is_dynamic=False,
                line=line,
            )
        )

    # Require with string literal
    for match in _TS_REQUIRE.finditer(source):
        module = match.group("module")
        clause = match.group("clause")
        names = _clean_names(clause) if clause else []
        line = get_line_no(match.start())
        imports.append(
            ImportReference(
                file=file_path,
                module=module,
                names=names,
                is_relative=module.startswith("."),
                is_dynamic=False,
                line=line,
            )
        )

    # Dynamic imports
    for match in _TS_DYNAMIC_IMPORT.finditer(source):
        arg = match.group("arg").strip()
        line = get_line_no(match.start())
        # Check if argument is literal or expression
        is_literal = (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'"))
        module_name = arg.strip("'\"") if is_literal else "<dynamic>"
        imports.append(
            ImportReference(
                file=file_path,
                module=module_name,
                names=[],
                is_relative=module_name.startswith("."),
                is_dynamic=True,
                line=line,
            )
        )
        if not is_literal:
            diagnostics.append(
                IndexDiagnostic(
                    file=file_path,
                    line=line,
                    level="info",
                    message=f"Dynamic import expression cannot be statically resolved: import({arg})",
                    reason="dynamic_import",
                )
            )

    # Dynamic require
    for match in _TS_DYNAMIC_REQUIRE.finditer(source):
        arg = match.group("arg").strip()
        line = get_line_no(match.start())
        imports.append(
            ImportReference(
                file=file_path,
                module="<dynamic>",
                names=[],
                is_relative=False,
                is_dynamic=True,
                line=line,
            )
        )
        diagnostics.append(
            IndexDiagnostic(
                file=file_path,
                line=line,
                level="info",
                message=f"Dynamic require expression cannot be statically resolved: require({arg})",
                reason="dynamic_import",
            )
        )

    return imports, diagnostics


def resolve_import_path(
    from_file: str,
    module: str,
    available_files: Sequence[str] | set[str],
) -> str | None:
    """Safely resolve an import string to a local repository file if possible."""
    if not module or module == "<dynamic>":
        return None

    files_set = set(available_files)
    from_dir = posixpath.dirname(from_file)

    # Relative imports
    if module.startswith("."):
        # Normalize relative path
        target_base = posixpath.normpath(posixpath.join(from_dir, module))

        # Direct file check
        if target_base in files_set:
            return target_base

        # TypeScript / JavaScript extension resolution
        for ext in (".ts", ".tsx", ".js", ".jsx", ".d.ts"):
            candidate = target_base + ext
            if candidate in files_set:
                return candidate

        # Index file resolution
        for idx in ("/index.ts", "/index.tsx", "/index.js", "/index.jsx"):
            candidate = target_base + idx
            if candidate in files_set:
                return candidate

        # Python relative resolution
        candidate_py = target_base + ".py"
        if candidate_py in files_set:
            return candidate_py
        candidate_py_init = posixpath.join(target_base, "__init__.py")
        if candidate_py_init in files_set:
            return candidate_py_init

        # Check Python package-style relative imports (e.g. .models.user)
        leading_dots = len(module) - len(module.lstrip("."))
        remainder = module[leading_dots:].replace(".", "/")
        rel_prefix = "." if leading_dots == 1 else ".." + ("/.." * (leading_dots - 2))
        py_target = posixpath.normpath(posixpath.join(from_dir, posixpath.join(rel_prefix, remainder) if remainder else rel_prefix))
        if py_target + ".py" in files_set:
            return py_target + ".py"
        if posixpath.join(py_target, "__init__.py") in files_set:
            return posixpath.join(py_target, "__init__.py")

    else:
        # Check non-relative Python module (e.g. auth.service -> auth/service.py)
        as_path = module.replace(".", "/")
        for candidate in (
            f"{as_path}.py",
            f"{as_path}/__init__.py",
            f"src/{as_path}.py",
            f"src/{as_path}/__init__.py",
        ):
            if candidate in files_set:
                return candidate

        # Check TS path alias or root path if matches exactly
        for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
            candidate = as_path + ext
            if candidate in files_set:
                return candidate

    return None


__all__ = [
    "extract_python_imports",
    "extract_ts_js_imports",
    "resolve_import_path",
]
