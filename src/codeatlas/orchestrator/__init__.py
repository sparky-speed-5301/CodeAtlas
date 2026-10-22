"""Review orchestration support types."""

from .manifest import RunManifest
from .support import OrchestrationError, ProviderError, ReviewRequest, ReviewResult
from .review import ReviewResult as CompletedReview, run_review
from .repair import RepairOrchestrator, RepairRejected, record_repair_result
from .context import RepairContextBuildError, RepairContextBuildResult, RepairContextBuilder
from .repair_models import (
    REPAIR_POLICY_VERSION,
    RepairContext,
    RepairLimits,
    RepairRepositoryState,
    RepairResult,
    compute_context_hash,
)

__all__ = [
    "RunManifest", "OrchestrationError", "ProviderError", "ReviewRequest", "ReviewResult",
    "CompletedReview", "run_review", "RepairOrchestrator", "RepairRejected", "RepairContext", "RepairLimits",
    "RepairRepositoryState", "RepairResult", "REPAIR_POLICY_VERSION",
    "record_repair_result", "RepairContextBuilder", "RepairContextBuildError",
    "RepairContextBuildResult", "compute_context_hash", "RepairAttempt", "RepairLifecycleEvent", "RepairLoop",
    "RepairLoopResult", "RepairVerificationEvidence", "RepairVerificationLoop",
]


def __getattr__(name: str):
    """Load the provider-dependent loop lazily to avoid an import cycle."""
    if name in {"RepairAttempt", "RepairLifecycleEvent", "RepairLoop", "RepairLoopResult", "RepairVerificationEvidence", "RepairVerificationLoop"}:
        from .repair_loop import (
            RepairAttempt,
            RepairLifecycleEvent,
            RepairLoop,
            RepairLoopResult,
            RepairVerificationEvidence,
            RepairVerificationLoop,
        )
        return {
            "RepairAttempt": RepairAttempt,
            "RepairLifecycleEvent": RepairLifecycleEvent,
            "RepairLoop": RepairLoop,
            "RepairLoopResult": RepairLoopResult,
            "RepairVerificationEvidence": RepairVerificationEvidence,
            "RepairVerificationLoop": RepairVerificationLoop,
        }[name]
    raise AttributeError(name)
