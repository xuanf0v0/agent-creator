from __future__ import annotations

import asyncio
import os
from pathlib import Path

from agent_harness.catalog import AgentCatalog
from agent_harness.models import AgentState
from agent_harness.supervisor import AgentSupervisor


async def test_start_rejects_occupied_port(manifest_dir: Path, monkeypatch) -> None:
    supervisor = AgentSupervisor(AgentCatalog.load(manifest_dir))
    monkeypatch.setattr(supervisor, "_port_in_use", lambda _port: True)
    result = await supervisor.start("worker")
    assert result.status is AgentState.ERROR
    assert "already in use" in result.error_message


async def test_setup_records_output(manifest_dir: Path) -> None:
    agent = AgentCatalog.load(manifest_dir).require("worker")
    agent.environment.setup_command = ["python3", "-c", "print('prepared')"]
    supervisor = AgentSupervisor(AgentCatalog([agent]))
    result = await supervisor.setup("worker")
    assert result.status is AgentState.STOPPED
    assert supervisor.logs.tail("worker") == ["prepared"]


def test_agent_environment_does_not_inherit_harness_venv(manifest_dir: Path, monkeypatch) -> None:
    agent = AgentCatalog.load(manifest_dir).require("worker")
    monkeypatch.setenv("VIRTUAL_ENV", "/harness/.venv")
    assert "VIRTUAL_ENV" not in AgentSupervisor._environment(agent)
