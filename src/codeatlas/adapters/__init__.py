"""Language adapter interfaces and implementations."""

from codeatlas.core.language import detect_language
from .base import LanguageAdapter, LanguageCapabilityReport, LanguageParseResult
from .go import GoAdapter
from .java import JavaAdapter
from .javascript import JavaScriptAdapter
from .python import PythonAdapter
from .rust import RustAdapter
from .typescript import TypeScriptAdapter


def select_language_adapter(path: str) -> LanguageAdapter | None:
    adapters = {
        "python": PythonAdapter, "javascript": JavaScriptAdapter, "typescript": TypeScriptAdapter,
        "go": GoAdapter, "java": JavaAdapter, "rust": RustAdapter,
    }
    adapter = adapters.get(detect_language(path).value)
    return adapter() if adapter else None


def language_capability_reports(root=None) -> tuple[LanguageCapabilityReport, ...]:
    """Report capabilities for every supported language without repository reads."""
    adapters = (PythonAdapter(), JavaScriptAdapter(), TypeScriptAdapter(), GoAdapter(), JavaAdapter(), RustAdapter())
    return tuple(adapter.capability_report(root) for adapter in adapters)


__all__ = ["LanguageAdapter", "LanguageCapabilityReport", "LanguageParseResult", "PythonAdapter",
           "JavaScriptAdapter", "TypeScriptAdapter", "GoAdapter", "JavaAdapter", "RustAdapter",
           "language_capability_reports", "select_language_adapter"]
