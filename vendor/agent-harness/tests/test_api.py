from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.api import create_app


def test_management_endpoints(manifest_dir: Path) -> None:
    with TestClient(create_app(manifest_dir)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        agents = client.get("/api/agents").json()
        assert agents[0]["id"] == "worker"
        assert client.get("/api/agents/missing").status_code == 404
        assert client.get("/api/agents/worker/logs?lines=2001").status_code == 422
        assert client.get("/api/agents/worker/service/health").status_code == 503


def test_config_api_rejects_unknown_field(manifest_dir: Path) -> None:
    with TestClient(create_app(manifest_dir)) as client:
        response = client.put("/api/agents/worker/config", json={"UNKNOWN": "x"})
        assert response.status_code == 422
