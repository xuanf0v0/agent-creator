from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from openagent_studio.models import WorkflowNode
from openagent_studio.workflow_runner import WorkflowManager, WorkflowRun


ROOT = Path(__file__).resolve().parents[1]


def test_http_request_propagates_n8n_error_and_uses_configured_timeout() -> None:
    timeout: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        timeout.update(request.extensions["timeout"])
        return httpx.Response(500, json={"error": "n8n failure"})

    manager = WorkflowManager()
    manager._http_client.close()
    manager._http_client = httpx.Client(transport=httpx.MockTransport(handler))
    run = WorkflowRun(id="test-run", workflow_id="test", input="test-query")
    node = WorkflowNode(
        id="n8n",
        type="http_request",
        data={
            "url": "https://example.com/webhook",
            "method": "POST",
            "body": {"query": "{{input}}"},
            "timeout_seconds": 60,
            "fail_on_error": True,
        },
    )

    with pytest.raises(RuntimeError, match="HTTP 请求返回 500"):
        manager._http_request(run, node, run.input)

    assert timeout == {"connect": 60.0, "read": 60.0, "write": 60.0, "pool": 60.0}
    manager.stop_scheduler()


def test_versioned_n8n_workflows_match_bidirectional_contract() -> None:
    fetch = json.loads((ROOT / "n8n/workflows/studio-fetch-sheet.json").read_text(encoding="utf-8"))
    callback = json.loads((ROOT / "n8n/workflows/studio-callback-test.json").read_text(encoding="utf-8"))

    assert fetch["id"] == "studioFetchSheet1"
    assert fetch["nodes"][0]["parameters"]["path"] == "studio-fetch-sheet"
    assert fetch["nodes"][0]["parameters"]["responseMode"] == "responseNode"
    assert "source: 'n8n'" in fetch["nodes"][1]["parameters"]["responseBody"]
    assert fetch["connections"]["Studio Fetch Webhook"]["main"][0][0]["node"] == "Return Studio Payload"
    assert callback["id"] == "studioCallback1"
    assert callback["nodes"][0]["parameters"]["responseMode"] == "responseNode"
    assert callback["nodes"][1]["parameters"]["url"] == "http://host.docker.internal:8787/hooks/n8n-callback"
    assert callback["connections"]["Call Studio"]["main"][0][0]["node"] == "Return Callback Result"
    assert "credentials" not in json.dumps([fetch, callback])


def test_project_n8n_example_has_strict_acceptance_contract() -> None:
    project = yaml.safe_load((ROOT / "project.yaml").read_text(encoding="utf-8"))
    workflows = {workflow["id"]: workflow for workflow in project["workflows"]}
    fetch = workflows["n8n-fetch"]
    request = next(node for node in fetch["nodes"] if node["id"] == "fetch")
    assertions = fetch["evaluation"]["cases"][0]["assertions"]

    assert request["data"]["url"] == "http://127.0.0.1:5678/webhook/studio-fetch-sheet"
    assert request["data"]["timeout_seconds"] == 60
    assert {item["path"]: item.get("expected") for item in assertions} | {
        "body.ok": True,
        "body.query": "test-query",
        "body.source": "n8n",
    } == {item["path"]: item.get("expected") for item in assertions}
    assert workflows["n8n-callback"]["nodes"][0]["data"]["path"] == "/hooks/n8n-callback"


def test_compose_keeps_n8n_local_and_credential_storage_private() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.n8n.yml").read_text(encoding="utf-8"))
    service = compose["services"]["n8n"]

    assert service["ports"] == ["127.0.0.1:5678:5678"]
    assert service["environment"]["N8N_WEBHOOK_URL"] == "http://127.0.0.1:5678/"
    assert "WEBHOOK_URL" not in service["environment"]
    assert "N8N_RUNNERS_ENABLED" not in service["environment"]
    assert service["volumes"] == ["n8n_data:/home/node/.n8n"]
