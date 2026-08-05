from __future__ import annotations

from pathlib import Path
import threading
import time
from pydantic import ValidationError
import pytest

from fastapi.testclient import TestClient

from agent_harness.api import create_app
from agent_harness.catalog import AgentCatalog
from agent_harness.models import AgentManifest
from agent_harness.runtime_policy import _macos_command
from agent_harness.state_store import StateStore
from agent_harness.supervisor import AgentSupervisor
from agent_harness.task_runner import TaskRunner
from agent_harness.environment import fingerprint


def test_task_manifest_gets_safe_setup_default(tmp_path: Path) -> None:
    agent = AgentManifest.model_validate({
        "id": "worker", "name": "Worker", "cwd": tmp_path,
        "task": {"command": ["python", "-c", "pass"], "verification": [{"name": "ok", "command": ["python", "-c", "pass"]}]},
    })
    assert agent.environment.setup_command is None
    with pytest.raises(ValidationError):
        AgentManifest.model_validate({"id": "bad", "name": "Bad", "cwd": tmp_path, "task": {"command": ["tool"], "verification": [{"name": "ok", "command": ["tool"]}]}, "environment": {"setup_command": []}})


def test_environment_status_is_structured_and_setup_is_idempotent(manifest_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_HARNESS_HOME", str(tmp_path / "home"))
    manifest = manifest_dir / "worker.yaml"
    manifest.write_text(manifest.read_text().replace("setup_command: [python, -m, pip, --version]", "setup_command: [python3, -c, pass]"))
    app = create_app(manifest_dir, tmp_path / "state.db")
    with TestClient(app) as client:
        initial = client.get("/api/agents/worker/environment").json()
        assert initial["state"] == "setup_required"
        assert initial["setup_required"] is True
        assert client.get("/api/agents/worker").json()["lifecycle_state"] == "setup_required"
        first = client.post("/api/agents/worker/setup", headers={"Idempotency-Key": "same"})
        second = client.post("/api/agents/worker/setup", headers={"Idempotency-Key": "same"})
        assert first.status_code == second.status_code == 200
        assert first.json()["setup_operation"]["id"] == second.json()["setup_operation"]["id"]
        ready = client.get("/api/agents/worker/environment").json()
        assert ready["state"] == "ready"
        assert ready["previous_fingerprint"] == ready["current_fingerprint"]
        assert client.get("/api/agents/worker").json()["lifecycle_state"] == "ready"
        asynchronous = client.post("/api/agents/worker/setup", headers={"Idempotency-Key": "async", "Prefer": "respond-async"})
        assert asynchronous.status_code == 202
        operation_id = asynchronous.json()["setup_operation"]["id"]
        deadline = time.time() + 2
        while time.time() < deadline and client.get(f"/api/setup-operations/{operation_id}").json()["status"] not in {"ready", "error"}:
            time.sleep(0.02)
        assert client.get(f"/api/setup-operations/{operation_id}").json()["status"] == "ready"
        manifest.write_text(manifest.read_text().replace("[python3, -c, pass]", "[python3, -c, 'print(1)']"))
        assert client.post("/api/system/reload").status_code == 200
        command_drift = client.get("/api/agents/worker/environment").json()
        assert "<setup_command>" in command_drift["changed_files"]
        worker = manifest_dir.parent / "worker"
        (worker / "pyproject.toml").write_text("[project]\nname='changed'\nversion='0.1'\n")
        drift = client.get("/api/agents/worker/environment").json()
        assert drift["setup_required"] is True
        assert set(drift["changed_files"]) == {"<setup_command>", "pyproject.toml"}


def test_bearer_auth_audit_metrics_and_hot_reload(manifest_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_HARNESS_TOKEN", "secret-token")
    app = create_app(manifest_dir, tmp_path / "state.db")
    with TestClient(app) as client:
        assert client.get("/api/agents").status_code == 401
        headers = {"Authorization": "Bearer secret-token", "X-Actor": "tester"}
        assert client.get("/api/agents", headers=headers).status_code == 200
        assert client.post("/api/system/reload", headers=headers).json()["reloaded"] is True
        audit = client.get("/api/system/audit", headers=headers).json()
        assert any(item["actor"] == "tester" and item["path"] == "/api/agents" for item in audit)
        metrics = client.get("/metrics", headers=headers)
        assert metrics.status_code == 200
        assert "agent_harness_uptime_seconds" in metrics.text


def test_runtime_policy_builds_filesystem_and_network_sandbox(tmp_path: Path) -> None:
    agent = AgentManifest.model_validate({
        "id": "worker", "name": "Worker", "cwd": tmp_path,
        "task": {
            "command": ["tool"],
            "sandbox": {"network": "allowlist", "network_allowlist": ["127.0.0.1"]},
            "verification": [{"name": "ok", "command": ["tool"]}],
        },
    })
    command = _macos_command(["tool"], tmp_path, agent, 43123)
    assert command[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert "deny file-write" in command[2]
    assert 'remote ip "localhost:43123"' in command[2]
    read_only = AgentManifest.model_validate({
        "id": "readonly", "name": "Readonly", "cwd": tmp_path,
        "task": {"command": ["tool"], "tools": {"deny": ["write", "network"]}, "verification": [{"name": "ok", "command": ["tool"]}]},
    })
    assert read_only.task and read_only.task.sandbox.workspace_write is False
    assert str(tmp_path) not in _macos_command(["tool"], tmp_path, read_only)[2]


async def test_auto_setup_recovers_drift_and_persists_logs(tmp_path: Path) -> None:
    workspace = tmp_path / "worker"; workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='worker'\nversion='0.1'\n")
    agent = AgentManifest.model_validate({
        "id": "worker", "name": "Worker", "cwd": workspace,
        "environment": {"auto_setup_on_drift": True, "setup_command": ["python3", "-c", "print('auto prepared')"]},
        "task": {"command": ["python3", "-c", "import sys; sys.stdin.read()"], "sandbox": {"enabled": False}, "verification": [{"name": "ok", "command": ["python3", "-c", "pass"]}]},
    })
    catalog = AgentCatalog([agent]); store = StateStore(tmp_path / "state.db")
    supervisor = AgentSupervisor(catalog); runner = TaskRunner(catalog, store, supervisor)
    await runner.start()
    task = await runner.create("worker", "Auto", "run")
    await runner._queues["worker"].join()
    assert store.task(task["id"])["status"] == "completed"
    setup = store.latest_setup("worker")
    assert setup and setup["status"] == "ready"
    assert setup["logs"] == ["auto prepared"]
    assert any(event["kind"] == "environment.auto_setup.completed" for event in store.events(task["id"]))
    system_kinds = [event["kind"] for event in store.system_events()]
    assert "setup.ready" in system_kinds
    assert "task.completed" in system_kinds
    await runner.shutdown(0); store.close()


def test_restart_marks_interrupted_setup_as_error(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = StateStore(path)
    operation = store.start_setup("worker")
    store.update_setup(operation["id"], "preparing")
    store.close()
    reopened = StateStore(path)
    recovered = reopened.setup_operation(operation["id"])
    assert recovered["status"] == "error"
    assert recovered["error_code"] == "harness_interrupted"
    reopened.close()


async def test_restart_restores_queued_tasks(tmp_path: Path) -> None:
    workspace = tmp_path / "worker"; workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='worker'\nversion='0.1'\n")
    agent = AgentManifest.model_validate({
        "id": "worker", "name": "Worker", "cwd": workspace,
        "task": {"command": ["python3", "-c", "import sys; sys.stdin.read()"], "sandbox": {"enabled": False}, "verification": [{"name": "ok", "command": ["python3", "-c", "pass"]}]},
    })
    catalog = AgentCatalog([agent]); store = StateStore(tmp_path / "state.db")
    value, files = fingerprint(agent); store.set_environment("worker", value, files)
    queued = store.create_task("worker", workspace, ".", "Restored", "run")
    runner = TaskRunner(catalog, store)
    await runner.start(); await runner._queues["worker"].join()
    assert store.task(queued["id"])["status"] == "completed"
    await runner.shutdown(0); store.close()


def test_concurrent_setup_claim_has_one_owner(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    barrier = threading.Barrier(3)
    results: list[dict] = []
    def claim() -> None:
        barrier.wait(); results.append(store.start_setup("worker"))
    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join()
    assert len({item["id"] for item in results}) == 1
    assert sum(bool(item["_created"]) for item in results) == 1
    store.close()


def test_manifest_watcher_reloads_without_restart(manifest_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_HARNESS_HOME", str(tmp_path / "home"))
    with TestClient(create_app(manifest_dir, tmp_path / "state.db")) as client:
        manifest = manifest_dir / "worker.yaml"
        manifest.write_text(manifest.read_text().replace("name: Worker", "name: Reloaded Worker"))
        deadline = time.time() + 3
        while time.time() < deadline:
            if client.get("/api/agents/worker").json()["name"] == "Reloaded Worker": break
            time.sleep(0.1)
        assert client.get("/api/agents/worker").json()["name"] == "Reloaded Worker"
