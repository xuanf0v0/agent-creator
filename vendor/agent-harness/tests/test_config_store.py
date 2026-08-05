from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_harness.catalog import AgentCatalog
from agent_harness.config_store import ConfigStore


def test_secret_mask_and_atomic_update(manifest_dir: Path) -> None:
    agent = AgentCatalog.load(manifest_dir).require("worker")
    agent.env_file.write_text("# keep\nAPI_KEY=abcdefghijklm\nMODE=fast\n", encoding="utf-8")
    store = ConfigStore()
    values = {item["key"]: item for item in store.get(agent)}
    assert values["API_KEY"]["value"] == "abcd********jklm"
    store.update(agent, {"API_KEY": "abcd********jklm", "MODE": "careful"})
    content = agent.env_file.read_text(encoding="utf-8")
    assert "# keep" in content and "API_KEY=abcdefghijklm" in content and "MODE=careful" in content
    if os.name != "nt":
        assert agent.env_file.stat().st_mode & 0o777 == 0o600


def test_rejects_unknown_and_invalid_config(manifest_dir: Path) -> None:
    agent = AgentCatalog.load(manifest_dir).require("worker")
    store = ConfigStore()
    with pytest.raises(ValueError, match="unknown config"):
        store.update(agent, {"OTHER": "x"})
    with pytest.raises(ValueError, match="must be one of"):
        store.update(agent, {"MODE": "turbo"})
