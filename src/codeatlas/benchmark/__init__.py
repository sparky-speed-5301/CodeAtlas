"""Deterministic repair-quality benchmarking primitives."""

from .provider_comparison import (
    ProviderComparisonControl,
    ProviderComparisonObservation,
    ProviderComparisonReport,
    ProviderPatchLimits,
    load_provider_comparisons,
    score_provider_comparisons,
)
from .repair import (
    RepairBenchmarkCase,
    RepairBenchmarkResult,
    RepairBenchmarkSummary,
    benchmark_candidate,
    load_repository_cases,
    run_deterministic_benchmark,
    run_repository_benchmark,
)
from .repair_metrics import (
    RepairBenchmarkReport,
    RepairObservation,
    score_repair_benchmark,
)
from .repository import (
    ExpectedAnchor,
    MetricResult,
    ObservedFinding,
    PatchScopeExpectation,
    RepositoryBenchmarkCase,
    RepositoryBenchmarkSuite,
    ReviewBenchmarkReport,
    ReviewObservation,
    TestCommandPolicy,
    load_benchmark_suite,
    load_review_observations,
    score_review_benchmark,
)

__all__ = [
    "ExpectedAnchor",
    "MetricResult",
    "ObservedFinding",
    "PatchScopeExpectation",
    "ProviderComparisonControl",
    "ProviderComparisonObservation",
    "ProviderComparisonReport",
    "ProviderPatchLimits",
    "RepairBenchmarkCase",
    "RepairBenchmarkReport",
    "RepairBenchmarkResult",
    "RepairBenchmarkSummary",
    "RepairObservation",
    "RepositoryBenchmarkCase",
    "RepositoryBenchmarkSuite",
    "ReviewBenchmarkReport",
    "ReviewObservation",
    "TestCommandPolicy",
    "benchmark_candidate",
    "load_benchmark_suite",
    "load_provider_comparisons",
    "load_repository_cases",
    "load_review_observations",
    "run_deterministic_benchmark",
    "run_repository_benchmark",
    "score_provider_comparisons",
    "score_repair_benchmark",
    "score_review_benchmark",
]
