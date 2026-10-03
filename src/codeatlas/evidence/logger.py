"""Source-safe structured JSONL evidence logging."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


SAFE_FIELDS = frozenset({
    "run_id", "repository", "base_ref", "head_ref", "base_commit", "head_commit",
    "file", "files", "language", "languages", "count", "status", "provider", "duration_ms",
    "error_type", "message", "finding_id", "tool", "test", "reason", "decision", "packet_id",
    "proposal_id", "patch_hash", "sandbox_id", "operation", "validation_status",
    # Live-provider metadata: identifiers and aggregates only, never payloads.
    "model", "latency_ms", "retries", "request_id", "input_tokens", "output_tokens",
    "estimated_cost", "dry_run", "budget", "safety_event",
    # Patch-suggestion metadata: identifiers and decisions only, never diffs.
    "suggestion_id", "risk_level", "valid", "applies_cleanly",
    # Isolated-validation metadata (Phase 7C): identifiers and aggregates only.
    "resulting_diff_hash", "retained",
    "network_isolation_verified",
})


class EvidenceLogger:
    """Append lifecycle events while allowing only explicitly safe metadata."""

    def __init__(self, destination: str | Path | TextIO) -> None:
        self._owned = not hasattr(destination, "write")
        if self._owned:
            path = Path(destination).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = open(path, "a", encoding="utf-8")
        else:
            self._stream = destination

    def __enter__(self) -> "EvidenceLogger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._stream.close()

    def emit(self, event: str, **details: Any) -> None:
        safe = {key: value for key, value in details.items() if key in SAFE_FIELDS}
        record = {"event": event, "timestamp": datetime.now(timezone.utc).isoformat(), **safe}
        self._stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self._stream.flush()

    def run_started(self, **details: Any) -> None:
        self.emit("run_started", **details)

    def snapshot_created(self, **details: Any) -> None:
        self.emit("snapshot_created", **details)

    def repository_validated(self, **details: Any) -> None:
        self.emit("repository_validated", **details)

    def commits_resolved(self, **details: Any) -> None:
        self.emit("commits_resolved", **details)

    def diff_extracted(self, **details: Any) -> None:
        self.emit("diff_extracted", **details)

    def languages_detected(self, **details: Any) -> None:
        self.emit("languages_detected", **details)

    def file_analyzed(self, **details: Any) -> None:
        self.emit("file_analyzed", **details)

    def finding_emitted(self, **details: Any) -> None:
        self.emit("finding_emitted", **details)

    def run_completed(self, **details: Any) -> None:
        self.emit("run_completed", **details)

    def run_failed(self, **details: Any) -> None:
        self.emit("run_failed", **details)


__all__ = ["EvidenceLogger"]
