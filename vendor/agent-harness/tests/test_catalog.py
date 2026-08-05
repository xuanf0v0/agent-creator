from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.catalog import AgentCatalog, ManifestError


def test_load_resolves_paths(manifest_dir: Path) -> None:
    agent = AgentCatalog.load(manifest_dir).require("worker")
    assert agent.cwd == manifest_dir.parent / "worker"
    assert agent.env_file == manifest_dir.parent / "worker" / ".env"

def test_rejects_duplicate_ports(manifest_dir: Path) -> None:
    (manifest_dir / "second.yaml").write_text(
        "id: second\nname: Second\ncwd: ../worker\ncommand: [run]\nport: 19091\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="duplicate agent port"):
        AgentCatalog.load(manifest_dir)


def test_rejects_path_outside_project(manifest_dir: Path) -> None:
    path = manifest_dir / "worker.yaml"
    path.write_text(path.read_text().replace("../worker", "../../escape"), encoding="utf-8")
    with pytest.raises(ManifestError, match="escapes allowed root"):
        AgentCatalog.load(manifest_dir)
