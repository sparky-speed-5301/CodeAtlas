"""Models shared by deterministic analyzers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from re import Pattern

from codeatlas.git.models import Diff


@dataclass(frozen=True)
class AnalysisContext:
    """The immutable inputs available to an analyzer."""

    snapshot: object
    diff: Diff
    allow_patterns: tuple[str | Pattern[str], ...] = ()
    ignore_placeholders: bool = True
    min_confidence: float = 0.0

    @property
    def snapshot_path(self) -> Path:
        path = getattr(self.snapshot, "path", self.snapshot)
        return Path(str(path))


@dataclass(frozen=True)
class AnalyzerOptions:
    """Configuration accepted by analyzers and the registry."""

    allow_patterns: tuple[str | Pattern[str], ...] = ()
    ignore_placeholders: bool = True
    min_confidence: float = 0.0


__all__ = ["AnalysisContext", "AnalyzerOptions"]
