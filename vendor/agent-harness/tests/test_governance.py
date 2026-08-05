from __future__ import annotations

import asyncio
from pathlib import Path

from agent_harness.catalog import AgentCatalog
from agent_harness.environment import fingerprint
from agent_harness.instructions import load_instructions, safe_task_directory
from agent_harness.models import AgentManifest
from agent_harness.state_store import StateStore
from agent_harness.task_runner import TaskRunner


def test_instructions_merge_root_and_leaf(tmp_path: Path) -> None:
    leaf = tmp_path / "src" / "feature"
    leaf.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("compat", encoding="utf-8")
    (tmp_path / "src" / "AGENTS.md").write_text("src", encoding="utf-8")
    bundle = load_instructions(tmp_path, leaf)
    assert bundle.sources == ("AGENTS.md", "CLAUDE.md", "src/AGENTS.md")
    assert bundle.content.index("root") < bundle.content.index("src")


def test_task_directory_rejects_escape(tmp_path: Path) -> None:
    try:
        safe_task_directory(tmp_path, "../outside")
    except ValueError as exc:
        assert "escapes" in str(exc)
    else:
        raise AssertionError("escape accepted")


async def test_task_blocks_on_drift_then_completes_after_setup(tmp_path: Path) -> None:
    workspace = tmp_path / "worker"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1'\n", encoding="utf-8")
    worker = workspace / "worker.py"
    worker.write_text("import sys; from pathlib import Path; Path('done').write_text(sys.stdin.read()); print('worked')", encoding="utf-8")
    check = workspace / "check.py"
    check.write_text("from pathlib import Path; raise SystemExit(0 if Path('done').exists() else 1)", encoding="utf-8")
    agent = AgentManifest.model_validate({
        "id": "worker", "name": "Worker", "cwd": workspace,
        "task": {"command": ["python3", str(worker)], "sandbox": {"enabled": False}, "verification": [{"name": "done", "command": ["python3", str(check)]}]},
    })
    catalog = AgentCatalog([agent])
    store = StateStore(tmp_path / "state.db")
    runner = TaskRunner(catalog, store)
    await runner.start()
    first = await runner.create("worker", "First", "write")
    await runner._queues["worker"].join()
    assert store.task(first["id"])["status"] == "blocked"
    assert store.task(first["id"])["error_code"] == "setup_required"
    value, files = fingerprint(agent)
    store.set_environment("worker", value, files)
    await runner.retry(first["id"])
    await runner._queues["worker"].join()
    completed = store.task(first["id"])
    assert completed["status"] == "completed"
    assert completed["attempts"][0]["evidence"][0]["exit_code"] == 0
    assert (workspace / ".harness" / "PROGRESS.md").is_file()
    await runner.shutdown()


def test_state_store_marks_interrupted_task_failed(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = StateStore(path)
    task = store.create_task("a", tmp_path, ".", "Work", "prompt")
    store.set_task_status(task["id"], "working")
    reopened = StateStore(path)
    assert reopened.task(task["id"])["status"] == "failed"
