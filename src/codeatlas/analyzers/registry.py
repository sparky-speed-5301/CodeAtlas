"""Analyzer registry."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from codeatlas.findings import Finding
from codeatlas.git.models import Diff

from .base import Analyzer
from .models import AnalysisContext
from .secrets import HardcodedSecretAnalyzer
from .sensitive import SensitiveDataExposureAnalyzer


class AnalyzerRegistry:
    def __init__(self, analyzers: Iterable[Analyzer] = ()) -> None:
        self._analyzers: dict[str, Analyzer] = {analyzer.name: analyzer for analyzer in analyzers}

    def register(self, analyzer: Analyzer) -> None:
        self._analyzers[analyzer.name] = analyzer

    def get(self, name: str) -> Analyzer:
        return self._analyzers[name]

    def analyze(self, context: AnalysisContext | object, diff: Diff | None = None) -> Sequence[Finding]:
        if not isinstance(context, AnalysisContext):
            if diff is None:
                raise TypeError("analyze requires AnalysisContext or snapshot and Diff")
            context = AnalysisContext(context, diff)
        findings: list[Finding] = []
        for analyzer in self._analyzers.values():
            findings.extend(analyzer.analyze(context))
        return tuple(findings)

    def __iter__(self):
        return iter(self._analyzers.values())


def default_registry() -> AnalyzerRegistry:
    return AnalyzerRegistry((HardcodedSecretAnalyzer(), SensitiveDataExposureAnalyzer()))


__all__ = ["AnalyzerRegistry", "default_registry"]
