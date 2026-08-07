from __future__ import annotations

import os
import time

import pytest

from openagent_studio.models import ProjectSpec
from openagent_studio.workflow_runner import TERMINAL_RUN_STATES, WorkflowManager


pytestmark = pytest.mark.skipif(
    os.environ.get("OPENAGENT_RUN_LIVE_HARNESS_TESTS") != "1",
    reason="set OPENAGENT_RUN_LIVE_HARNESS_TESTS=1 to call the real Harness and model",
)


def test_real_agent_output_chain_returns_only_the_model_answer():
    marker = "OPENAGENT_LIVE_HARNESS_OUTPUT_OK"
    agent_id = os.environ.get("OPENAGENT_LIVE_HARNESS_AGENT_ID", "coding")
    project = ProjectSpec.model_validate({
        "version": "1",
        "name": "Live Harness integration",
        "harness": [{
            "id": "live-agent",
            "name": "Live Harness agent",
            "backend_id": "default",
            "agent_id": agent_id,
        }],
        "workflows": [{
            "id": "live-smoke",
            "name": "Live agent to output smoke test",
            "nodes": [
                {
                    "id": "agent",
                    "type": "agent",
                    "data": {
                        "agent_id": "live-agent",
                        "title": "Live Harness output acceptance",
                        "prompt": (
                            "Do not search the repository. Do not call tools. Do not explain. "
                            f"Your complete final response must be exactly this single line: {marker}"
                        ),
                    },
                },
                {"id": "result", "type": "output", "data": {}},
            ],
            "edges": [{"source": "agent", "target": "result"}],
        }],
    })
    manager = WorkflowManager(
        base_url=os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765"),
        poll_interval=0.25,
    )
    run = manager.start(project, "live-smoke", {"input": "return the exact marker"})
    timeout = float(os.environ.get("OPENAGENT_LIVE_HARNESS_TIMEOUT", "120"))
    deadline = time.monotonic() + timeout
    while run.status not in TERMINAL_RUN_STATES and time.monotonic() < deadline:
        time.sleep(0.25)
    if run.status not in TERMINAL_RUN_STATES:
        manager.cancel(run.id)
        pytest.fail(f"live Harness workflow timed out after {timeout:g}s")

    assert run.status == "completed", run.error
    envelope = run.outputs["agent"]
    assert envelope["text"] == marker
    assert envelope["result"]["output"] == {"type": "text", "text": marker}
    assert run.outputs["result"] == marker
