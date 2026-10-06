"""TypeScript repair adapter sharing the existing JavaScript/TypeScript parser."""

from codeatlas.adapters.javascript import JavaScriptAdapter


class TypeScriptAdapter(JavaScriptAdapter):
    language = "typescript"


__all__ = ["TypeScriptAdapter"]
