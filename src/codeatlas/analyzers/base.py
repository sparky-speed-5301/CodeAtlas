"""Analyzer protocols."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from codeatlas.findings import Finding

from .models import AnalysisContext


class Analyzer(Protocol):
    """A deterministic analyzer operating on a snapshot and a diff."""

    name: str
    supported_languages: set[str]

    def analyze(self, context: AnalysisContext) -> Sequence[Finding]:
        ...


__all__ = ["Analyzer"]
