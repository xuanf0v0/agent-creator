from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def manifest_dir(tmp_path: Path) -> Path:
    agents = tmp_path / "agents"
    worker = tmp_path / "worker"
    agents.mkdir()
    worker.mkdir()
    (agents / "worker.yaml").write_text(
        """
id: worker
name: Worker
cwd: ../worker
command: [python, -m, worker]
setup_command: [python, -m, pip, --version]
port: 19091
health: {path: /health, timeout_seconds: 1, interval_seconds: 0.01}
env_file: .env
config:
  - {key: API_KEY, label: API key, type: secret}
  - {key: MODE, label: Mode, type: select, options: [fast, careful], default: fast}
""".strip(),
        encoding="utf-8",
    )
    return agents
