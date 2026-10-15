"""Real-process interruption evidence against the same durable SQLite database."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

WORKER = Path(__file__).with_name("recovery_worker.py")
ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def build_repo(repo, ready):
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Recovery test")
    source = repo / "src/app.py"
    source.write_text("def render(user):\n    return user\n", encoding="utf-8")
    # This fixture is controlled by the harness, not downloaded PR code.
    (repo / "tests/test_app.py").write_text(
        "import json, os, time\nfrom pathlib import Path\n"
        "def test_render():\n"
        f"    ready = Path({str(ready)!r})\n"
        "    if ready.with_suffix('.gate').exists():\n"
        "        scope = json.loads(ready.with_suffix('.scope.json').read_text())\n"
        "        pending = ready.with_suffix('.pending')\n"
        "        pending.write_text(json.dumps({**scope, 'test_pid': os.getpid(), 'test_cwd': os.getcwd()}))\n"
        "        pending.replace(ready)\n"
        "        time.sleep(120)\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    source.write_text("def render(user):\n    print(user.password)\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    return source.read_bytes()


def environment(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp_path)
    return env


def kill_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


@pytest.mark.parametrize("stage", ["queued", "provider", "test", "revert"])
def test_killed_worker_recovers_without_automatic_execution(tmp_path: Path, stage: str):
    ready = tmp_path / "ready.json"
    repo = tmp_path / "repo"
    original = build_repo(repo, ready)
    database = tmp_path / "service.sqlite3"
    env = environment(tmp_path)
    log = tmp_path / "worker.log"
    with log.open("w", encoding="utf-8") as stream:
        worker = subprocess.Popen(
            [sys.executable, str(WORKER), stage, str(database), str(repo), str(ready)],
            cwd=ROOT, env=env, stdout=stream, stderr=stream,
            start_new_session=os.name != "nt",
        )
        try:
            deadline = time.monotonic() + 60
            while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert ready.exists(), log.read_text(encoding="utf-8")
            scope = json.loads(ready.read_text(encoding="utf-8"))
            assert worker.poll() is None
            with sqlite3.connect(database) as db:
                rows = db.execute("SELECT run_id,status,payload FROM review_runs ORDER BY run_id").fetchall()
                assert {row[0] for row in rows} == set(scope["run_ids"])
                if stage == "queued":
                    assert "preparing" in scope["statuses"]
                else:
                    assert rows[0][1] == "completed"
                    operation = json.loads(rows[0][2])["review"]["operation"]
                    assert operation["name"] == {"provider": "generation", "test": "validation", "revert": "revert"}[stage]
                if stage == "test":
                    assert scope["test_pid"] != worker.pid
                    assert Path(scope["test_cwd"]).resolve() != repo.resolve()
                if stage == "revert":
                    assert scope["restored_before_interruption"] is True
            before = (repo / "src/app.py").read_bytes()
            assert before == original
        finally:
            kill_tree(worker)
    assert worker.returncode != 0
    # Two fresh owners verify lock release and stable, repeatable recovery.
    for attempt in range(2):
        output = tmp_path / f"recovered-{attempt}.json"
        recovered = subprocess.run(
            [sys.executable, str(WORKER), "recover", str(database), str(ready), str(output)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        evidence = json.loads(output.read_text(encoding="utf-8"))
        assert evidence["counters"] == {"provider": 0, "test": 0, "git_mutation": 0, "workspace_write": 0}
        assert evidence["errors"] == []
        assert (repo / "src/app.py").read_bytes() == before
        if stage == "queued":
            assert all(item["status"] == "recovery_failed" for item in evidence["statuses"])
            assert all(item["completed_at"] == "" for item in evidence["statuses"])
        else:
            assert evidence["statuses"][0]["status"] == "completed"
            assert evidence["findings"]
            assert evidence["safe_error_code"] == "operation_interrupted"
            assert evidence["statuses"][0]["errors"] == ["operation_interrupted"]
            assert set(evidence["blocked"].values()) == {"recovered_operation_unavailable"}
            if stage in {"test", "revert"}:
                assert evidence["fix"]["patch_hash"] == scope["patch_hash"]
                assert evidence["fix"]["patch_text"] == scope["patch_text"]
                assert evidence["fix"]["recovery_state"] == "recovery_failed"
                assert evidence["history"]["revert_available"] is False
            if stage == "test":
                assert evidence["fix"]["validation_status"] == "validation_failed"
                assert evidence["fix"]["validation_result"]["valid"] is False
                assert evidence["statuses"][0]["limitations"].count(
                    "Interrupted validation sandbox cleanup is unverified; recovery does not delete workspaces."
                ) == 1
            if stage == "revert":
                assert evidence["history"]["apply_status"] == "applied"
                assert all(item["event"] != "reverted" for item in evidence["history"]["events"])
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    # Crash-created sandboxes are observed, not claimed to be auto-cleaned.
    if stage == "test":
        assert Path(scope["test_cwd"]).exists()
