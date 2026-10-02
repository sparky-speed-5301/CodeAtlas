"""Conservative extension-based source language detection."""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class Language(str, Enum):
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    JAVA = "java"
    GO = "go"
    RUST = "rust"
    UNKNOWN = "unknown"


_EXTENSIONS = {
    ".ts": Language.TYPESCRIPT, ".tsx": Language.TYPESCRIPT,
    ".js": Language.JAVASCRIPT, ".jsx": Language.JAVASCRIPT, ".mjs": Language.JAVASCRIPT, ".cjs": Language.JAVASCRIPT,
    ".py": Language.PYTHON, ".java": Language.JAVA, ".go": Language.GO, ".rs": Language.RUST,
}


def detect_language(path: str | Path) -> Language:
    return _EXTENSIONS.get(Path(path).suffix.lower(), Language.UNKNOWN)
