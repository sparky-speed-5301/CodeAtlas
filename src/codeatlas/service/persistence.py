"""Versioned, scoped SQLite storage for bounded review metadata.

One service owns a database at a time. Clients share that service through the
API; they never read SQLite directly. Recovery never executes repository code.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codeatlas.orchestrator.repair_models import (
    REPAIR_POLICY_VERSION,
    repair_payload_is_safe,
)

SCHEMA_VERSION = 2
PAYLOAD_VERSION = "5.1"
KINDS = (
    "findings", "finding_feedback", "fix_proposals", "validation_attempts",
    "validation_evidence", "apply_events", "revert_events", "lifecycle_events",
    "provider_metadata", "capability_snapshots", "safe_errors",
    "patch_proposals",
)


class PersistenceError(RuntimeError):
    """A fixed reason code, never a SQLite diagnostic or input value."""

    def __init__(self, code: str = "persistence_failed") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PersistenceConfig:
    path: Path
    max_runs: int = 500
    max_age_seconds: int = 90 * 86400
    max_payload_bytes: int = 512_000
    max_records_per_run: int = 5000

    def __post_init__(self) -> None:
        for value, low, high in (
            (self.max_runs, 1, 10000), (self.max_age_seconds, 60, 3650 * 86400),
            (self.max_payload_bytes, 16384, 1_048_576), (self.max_records_per_run, 10, 10000),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid persistence retention or payload bound")


def now() -> str:
    return datetime.now(UTC).isoformat()


def repository_id(path: str | Path) -> str:
    canonical = os.path.normcase(str(Path(path).expanduser().resolve()))
    return "repo-" + hashlib.sha256(canonical.encode()).hexdigest()


def default_database_path() -> Path:
    """User-scoped default used by the long-running service command."""
    return Path.home() / ".codeatlas" / "service.sqlite3"


def canonical_json(value: Any, max_bytes: int = 512_000) -> str:
    """No truncation: a hashed artifact either survives exactly or is rejected."""
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise PersistenceError("invalid_persistence_payload") from None
    if len(text.encode()) > max_bytes:
        raise PersistenceError("persistence_payload_too_large")
    return text


# All entity rows carry the enclosing run identity, revisions, schema/policy
# versions and timestamps. Compound keys prevent deterministic proposal IDs
# from colliding between runs or between repositories with identical content.
MIGRATIONS = ((1, (
    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)",
    """CREATE TABLE repositories(
        repository_id TEXT PRIMARY KEY, canonical_path TEXT NOT NULL UNIQUE,
        schema_version TEXT NOT NULL, policy_version TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE review_runs(
        repository_id TEXT NOT NULL REFERENCES repositories(repository_id) ON DELETE CASCADE,
        run_id TEXT NOT NULL UNIQUE, base_commit TEXT NOT NULL, head_commit TEXT NOT NULL,
        schema_version TEXT NOT NULL, policy_version TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        status TEXT NOT NULL, recovery_state TEXT NOT NULL, payload TEXT NOT NULL,
        payload_hash TEXT NOT NULL, PRIMARY KEY(repository_id, run_id))""",
    """CREATE TABLE records(
        repository_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL,
        entity_id TEXT NOT NULL, finding_id TEXT NOT NULL, proposal_id TEXT NOT NULL,
        base_commit TEXT NOT NULL, head_commit TEXT NOT NULL, patch_hash TEXT NOT NULL,
        schema_version TEXT NOT NULL, policy_version TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
        PRIMARY KEY(repository_id, run_id, kind, entity_id),
        FOREIGN KEY(repository_id, run_id) REFERENCES review_runs(repository_id, run_id) ON DELETE CASCADE)""",
    "CREATE INDEX records_scope ON records(repository_id, run_id, finding_id, proposal_id)",
    *(f"CREATE VIEW {kind} AS SELECT * FROM records WHERE kind='{kind}'" for kind in KINDS if kind != "patch_proposals"),
)), (2, (
    "CREATE VIEW patch_proposals AS SELECT * FROM records WHERE kind='patch_proposals'",
)))


class SQLitePersistence:
    def __init__(self, config: PersistenceConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._owner: Any = None
        self._closed = False
        path = config.path.expanduser().resolve()
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise PersistenceError("database_unavailable") from None
        try:
            # Kernel locks disappear after a crash. A leftover lock file is safe.
            self._owner = open(str(path) + ".lock", "a+b")  # noqa: SIM115 - lock must remain held
            if os.name == "nt":  # exercised only on Windows installations
                import msvcrt
                self._owner.seek(0)
                self._owner.write(b"\0")
                self._owner.flush()
                self._owner.seek(0)
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(  # type: ignore[attr-defined]
                    self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB  # type: ignore[attr-defined]
                )
        except OSError:
            if self._owner is not None:
                self._owner.close()
            raise PersistenceError("database_in_use") from None
        try:
            self._db = sqlite3.connect(str(path), timeout=5, check_same_thread=False, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA secure_delete=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA max_page_count=262144")  # hard 1 GiB at default page size
            self._migrate()
            self._enforce_run_retention()
        except PersistenceError as error:
            if hasattr(self, "_db"):
                self._db.close()
            self._owner.close()
            if error.code in {"unsupported_database_version", "migration_validation_failed"}:
                raise
            raise PersistenceError("migration_validation_failed") from None
        except Exception:  # noqa: BLE001 - startup must expose one safe persistence reason
            if hasattr(self, "_db"):
                self._db.close()
            self._owner.close()
            raise PersistenceError("migration_validation_failed") from None

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise PersistenceError("persistence_closed")
            try:
                self._db.execute("BEGIN IMMEDIATE")
                yield self._db
                self._db.execute("COMMIT")
            except Exception as error:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if isinstance(error, sqlite3.Error):
                    raise PersistenceError() from None
                raise

    def _migrate(self) -> None:
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if type(version) is not int or version < 0:
                raise PersistenceError("migration_validation_failed")
            if version > SCHEMA_VERSION:
                raise PersistenceError("unsupported_database_version")
            for target, statements in MIGRATIONS:
                checksum = hashlib.sha256("\n".join(statements).encode()).hexdigest()
                if target <= version:
                    row = db.execute("SELECT checksum FROM schema_migrations WHERE version=?", (target,)).fetchone()
                    if row is None or row[0] != checksum:
                        raise PersistenceError("migration_validation_failed")
                else:
                    for statement in statements:
                        db.execute(statement)
                    db.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (target, checksum, now()))
                    db.execute(f"PRAGMA user_version={target}")
            ledger = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
            if ledger != list(range(1, SCHEMA_VERSION + 1)):
                raise PersistenceError("migration_validation_failed")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise PersistenceError("migration_validation_failed")
            # Validate actual DDL as well as the migration ledger.
            for _, statements in MIGRATIONS:
                for statement in statements:
                    if statement.lstrip().upper().startswith("PRAGMA"):
                        continue
                    name = statement.split()[2].split("(")[0]
                    actual = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
                    if actual is None or " ".join(actual[0].split()) != " ".join(statement.split()):
                        raise PersistenceError("migration_validation_failed")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise PersistenceError("migration_validation_failed")

    def _enforce_run_retention(self) -> None:
        """Apply a stricter max_runs setting when an existing store is reopened."""
        with self.transaction() as db:
            self._evict_terminal_runs_locked(db, self.config.max_runs)
            db.execute("DELETE FROM repositories WHERE repository_id NOT IN (SELECT repository_id FROM review_runs)")

    def _evict_terminal_runs_locked(self, db: sqlite3.Connection, target_count: int) -> None:
        terminal = "('completed','failed','cancelled','recovery_failed','expired')"
        while db.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] > target_count:
            oldest = db.execute(
                "SELECT repository_id,run_id,payload FROM review_runs "
                f"WHERE status IN {terminal} ORDER BY created_at,run_id LIMIT 1"
            ).fetchone()
            if oldest is None:
                return
            self._expire_locked(db, str(oldest[0]), str(oldest[1]), oldest[2])
            db.execute("DELETE FROM review_runs WHERE repository_id=? AND run_id=?", (oldest[0], oldest[1]))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._owner.close()
                self._closed = True

    def save(self, snapshot: dict[str, Any], *, event: str) -> None:
        """Atomic checkpoint. The codec supplies allowlisted metadata only."""
        from codeatlas.service.persistence_codec import validate_snapshot

        # Check the serialized run envelope before the codec can reject an
        # otherwise well-shaped but oversized snapshot. Validation still runs
        # immediately afterwards and remains the source of safety decisions.
        if isinstance(snapshot, dict) and isinstance(snapshot.get("review"), dict):
            canonical_json(
                {"review": snapshot["review"], "artifacts": snapshot.get("artifacts", {})},
                self.config.max_payload_bytes,
            )
        validate_snapshot(snapshot)
        rid, run = snapshot["repository_id"], snapshot["run_id"]
        payload = canonical_json({"review": snapshot["review"], "artifacts": snapshot.get("artifacts", {})}, self.config.max_payload_bytes)
        entities = snapshot["entities"]
        # The checkpoint always adds one lifecycle record. Leave room for it
        # before doing the more complete count that includes retained history.
        if len(entities) >= self.config.max_records_per_run:
            raise PersistenceError("persistence_record_limit")
        encoded = [(item, canonical_json(item["payload"], self.config.max_payload_bytes)) for item in entities]
        timestamp = now()
        created = snapshot["review"]["status"]["created_at"]
        expires = (datetime.fromisoformat(created) + timedelta(seconds=self.config.max_age_seconds)).isoformat()
        with self.transaction() as db:
            previous = db.execute("SELECT repository_id,created_at FROM review_runs WHERE run_id=?", (run,)).fetchone()
            if previous and (previous[0] != rid or previous[1] != created):
                raise PersistenceError("persistence_scope_mismatch")
            if previous is None:
                cutoff = (datetime.now(UTC) - timedelta(seconds=self.config.max_age_seconds)).isoformat()
                aged = list(db.execute(
                    "SELECT repository_id,run_id,payload FROM review_runs WHERE created_at<? AND status IN ('completed','failed','cancelled','recovery_failed','expired')",
                    (cutoff,),
                ))
                for aged_row in aged:
                    self._expire_locked(db, str(aged_row[0]), str(aged_row[1]), aged_row[2])
                # Retention admission never evicts live operations.
                self._evict_terminal_runs_locked(db, self.config.max_runs - 1)
                if db.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] >= self.config.max_runs:
                    raise PersistenceError("persistence_capacity_reached")
            db.execute("INSERT INTO repositories VALUES(?,?,?,?,?,?) ON CONFLICT(repository_id) DO UPDATE SET updated_at=excluded.updated_at",
                       (rid, snapshot["repository_path"], PAYLOAD_VERSION, REPAIR_POLICY_VERSION, created, timestamp))
            status = snapshot["review"]["status"]
            db.execute("""INSERT INTO review_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(repository_id,run_id) DO UPDATE SET base_commit=excluded.base_commit,
                head_commit=excluded.head_commit,updated_at=excluded.updated_at,status=excluded.status,
                recovery_state=excluded.recovery_state,payload=excluded.payload,payload_hash=excluded.payload_hash""",
                (rid, run, status.get("base_commit") or "", status.get("head_commit") or "", PAYLOAD_VERSION,
                 REPAIR_POLICY_VERSION, created, timestamp, expires, status["status"], status.get("recovery_state", "not_required"),
                 payload, hashlib.sha256(payload.encode()).hexdigest()))
            # Current projections are replaced atomically; operation history is append-only.
            db.execute("DELETE FROM records WHERE repository_id=? AND run_id=? AND kind NOT IN ('lifecycle_events','validation_attempts','safe_errors')", (rid, run))
            for item, text in encoded:
                self._put(db, snapshot, item, text, timestamp)
            # Every checkpoint records the durable operation state before/after work.
            event_payload = {"event": event, "status": status["status"], "operation": snapshot["review"].get("operation"), "at": timestamp}
            if type(event) is not str or len(event) > 128 or not repair_payload_is_safe(event_payload):
                raise PersistenceError("invalid_persistence_payload")
            event_text = canonical_json(event_payload, self.config.max_payload_bytes)
            import uuid
            self._put(db, snapshot, {"kind": "lifecycle_events", "id": uuid.uuid4().hex, "payload": event_payload}, event_text, timestamp)
            for kind in ("lifecycle_events", "validation_attempts", "safe_errors"):
                db.execute("""DELETE FROM records WHERE repository_id=? AND run_id=? AND kind=? AND entity_id NOT IN
                    (SELECT entity_id FROM records WHERE repository_id=? AND run_id=? AND kind=? ORDER BY updated_at DESC,entity_id DESC LIMIT 50)""",
                    (rid, run, kind, rid, run, kind))
            if db.execute("SELECT COUNT(*) FROM records WHERE repository_id=? AND run_id=?", (rid, run)).fetchone()[0] > self.config.max_records_per_run:
                raise PersistenceError("persistence_record_limit")
            db.execute("DELETE FROM repositories WHERE repository_id NOT IN (SELECT repository_id FROM review_runs)")

    def _expire_locked(self, db: sqlite3.Connection, rid: str, run_id: str, raw_payload: str) -> None:
        try:
            stored = json.loads(raw_payload)
            stored["review"]["status"]["status"] = "expired"
            stored["review"]["status"]["recovery_state"] = "expired"
            stored["review"]["status"]["progress_text"] = "Persisted review data expired by retention policy"
            stored["review"]["status"]["finding_counts"] = {}
            stored["review"]["latest"] = {}
            stored["review"]["operation"] = None
            stored["artifacts"] = {}
            text = canonical_json(stored, self.config.max_payload_bytes)
        except (KeyError, TypeError, ValueError, PersistenceError):
            text = canonical_json({"review": {"status": {"status": "expired", "recovery_state": "expired"}, "artifacts": {}}}, self.config.max_payload_bytes)
        db.execute("DELETE FROM records WHERE repository_id=? AND run_id=?", (rid, run_id))
        db.execute("UPDATE review_runs SET status='expired',recovery_state='expired',payload=?,payload_hash=?,updated_at=? WHERE repository_id=? AND run_id=?",
                   (text, hashlib.sha256(text.encode()).hexdigest(), now(), rid, run_id))

    def _put(self, db: sqlite3.Connection, snapshot: dict[str, Any], item: dict[str, Any], text: str, timestamp: str) -> None:
        status = snapshot["review"]["status"]
        db.execute("""INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(repository_id,run_id,kind,entity_id) DO UPDATE SET payload=excluded.payload,
            payload_hash=excluded.payload_hash,updated_at=excluded.updated_at""",
            (snapshot["repository_id"], snapshot["run_id"], item["kind"], item["id"],
             item.get("finding_id", ""), item.get("proposal_id", ""), status.get("base_commit") or "",
             status.get("head_commit") or "", item.get("patch_hash", ""), PAYLOAD_VERSION, REPAIR_POLICY_VERSION,
             timestamp, timestamp, text, hashlib.sha256(text.encode()).hexdigest()))

    def load(self, rid: str, run_id: str) -> dict[str, Any] | None:
        """Scope is mandatory, including for history/evidence retrieval."""
        with self._lock:
            row = self._db.execute("SELECT r.*,p.canonical_path FROM review_runs r JOIN repositories p USING(repository_id) WHERE r.repository_id=? AND r.run_id=?", (rid, run_id)).fetchone()
            if row is None:
                return None
            def decode(item: sqlite3.Row) -> Any:
                if (item["schema_version"] != PAYLOAD_VERSION or item["policy_version"] != REPAIR_POLICY_VERSION
                        or hashlib.sha256(item["payload"].encode()).hexdigest() != item["payload_hash"]
                        or len(item["payload"].encode()) > self.config.max_payload_bytes):
                    raise PersistenceError("persisted_identity_invalid")
                try:
                    value = json.loads(item["payload"])
                except ValueError:
                    raise PersistenceError("persisted_payload_invalid") from None
                if not repair_payload_is_safe(value):
                    raise PersistenceError("persisted_payload_unsafe")
                return value
            stored = decode(row)
            if not isinstance(stored, dict) or not isinstance(stored.get("review"), dict):
                raise PersistenceError("persisted_payload_invalid")
            review = stored["review"]
            status = review.get("status")
            if not isinstance(status, dict):
                raise PersistenceError("persisted_payload_invalid")
            if (
                status.get("run_id") != row["run_id"]
                or (status.get("base_commit") or "") != row["base_commit"]
                or (status.get("head_commit") or "") != row["head_commit"]
                or status.get("status") != row["status"]
                or status.get("created_at") != row["created_at"]
            ):
                raise PersistenceError("persisted_identity_invalid")
            if self._db.execute(
                "SELECT COUNT(*) FROM records WHERE repository_id=? AND run_id=?", (rid, run_id)
            ).fetchone()[0] > self.config.max_records_per_run:
                raise PersistenceError("persistence_record_limit")
            entities = []
            latest_event: tuple[str, str] | None = None
            for child in self._db.execute("SELECT * FROM records WHERE repository_id=? AND run_id=? ORDER BY created_at,entity_id", (rid, run_id)):
                if (child["kind"] != "lifecycle_events"
                        and (child["base_commit"] != row["base_commit"] or child["head_commit"] != row["head_commit"])):
                    raise PersistenceError("persisted_identity_invalid")
                value = decode(child)
                if child["kind"] == "lifecycle_events":
                    if (
                        not isinstance(value, dict)
                        or type(value.get("event")) is not str
                        or len(value["event"]) > 128
                        or type(value.get("status")) is not str
                        or len(value["status"]) > 64
                        or type(value.get("at")) is not str
                        or len(value["at"]) > 64
                    ):
                        raise PersistenceError("persisted_payload_invalid")
                    child_updated = str(child["updated_at"])
                    if latest_event is None or child_updated > latest_event[0]:
                        latest_event = (child_updated, value["at"])
                entities.append({"kind": child["kind"], "id": child["entity_id"], "finding_id": child["finding_id"],
                                 "proposal_id": child["proposal_id"], "patch_hash": child["patch_hash"], "payload": value})
            if latest_event is not None and (latest_event[0] != row["updated_at"] or latest_event[1] != row["updated_at"]):
                raise PersistenceError("persisted_identity_invalid")
            result = {"repository_id": rid, "repository_path": row["canonical_path"], "run_id": run_id,
                      "review": review, "entities": entities, "artifacts": stored.get("artifacts", {})}
            from codeatlas.service.persistence_codec import validate_snapshot
            validate_snapshot(result)
            return result

    def recovery_index(self) -> list[dict[str, Any]]:
        """Mark pending before decoding any artifacts; no automatic replay."""
        with self.transaction() as db:
            for row in list(db.execute("SELECT repository_id,run_id,payload FROM review_runs WHERE expires_at<? AND status!='expired'", (now(),))):
                self._expire_locked(db, row[0], row[1], row[2])
            db.execute("UPDATE review_runs SET recovery_state='pending_recovery' WHERE recovery_state!='expired'")
            return [dict(row) for row in db.execute("SELECT repository_id,run_id,created_at,expires_at FROM review_runs ORDER BY created_at,run_id")]

    def load_reviews(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT r.*,p.canonical_path FROM review_runs r JOIN repositories p USING(repository_id) ORDER BY r.created_at, r.run_id"))

    def quarantine(self, rid: str, run_id: str, *, expired: bool = False) -> None:
        """Discard unverifiable artifacts, retaining a bounded identity tombstone."""
        state = "expired" if expired else "recovery_failed"
        with self.transaction() as db:
            db.execute("DELETE FROM records WHERE repository_id=? AND run_id=?", (rid, run_id))
            db.execute("UPDATE review_runs SET status=?,recovery_state=?,payload='{}',payload_hash=?,updated_at=? WHERE repository_id=? AND run_id=?",
                       (state, state, hashlib.sha256(b"{}").hexdigest(), now(), rid, run_id))
