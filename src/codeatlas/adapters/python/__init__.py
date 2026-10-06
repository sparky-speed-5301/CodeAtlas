"""Python repair adapter using the existing standard-library AST parser."""

from codeatlas.adapters.base import BaseLanguageAdapter, LanguageParseResult
from codeatlas.repository.symbols import extract_python_symbols


class PythonAdapter(BaseLanguageAdapter):
    language = "python"

    def parse_modified_file(self, path: str, source: str) -> LanguageParseResult:
        try:
            symbols, diagnostics = extract_python_symbols(path, source)
        except (ValueError, RecursionError):
            return LanguageParseResult(syntax_valid=False, errors=("Python syntax parsing failed",))
        errors = tuple(f"Python syntax error at line {d.line}" for d in diagnostics if d.level == "error")
        return LanguageParseResult(symbols=tuple(symbols), syntax_valid=not errors, errors=errors)


__all__ = ["PythonAdapter"]
