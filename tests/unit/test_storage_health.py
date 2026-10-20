"""Read-only storage health on the service-owned connection."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from codeatlas.service import persistence as p
from codeatlas.service.contract import endpoint_for, openapi_document
from codeatlas.service.models import HealthResponse, StorageHealthResponse
from codeatlas.service.server import ReviewHttpHandler, create_server
from codeatlas.service.state import ReviewStateManager


@pytest.fixture
def store(tmp_path):
    backend = p.SQLitePersistence(p.PersistenceConfig(tmp_path / "private.sqlite3"))
    try:
        yield backend
    finally:
        backend.close()


def test_owned_connection_is_read_only_and_keeps_settings(store, monkeypatch):
    statements = []
    before = store._db.total_changes
    settings = ("busy_timeout", "journal_mode", "foreign_keys", "synchronous", "user_version")
    values = [store._db.execute(f"PRAGMA {name}").fetchone()[0] for name in settings]
    owner = store._owner

    def forbidden(*args, **kwargs):
        raise AssertionError("Health must use the existing owner and connection")

    monkeypatch.setattr(p.sqlite3, "connect", forbidden)
    monkeypatch.setattr(store, "_migrate", forbidden)
    monkeypatch.setattr(store, "transaction", forbidden)
    store._db.set_trace_callback(statements.append)
    try:
        assert store.storage_health() == "ok"
    finally:
        store._db.set_trace_callback(None)
    assert store._db.total_changes == before
    assert store._owner is owner and not owner.closed
    assert [store._db.execute(f"PRAGMA {name}").fetchone()[0] for name in settings] == values
    assert statements == [
        "PRAGMA quick_check(1)", "PRAGMA user_version",
        f"SELECT version, substr(checksum, 1, 65) FROM schema_migrations ORDER BY version LIMIT {p.SCHEMA_VERSION + 1}",
    ]
    with pytest.raises(p.PersistenceError, match="database_in_use"):
        p.SQLitePersistence(store.config)


@pytest.mark.parametrize("statement", [
    "PRAGMA user_version=0", "PRAGMA user_version=999",
    "UPDATE schema_migrations SET checksum='private-path-payload' WHERE version=1",
    "DELETE FROM schema_migrations WHERE version=1",
    "INSERT INTO schema_migrations VALUES(999,'private-path-payload','private')",
    "DROP TABLE schema_migrations",
])
def test_migration_invalid_does_not_repair_or_write(store, statement):
    store._db.execute(statement)
    before = store._db.total_changes
    assert store.storage_health() == "migration_invalid"
    assert store._db.total_changes == before


def test_quick_check_detects_corruption_without_details(store):
    store._db.execute("CREATE TABLE unhealthy(value INTEGER CHECK(value > 0))")
    store._db.execute("PRAGMA ignore_check_constraints=ON")
    store._db.execute("INSERT INTO unhealthy VALUES(-1)")
    store._db.execute("PRAGMA ignore_check_constraints=OFF")
    assert store.storage_health() == "corrupted_database"


@pytest.mark.parametrize("code,expected", [
    (sqlite3.SQLITE_CORRUPT, "corrupted_database"),
    (sqlite3.SQLITE_NOTADB, "corrupted_database"),
    (sqlite3.SQLITE_BUSY, "database_unavailable"),
    (sqlite3.SQLITE_LOCKED, "database_unavailable"),
    (sqlite3.SQLITE_IOERR, "database_unavailable"),
    (sqlite3.SQLITE_CANTOPEN, "database_unavailable"),
    (sqlite3.SQLITE_INTERRUPT, "database_unavailable"),
])
def test_sqlite_failures_are_stable_and_clear_progress(store, monkeypatch, code, expected):
    db = store._db
    cleared = []
    error = sqlite3.DatabaseError("private-database-path-and-payload")
    error.sqlite_errorcode = code

    def execute(statement):
        raise error

    def progress(callback, steps):
        cleared.append((callback, steps))
        db.set_progress_handler(callback, steps)

    monkeypatch.setattr(store, "_db", SimpleNamespace(execute=execute, set_progress_handler=progress))
    assert store.storage_health() == expected
    assert cleared[-1] == (None, 0)
    monkeypatch.setattr(store, "_db", db)
    assert store.storage_health() == "ok"


def test_closed_database_is_unavailable(store):
    store.close()
    assert store.storage_health() == "database_unavailable"


def test_owner_mutex_wait_is_bounded(store, monkeypatch):
    monkeypatch.setattr(p, "STORAGE_HEALTH_SECONDS", 0.02)
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with store._lock:
            acquired.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert acquired.wait(2)
        start = time.monotonic()
        assert store.storage_health() == "database_unavailable"
        assert time.monotonic() - start < 1
    finally:
        release.set()
        worker.join(2)
    assert store.storage_health() == "ok"


@pytest.mark.parametrize("bound", ["steps", "time"])
def test_vm_work_is_bounded_and_progress_is_cleared(store, monkeypatch, bound):
    db = store._db
    if bound == "steps":
        monkeypatch.setattr(p, "STORAGE_HEALTH_MAX_STEPS", 1000)
    else:
        monkeypatch.setattr(p, "STORAGE_HEALTH_MAX_STEPS", 10**12)
        ticks = iter([0.0, 0.0, 1.0])
        monkeypatch.setattr(p.time, "monotonic", lambda: next(ticks))

    def execute(statement):
        assert statement == "PRAGMA quick_check(1)"
        return db.execute(
            "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL "
            "SELECT n+1 FROM numbers WHERE n<10000000) SELECT sum(n) FROM numbers"
        )

    monkeypatch.setattr(store, "_db", SimpleNamespace(
        execute=execute, set_progress_handler=db.set_progress_handler,
    ))
    assert store.storage_health() == "database_unavailable"
    monkeypatch.setattr(store, "_db", db)
    monkeypatch.undo()
    assert db.execute(
        "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL "
        "SELECT n+1 FROM numbers WHERE n<1000) SELECT sum(n) FROM numbers"
    ).fetchone()[0] == 500500
    assert store.storage_health() == "ok"


def test_process_state_does_not_construct_persistence(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Process storage must not open SQLite")

    monkeypatch.setattr(p.sqlite3, "connect", forbidden)
    manager = ReviewStateManager()
    try:
        assert manager.storage_health().model_dump() == {
            "state_lifetime": "process", "status": "not_applicable", "code": "not_applicable",
        }
    finally:
        manager.close()


@pytest.mark.parametrize("change", [
    {"code": "private-path-payload"}, {"status": "unknown"},
    {"state_lifetime": "sqlite"}, {"path": "private"}, {"errors": ["private"]},
])
def test_public_model_is_strict_and_bounded(change):
    data = {"state_lifetime": "persistent", "status": "ok", "code": "ok"}
    data.update(change)
    with pytest.raises(ValidationError):
        StorageHealthResponse.model_validate(data)


@pytest.mark.parametrize("peer", ["192.0.2.1", "::ffff:192.0.2.1", "invalid"])
def test_non_loopback_peer_is_rejected_before_storage_access(peer):
    handler = object.__new__(ReviewHttpHandler)
    handler.path = "/api/v1/diagnostics/storage"
    handler.command = "GET"
    handler.client_address = (peer, 1234)
    sent = []
    handler._send_json = lambda status, data: sent.append((status, data))
    handler.do_GET()
    assert sent == [(403, {"error": "forbidden", "message": "Loopback access required"})]


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_peers_are_allowed(peer):
    handler = object.__new__(ReviewHttpHandler)
    handler.path = "/api/v1/diagnostics/storage"
    handler.command = "GET"
    handler.client_address = (peer, 1234)
    response = StorageHealthResponse(state_lifetime="process", status="not_applicable", code="not_applicable")
    handler.state_manager = SimpleNamespace(storage_health=lambda: response)
    sent = []
    handler._send_json = lambda status, data: sent.append((status, data))
    handler.do_GET()
    assert sent == [(200, response.model_dump())]


@pytest.mark.parametrize("persistent", [False, True])
def test_loopback_http_aliases_and_unchanged_health(tmp_path, persistent):
    kwargs = {"database_path": tmp_path / "private.sqlite3"} if persistent else {}
    server = create_server(port=0, **kwargs)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for prefix in ("", "/api/v1"):
            with urllib.request.urlopen(base + prefix + "/diagnostics/storage", timeout=3) as response:
                body = json.load(response)
                assert response.status == 200
            assert body == {
                "state_lifetime": "persistent" if persistent else "process",
                "status": "ok" if persistent else "not_applicable",
                "code": "ok" if persistent else "not_applicable",
            }
            assert len(json.dumps(body)) < 128
            if persistent:
                server.state_manager.persistence._db.execute("PRAGMA user_version=999")
                with urllib.request.urlopen(base + prefix + "/diagnostics/storage", timeout=3) as response:
                    assert json.load(response)["code"] == "migration_invalid"
                server.state_manager.persistence._db.execute(f"PRAGMA user_version={p.SCHEMA_VERSION}")
        request = urllib.request.Request(base + "/api/v1/diagnostics/storage", method="POST", data=b"{}")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        assert raised.value.code == 405
        with urllib.request.urlopen(base + "/api/v1/health", timeout=3) as response:
            health = json.load(response)
        assert set(health) == set(HealthResponse.model_fields)
        assert health["status"] == "ok"
        assert server.state_manager.list_reviews().total == 0
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()


def test_openapi_adds_storage_without_changing_health_contract():
    endpoint = endpoint_for("GET", "/diagnostics/storage")
    assert endpoint.response is StorageHealthResponse
    assert endpoint_for("POST", "/diagnostics/storage") is None
    document = openapi_document()
    operation = document["paths"]["/diagnostics/storage"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/StorageHealthResponse",
    }
    assert "403" in operation["responses"]
    schema = document["components"]["schemas"]["StorageHealthResponse"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"state_lifetime", "status", "code"}
    assert document["components"]["schemas"]["HealthResponse"] == HealthResponse.model_json_schema()
