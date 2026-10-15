"""Opt-in reliability accounting; checkpoints do not interrupt running work."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class BudgetStatus(str, Enum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    REJECTED_BY_BUDGET = "rejected_by_budget"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class BudgetResult(Generic[T]):
    status: BudgetStatus
    reason: str = ""
    value: T | None = None
    truncated: bool = False
    observed: int | float | None = None
    limit: int | float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", BudgetStatus(self.status))


@dataclass(frozen=True)
class ReliabilityBudgets:
    """Inclusive accounting limits; None means no configured limit, not enforcement."""

    wall_time_seconds: float | None = None
    max_files: int | None = None
    max_context_bytes: int | None = None
    max_output_bytes: int | None = None
    max_artifact_bytes: int | None = None
    max_artifacts: int | None = None
    max_db_bytes: int | None = None
    max_db_records: int | None = None
    max_retries: int | None = None
    max_concurrent: int = 1
    max_queued: int = 0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if value is None:
                continue
            if name == "wall_time_seconds":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError("Invalid wall-time budget")
            elif type(value) is not int or value < (1 if name == "max_concurrent" else 0):
                raise ValueError("Invalid count or byte budget")


# These require runtime owners or platform isolation, not accounting checks.
UNAVAILABLE_CAPABILITIES = frozenset({
    "service_admission", "service_cancellation", "command_cancellation",
    "repository_file_accounting", "context_byte_accounting", "database_record_accounting",
    "command_capture_memory", "process_tree_termination", "memory_isolation",
    "process_isolation", "network_isolation", "database_total_bytes",
    "database_query_deadline", "provider_total_deadline", "provider_retry_accounting",
})


def capability_result(capability: str) -> BudgetResult[None]:
    """Never infer runtime enforcement from configured or inspected limits."""
    return BudgetResult(BudgetStatus.UNAVAILABLE, "capability_not_integrated")


class BudgetSession:
    """Thread-safe, atomic reservations and cooperative cancellation/deadlines."""

    def __init__(
        self, budgets: ReliabilityBudgets, *,
        clock: Callable[[], float] = time.monotonic,
        cancellation: threading.Event | None = None,
    ) -> None:
        self.budgets = budgets
        self._clock = clock
        self._started = clock()
        self._cancellation = cancellation if cancellation is not None else threading.Event()
        self._used: dict[str, int] = {}
        self._lock = threading.RLock()

    def cancel(self) -> None:
        self._cancellation.set()

    def checkpoint(self) -> BudgetResult[None]:
        if self._cancellation.is_set():
            return BudgetResult(BudgetStatus.CANCELLED, "cancellation_requested")
        elapsed = self._clock() - self._started
        limit = self.budgets.wall_time_seconds
        if limit is not None and elapsed >= limit:
            return BudgetResult(BudgetStatus.TIMED_OUT, "wall_time_exhausted", observed=elapsed, limit=limit)
        return BudgetResult(BudgetStatus.COMPLETED)

    def reserve(self, amounts: Mapping[str, int]) -> BudgetResult[None]:
        """Reject whole reservations without consuming any partial allowance."""
        for dimension, amount in amounts.items():
            if dimension not in vars(self.budgets) or dimension in {"wall_time_seconds", "max_concurrent", "max_queued", "max_retries"}:
                return BudgetResult(BudgetStatus.UNAVAILABLE, "unknown_accounting_dimension")
            if type(amount) is not int or amount < 0:
                return BudgetResult(BudgetStatus.FAILED, "invalid_reservation")
        with self._lock:
            result = self.checkpoint()
            if result.status != BudgetStatus.COMPLETED:
                return result
            for dimension, amount in amounts.items():
                observed = self._used.get(dimension, 0) + amount
                limit = getattr(self.budgets, dimension)
                if limit is not None and observed > limit:
                    return BudgetResult(BudgetStatus.REJECTED_BY_BUDGET, dimension, observed=observed, limit=limit)
            for dimension, amount in amounts.items():
                self._used[dimension] = self._used.get(dimension, 0) + amount
            return BudgetResult(BudgetStatus.COMPLETED)

    def begin_attempt(self) -> BudgetResult[None]:
        """One initial attempt plus max_retries; callers own retry execution."""
        with self._lock:
            result = self.checkpoint()
            if result.status != BudgetStatus.COMPLETED:
                return result
            attempts = self._used.get("attempts", 0)
            retries = self.budgets.max_retries
            if retries is not None and attempts >= retries + 1:
                return BudgetResult(BudgetStatus.REJECTED_BY_BUDGET, "retry_budget_exhausted", observed=attempts + 1, limit=retries + 1)
            self._used["attempts"] = attempts + 1
            return BudgetResult(BudgetStatus.COMPLETED)

    @property
    def usage(self) -> dict[str, int]:
        with self._lock:
            return dict(self._used)


@dataclass(eq=False)
class AdmissionLease:
    """Local admission token; it neither schedules nor starts service work."""

    _gate: AdmissionGate = field(repr=False)
    queued: bool = False
    closed: bool = False

    def promote(self) -> BudgetResult[None]:
        return self._gate._promote(self)

    def close(self) -> BudgetResult[None]:
        return self._gate._release(self)


class AdmissionGate:
    """Bound local active/queued leases independently of the service scheduler."""

    def __init__(self, session: BudgetSession) -> None:
        self.session = session
        self._active = 0
        self._queued = 0
        self._lock = threading.Lock()

    def acquire(self) -> BudgetResult[AdmissionLease]:
        with self._lock:
            check = self.session.checkpoint()
            if check.status != BudgetStatus.COMPLETED:
                return BudgetResult(check.status, check.reason)
            queued = self._active >= self.session.budgets.max_concurrent
            if queued and self._queued >= self.session.budgets.max_queued:
                return BudgetResult(BudgetStatus.REJECTED_BY_BUDGET, "admission_capacity_exhausted")
            if queued:
                self._queued += 1
            else:
                self._active += 1
            return BudgetResult(BudgetStatus.COMPLETED, value=AdmissionLease(self, queued))

    def _promote(self, lease: AdmissionLease) -> BudgetResult[None]:
        with self._lock:
            if lease.closed:
                return BudgetResult(BudgetStatus.FAILED, "lease_closed")
            check = self.session.checkpoint()
            if check.status != BudgetStatus.COMPLETED:
                return check
            if not lease.queued:
                return BudgetResult(BudgetStatus.COMPLETED)
            if self._active >= self.session.budgets.max_concurrent:
                return BudgetResult(BudgetStatus.REJECTED_BY_BUDGET, "concurrency_exhausted")
            self._queued -= 1
            self._active += 1
            lease.queued = False
            return BudgetResult(BudgetStatus.COMPLETED)

    def _release(self, lease: AdmissionLease) -> BudgetResult[None]:
        with self._lock:
            if not lease.closed:
                if lease.queued:
                    self._queued -= 1
                else:
                    self._active -= 1
                lease.closed = True
            return BudgetResult(BudgetStatus.COMPLETED)


def truncate_utf8(text: str, max_bytes: int) -> BudgetResult[str]:
    """Bound retained UTF-8 bytes without emitting partial code points."""
    if type(max_bytes) is not int or max_bytes < 0:
        return BudgetResult(BudgetStatus.FAILED, "invalid_output_budget")
    raw = text.encode("utf-8", errors="replace")
    truncated = len(raw) > max_bytes
    value = raw[:max_bytes].decode("utf-8", errors="ignore") if truncated else raw.decode("utf-8")
    return BudgetResult(BudgetStatus.COMPLETED, value=value, truncated=truncated, observed=len(raw), limit=max_bytes)
