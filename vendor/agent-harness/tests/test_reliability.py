from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.api import create_app
from agent_harness.catalog import AgentCatalog
from agent_harness.instance_lock import InstanceLock, InstanceLockedError
from agent_harness.models import AgentManifest
from agent_harness.state_store import SCHEMA_VERSION, StateConflict, StateStore
from agent_harness.task_runner import TaskRunner


def test_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "harness.lock")
    second = InstanceLock(tmp_path / "harness.lock")
    first.acquire()
    try:
        with pytest.raises(InstanceLockedError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_schema_migrates_existing_database_and_backs_up(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, agent_id TEXT, workspace TEXT, relative_path TEXT, title TEXT, prompt TEXT, status TEXT, created_at REAL, updated_at REAL, blocked_reason TEXT DEFAULT '')")
    db.execute("INSERT INTO tasks VALUES ('old','a','/tmp','.','Old','p','queued',1,1,'')")
    db.commit(); db.close()
    store = StateStore(path)
    assert store.task("old")["title"] == "Old"
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert list(tmp_path.glob("state.db.backup-v0-*"))
    store.close()


def test_idempotent_create_replays_and_rejects_changed_payload(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    first = store.create_task("a", tmp_path, ".", "Title", "Prompt", "same")
    replay = store.create_task("a", tmp_path, ".", "Title", "Prompt", "same")
    assert replay["id"] == first["id"]
    with pytest.raises(StateConflict):
        store.create_task("a", tmp_path, ".", "Other", "Prompt", "same")
    store.close()


def test_cas_rejects_duplicate_transition(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    task = store.create_task("a", tmp_path, ".", "T", "P")
    store.transition(task["id"], {"queued"}, "working")
    with pytest.raises(StateConflict):
        store.transition(task["id"], {"queued"}, "working")
    store.close()


async def test_log_limit_truncates_without_failing_task(tmp_path: Path) -> None:
    workspace = tmp_path / "worker"; workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1'\n")
    script = workspace / "worker.py"; script.write_text("import sys; sys.stdin.read(); [print('x'*20) for _ in range(150)]")
    agent = AgentManifest.model_validate({
        "id": "worker", "name": "Worker", "cwd": workspace,
        "task": {"command": ["python3", str(script)], "sandbox": {"enabled": False}, "limits": {"max_log_lines": 100, "max_log_bytes": 4096}, "verification": [{"name": "ok", "command": ["python3", "-c", "pass"]}]},
    })
    catalog = AgentCatalog([agent]); store = StateStore(tmp_path / "state.db")
    from agent_harness.environment import fingerprint
    value, files = fingerprint(agent); store.set_environment("worker", value, files)
    runner = TaskRunner(catalog, store); await runner.start()
    task = await runner.create("worker", "Logs", "run"); await runner._queues["worker"].join()
    assert store.task(task["id"])["status"] == "completed"
    assert len(store.logs(task["id"], 1000)) == 100
    assert any(item["kind"] == "log.truncated" for item in store.events(task["id"]))
    await runner.shutdown(0); store.close()


def test_readiness_and_idempotency_api(manifest_dir: Path, tmp_path: Path) -> None:
    manifest = manifest_dir / "worker.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "\ntask:\n  command: [python3, -c, pass]\n  verification:\n    - name: ok\n      command: [python3, -c, pass]\n",
        encoding="utf-8",
    )
    app = create_app(manifest_dir, tmp_path / "state.db")
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/api/system/status").json()["ready"] is True
        body = {"agent_id": "worker", "title": "T", "prompt": "P"}
        first = client.post("/api/tasks", json=body, headers={"Idempotency-Key": "one"})
        replay = client.post("/api/tasks", json=body, headers={"Idempotency-Key": "one"})
        assert first.status_code == replay.status_code == 202
        assert first.json()["id"] == replay.json()["id"]
        changed = client.post("/api/tasks", json={**body, "title": "Other"}, headers={"Idempotency-Key": "one"})
        assert changed.status_code == 409


def test_corrupt_database_starts_degraded_diagnostics(manifest_dir: Path, tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"not a sqlite database")
    app = create_app(manifest_dir, path)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "degraded"
        assert client.get("/ready").status_code == 503
        assert client.get("/api/system/status").json()["ready"] is False
        assert client.post("/api/tasks", json={}).status_code == 503
