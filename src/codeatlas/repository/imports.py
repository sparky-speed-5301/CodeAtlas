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

_GO_IMPORT = re.compile(r'''^\s*import\s+(?:(?P<alias>[A-Za-z_]\w*)\s+)?["'](?P<module>[^"']+)["']\s*$''')
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?P<static>static\s+)?(?P<module>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;\s*$")
_RUST_USE = re.compile(r"^\s*use\s+(?P<module>(?:crate|self|super|[A-Za-z_]\w*)(?:::[A-Za-z_]\w*)*)(?:\s*::\s*\{(?P<braced>[^}]+)\})?\s*;\s*$")


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


def extract_compiled_imports(
    file_path: str,
    source: str,
    language: str,
) -> tuple[list[ImportReference], list[IndexDiagnostic]]:
    """Extract conservative import/use declarations for Go, Java, and Rust."""
    imports: list[ImportReference] = []
    diagnostics: list[IndexDiagnostic] = []
    lines = source.splitlines()

    if language == "go":
        in_block = False
        for line_no, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("import ("):
                in_block = True
                continue
            if in_block and stripped == ")":
                in_block = False
                continue
            match = _GO_IMPORT.match(line) if in_block or stripped.startswith("import ") else None
            if not match:
                continue
            imports.append(
                ImportReference(
                    file=file_path,
                    module=match.group("module"),
                    names=[match.group("alias")] if match.group("alias") else [],
                    alias=match.group("alias"),
                    line=line_no,
                )
            )
    elif language == "java":
        for line_no, line in enumerate(lines, start=1):
            match = _JAVA_IMPORT.match(line)
            if not match:
                continue
            parts = match.group("module").split(".")
            imports.append(
                ImportReference(
                    file=file_path,
                    module=match.group("module"),
                    names=[parts[-1]],
                    line=line_no,
                )
            )
    elif language == "rust":
        for line_no, line in enumerate(lines, start=1):
            match = _RUST_USE.match(line)
            if not match:
                continue
            module = match.group("module")
            names = [item.strip().split(" as ", 1)[0].strip() for item in (match.group("braced") or "").split(",") if item.strip()]
            if not names:
                names = [module.rsplit("::", 1)[-1]]
            imports.append(
                ImportReference(
                    file=file_path,
                    module=module,
                    names=names,
                    line=line_no,
                )
            )

    return imports, diagnostics


def _unique_candidates(candidates: list[str], available_files: set[str]) -> list[str]:
    """Normalize and retain available candidates in deterministic order."""
    result: list[str] = []
    for candidate in candidates:
        normalized = posixpath.normpath(candidate).lstrip("./")
        if normalized in available_files and normalized not in result:
            result.append(normalized)
    return result


def _resolve_candidates(
    from_file: str,
    module: str,
    available_files: Sequence[str] | set[str],
    language: str | None,
) -> list[str]:
    files_set = {item.replace("\\", "/").lstrip("./") for item in available_files}
    from_dir = posixpath.dirname(from_file)
    candidates: list[str] = []

    if language == "rust" or (language is None and from_file.endswith(".rs")):
        rust_module = module
        if rust_module.startswith("crate::"):
            parts = rust_module.split("::")[1:]
            module_bases = [posixpath.join("src", *parts[:index]) for index in range(len(parts), 0, -1)]
        elif rust_module.startswith("self::"):
            parts = rust_module.split("::")[1:]
            module_bases = [posixpath.join(from_dir, *parts[:index]) for index in range(len(parts), 0, -1)]
        elif rust_module.startswith("super::"):
            parts = rust_module.split("::")
            base_dir = posixpath.dirname(from_dir)
            module_bases = [posixpath.join(base_dir, *parts[1:index]) for index in range(len(parts), 1, -1)]
        else:
            module_bases = []
        for base in module_bases:
            candidates.extend((base + ".rs", posixpath.join(base, "mod.rs")))
        return _unique_candidates(candidates, files_set)

    if language == "go" or (language is None and from_file.endswith(".go")):
        module_path = module.rstrip("/")
        suffix = "/" + module_path
        return sorted(
            path
            for path in files_set
            if path.endswith(".go")
            and not path.endswith("_test.go")
            and (posixpath.dirname(path) == module_path or posixpath.dirname(path).endswith(suffix))
        )

    if module.startswith("."):
        if language == "python" or (language is None and from_file.endswith(".py")):
            level = len(module) - len(module.lstrip("."))
            remainder = module[level:].replace(".", "/")
            base = from_dir
            for _ in range(max(level - 1, 0)):
                base = posixpath.dirname(base)
            target_base = posixpath.join(base, remainder) if remainder else base
            candidates.extend((target_base, target_base + ".py", posixpath.join(target_base, "__init__.py")))
        else:
            target_base = posixpath.normpath(posixpath.join(from_dir, module))
            candidates.append(target_base)
            candidates.extend(target_base + ext for ext in (".ts", ".tsx", ".js", ".jsx", ".d.ts", ".py"))
            candidates.extend(posixpath.join(target_base, name) for name in ("index.ts", "index.tsx", "index.js", "index.jsx", "__init__.py"))
    else:
        as_path = module.replace(".", "/")
        if language == "java" or (language is None and from_file.endswith(".java")):
            candidates.extend((as_path + ".java", posixpath.join(as_path, "package-info.java")))
        else:
            candidates.extend(
                (
                    f"{as_path}.py",
                    f"{as_path}/__init__.py",
                    f"src/{as_path}.py",
                    f"src/{as_path}/__init__.py",
                    as_path,
                    f"{as_path}.ts",
                    f"{as_path}.tsx",
                    f"{as_path}.js",
                    f"{as_path}.jsx",
                    f"{as_path}/index.ts",
                    f"{as_path}/index.js",
                )
            )

    return _unique_candidates(candidates, files_set)


def resolve_import(
    from_file: str,
    module: str,
    available_files: Sequence[str] | set[str],
    *,
    language: str | None = None,
    is_dynamic: bool = False,
) -> tuple[str | None, str, str | None]:
    """Resolve an import with explicit resolved, unresolved, ambiguous, or unsupported state."""
    if is_dynamic or not module or module == "<dynamic>":
        return None, "unsupported", "dynamic_import"
    candidates = _resolve_candidates(from_file, module, available_files, language)
    if not candidates:
        return None, "unresolved", "external_or_unresolved"
    if len(candidates) > 1:
        return None, "ambiguous", "multiple_local_candidates"
    return candidates[0], "resolved", None


def resolve_import_path(
    from_file: str,
    module: str,
    available_files: Sequence[str] | set[str],
) -> str | None:
    """Safely resolve an import string to a local repository file if possible."""
    candidates = _resolve_candidates(from_file, module, available_files, None)
    return candidates[0] if candidates else None


__all__ = [
    "extract_python_imports",
    "extract_ts_js_imports",
    "extract_compiled_imports",
    "resolve_import",
    "resolve_import_path",
]
