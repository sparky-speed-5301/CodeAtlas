"""Language adapter interfaces and implementations."""

from codeatlas.core.language import detect_language
from .base import LanguageAdapter, LanguageParseResult
from .javascript import JavaScriptAdapter
from .python import PythonAdapter
from .typescript import TypeScriptAdapter


def select_language_adapter(path: str) -> LanguageAdapter | None:
    adapters = {"python": PythonAdapter, "javascript": JavaScriptAdapter, "typescript": TypeScriptAdapter}
    adapter = adapters.get(detect_language(path).value)
    return adapter() if adapter else None


__all__ = ["LanguageAdapter", "LanguageParseResult", "PythonAdapter", "JavaScriptAdapter",
           "TypeScriptAdapter", "select_language_adapter"]
