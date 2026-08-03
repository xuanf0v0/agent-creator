from pathlib import Path
from fastapi.testclient import TestClient
from openagent_studio.app import create_app
from openagent_studio.generator import Generation, GeneratorManager
from openagent_studio.store import SpecStore
from openagent_studio.models import ProjectSpec
from openagent_studio.workflow_runner import WorkflowManager
from openagent_studio.workflow_runner import _cron_matches, _cron_valid
from openagent_studio.harness_opencode import build_prompt
import time
from datetime import datetime


def test_spec_round_trip_and_conflict(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    response = client.get("/api/spec")
    assert response.status_code == 200
    etag = response.json()["etag"]
    spec = response.json()["spec"]
    spec["name"] = "Demo"
    saved = client.put("/api/spec", headers={"if-match": etag}, json=spec)
    assert saved.status_code == 200
    assert client.put("/api/spec", headers={"if-match": etag}, json=spec).status_code == 409


def test_compile_artifacts(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    payload = {"version":"1", "name":"Demo", "agents":[{"id":"builder","name":"Builder","model":"deepseek/deepseek-v4-flash","max_steps":12}], "providers":[], "harness":[{"id":"builder","name":"Builder","cwd":"."}]}
    assert client.put("/api/spec", json=payload).status_code == 200
    output = client.get("/api/compile/opencode").json()
    assert output["agent"]["builder"]["maxSteps"] == 12
    assert client.get("/api/compile/harness/builder").status_code == 200


def test_ui_is_chinese(tmp_path: Path):
    body = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False)).get("/").text
    assert "智能体工作流画布" in body
    assert 'lang="zh-CN"' in body
    assert 'id="root"' in body
    assert "/assets/" in body


def test_form_options_and_validation(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    options = client.get("/api/form-options").json()
    assert any(item["label"] == "主智能体" for item in options["agent_modes"])
    assert client.post("/api/spec/validate", json={"version": "1", "name": "示例"}).json()["valid"] is True


def test_workflow_canvas_api(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    payload = {"version": "1", "name": "示例", "workflows": [{"id": "flow", "name": "测试流程", "nodes": [{"id": "a", "type": "agent", "data": {}, "position": {"x": 1, "y": 2}}], "edges": []}]}
    assert client.put("/api/spec", json=payload).status_code == 200
    assert client.get("/api/workflows").json()[0]["id"] == "flow"
    assert client.post("/api/workflows/flow/validate").json()["valid"] is True


def test_generator_applies_incremental_operations(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    manager = GeneratorManager(store)
    generation = Generation(id="g", workflow_id="flow", base_etag=store.etag(), draft={"id": "flow", "name": "流程", "nodes": [], "edges": []}, prompt="创建流程", harness_agent_ids={"coding"})
    manager._apply(generation, {"action": "add_node", "id": "review", "type": "agent", "data": {"description": "代码审查", "agent_id": "coding", "prompt": "审查 {{latest}}"}})
    manager._apply(generation, {"action": "add_node", "id": "done", "type": "output", "description": "输出结果"})
    manager._apply(generation, {"action": "connect_nodes", "source": "review", "target": "done"})
    assert [node["id"] for node in generation.draft["nodes"]] == ["review", "done"]
    assert generation.draft["nodes"][0]["data"]["prompt"] == "审查 {{latest}}"
    assert generation.draft["edges"] == [{"source": "review", "target": "done"}]
    assert [event["event"] for event in generation.events] == ["workflow.node.added", "workflow.node.added", "workflow.edge.added"]


def _wait_for(predicate, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for workflow run")


def test_workflow_run_api_executes_non_agent_nodes(tmp_path: Path):
    app = create_app(tmp_path / "project.yaml", auto_start_harness=False)
    client = TestClient(app)
    payload = {
        "version": "1", "name": "示例",
        "workflows": [{"id": "flow", "name": "测试流程", "nodes": [
            {"id": "prompt", "type": "prompt", "data": {"template": "任务：{{input}}"}},
            {"id": "output", "type": "output", "data": {}},
        ], "edges": [{"source": "prompt", "target": "output"}]}],
    }
    assert client.put("/api/spec", json=payload).status_code == 200
    started = client.post("/api/workflows/flow/runs", json={"input": "检查代码"})
    assert started.status_code == 202
    run_id = started.json()["id"]
    _wait_for(lambda: client.get(f"/api/workflow-runs/{run_id}").json()["status"] == "completed")
    result = client.get(f"/api/workflow-runs/{run_id}").json()
    assert result["outputs"]["output"] == "任务：检查代码"


def test_workflow_runner_calls_harness_task_and_waits_for_approval():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "示例",
        "harness": [{"id": "coding", "name": "Coding", "cwd": ".", "task": {"command": ["worker"], "verification": [{"name": "test", "command": ["test"]}]}}],
        "workflows": [{"id": "flow", "name": "测试流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "{{input}}"}},
            {"id": "approval", "type": "approval", "data": {"description": "确认结果"}},
            {"id": "output", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "approval"}, {"source": "approval", "target": "output"}]}],
    })
    manager = WorkflowManager(poll_interval=0.001)
    calls = []

    def scripted_request(method, path, body=None, headers=None):
        calls.append((method, path, body, headers))
        if path == "/api/tasks":
            return {"id": "task-1", "status": "queued"}
        if path == "/api/tasks/task-1":
            return {"id": "task-1", "status": "completed"}
        if path.startswith("/api/tasks/task-1/logs"):
            return [{"line": "done"}]
        raise AssertionError(path)

    manager._request = scripted_request
    run = manager.start(project, "flow", {"input": "完成任务"})
    _wait_for(lambda: run.node_states["approval"]["status"] == "waiting")
    manager.approve(run.id, "approval", True, "可以发布")
    _wait_for(lambda: run.status == "completed")
    assert calls[0][1] == "/api/tasks"
    assert calls[0][2]["prompt"] == "完成任务"
    assert calls[0][3]["Idempotency-Key"].startswith("openagent-")
    assert run.outputs["output"]["approved"] is True


def test_condition_edges_skip_inactive_branch():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "示例",
        "workflows": [{"id": "flow", "name": "条件流程", "nodes": [
            {"id": "condition", "type": "condition", "data": {"expression": "input == true"}},
            {"id": "yes", "type": "output", "data": {"template": "yes"}},
            {"id": "no", "type": "output", "data": {"template": "no"}},
        ], "edges": [
            {"source": "condition", "target": "yes", "condition": "true"},
            {"source": "condition", "target": "no", "condition": "false"},
        ]}],
    })
    manager = WorkflowManager()
    run = manager.start(project, "flow", {"input": True})
    _wait_for(lambda: run.status == "completed")
    assert run.outputs["yes"] == "yes"
    assert run.node_states["no"]["status"] == "skipped"


def test_real_harness_opencode_prompt_contains_governance_and_task():
    prompt = build_prompt({"task": {"prompt": "修复测试"}, "instructions": "只能修改任务范围内的文件"})
    assert prompt == "只能修改任务范围内的文件\n\n用户任务：\n修复测试"


def test_dify_style_data_nodes_and_switch_routing():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "选品",
        "workflows": [{"id": "selection", "name": "选品流程", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "category", "type": "transform", "data": {"operation": "extract", "path": "category"}},
            {"id": "route", "type": "switch", "data": {"cases": [{"value": "hot", "expression": "latest == electronics"}], "default_case": "other"}},
            {"id": "hot", "type": "output", "data": {"template": "热门类目"}},
            {"id": "other", "type": "output", "data": {"template": "其他类目"}},
        ], "edges": [
            {"source": "start", "target": "category"}, {"source": "category", "target": "route"},
            {"source": "route", "target": "hot", "condition": "hot"}, {"source": "route", "target": "other", "condition": "other"},
        ]}],
    })
    run = WorkflowManager().start(project, "selection", {"input": {"category": "electronics"}})
    _wait_for(lambda: run.status == "completed")
    assert run.outputs["hot"] == "热门类目"
    assert run.node_states["other"]["status"] == "skipped"


def test_webhook_node_starts_workflow(tmp_path: Path):
    app = create_app(tmp_path / "project.yaml", auto_start_harness=False)
    client = TestClient(app)
    payload = {"version": "1", "name": "Webhook", "workflows": [{"id": "hook-flow", "name": "Hook", "nodes": [
        {"id": "hook", "type": "webhook", "data": {"path": "/hooks/products/select", "method": "POST"}},
        {"id": "result", "type": "output", "data": {}},
    ], "edges": [{"source": "hook", "target": "result"}]}]}
    assert client.put("/api/spec", json=payload).status_code == 200
    response = client.post("/hooks/products/select", json={"market": "US"})
    assert response.status_code == 202
    run_id = response.json()["id"]
    _wait_for(lambda: client.get(f"/api/workflow-runs/{run_id}").json()["status"] == "completed")
    assert client.get(f"/api/workflow-runs/{run_id}").json()["outputs"]["result"]["body"] == {"market": "US"}


def test_cron_matching_supports_steps_and_ranges():
    now = datetime(2026, 8, 3, 10, 30)
    assert _cron_matches("*/15 9-18 * * 1", now)
    assert not _cron_matches("0 9 * * 1", now)
    assert _cron_valid("*/15 9-18 * * 1,3,5")
    assert not _cron_valid("60 9 * * 1")
    assert not _cron_valid("0 18-9 * * 1")


def test_multiple_trigger_roots_are_isolated():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "多入口",
        "workflows": [{"id": "flow", "name": "多入口流程", "nodes": [
            {"id": "manual", "type": "manual_trigger", "data": {}},
            {"id": "hook", "type": "webhook", "data": {"path": "/hooks/isolation", "method": "POST"}},
            {"id": "timer", "type": "schedule", "data": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"}},
            {"id": "manual-output", "type": "output", "data": {"template": "manual"}},
            {"id": "hook-output", "type": "output", "data": {"template": "hook"}},
            {"id": "timer-output", "type": "output", "data": {"template": "timer"}},
        ], "edges": [
            {"source": "manual", "target": "manual-output"},
            {"source": "hook", "target": "hook-output"},
            {"source": "timer", "target": "timer-output"},
        ]}],
    })
    run = WorkflowManager().start(project, "flow", {"input": {}, "_trigger_node_id": "hook"})
    _wait_for(lambda: run.status == "completed")
    assert run.outputs["hook-output"] == "hook"
    assert run.node_states["manual-output"]["status"] == "skipped"
    assert run.node_states["timer-output"]["status"] == "skipped"


def test_retry_and_fallback_policy():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "容错",
        "workflows": [{"id": "flow", "name": "容错流程", "nodes": [
            {"id": "bad", "type": "transform", "data": {
                "operation": "json_parse", "retry_count": 2, "retry_delay_seconds": 0,
                "on_error": "continue", "fallback_value": {"safe": True},
            }},
            {"id": "result", "type": "output", "data": {}},
        ], "edges": [{"source": "bad", "target": "result"}]}],
    })
    run = WorkflowManager().start(project, "flow", {"input": "not-json"})
    _wait_for(lambda: run.status == "completed")
    assert run.outputs["result"] == {"safe": True}
    assert "warning" in run.node_states["bad"]
    assert len([item for item in run.events if item["event"] == "node.retry"]) == 2


def test_knowledge_retrieval_and_subworkflow():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "选品",
        "workflows": [
            {"id": "retrieve", "name": "召回", "nodes": [
                {"id": "start", "type": "manual_trigger", "data": {}},
                {"id": "knowledge", "type": "knowledge_retrieval", "data": {
                    "query": "{{latest}}", "top_k": 1,
                    "documents": ["露营 户外 帐篷", "厨房 咖啡机"],
                }},
                {"id": "result", "type": "output", "data": {}},
            ], "edges": [{"source": "start", "target": "knowledge"}, {"source": "knowledge", "target": "result"}]},
            {"id": "parent", "name": "父流程", "nodes": [
                {"id": "child", "type": "subworkflow", "data": {"workflow_id": "retrieve", "input_template": "{{latest}}"}},
                {"id": "result", "type": "output", "data": {}},
            ], "edges": [{"source": "child", "target": "result"}]},
        ],
    })
    run = WorkflowManager().start(project, "parent", {"input": "露营"})
    _wait_for(lambda: run.status == "completed")
    child_outputs = run.outputs["result"]
    assert child_outputs["result"]["matches"][0]["content"] == "露营 户外 帐篷"


def test_http_node_rejects_private_address_before_request():
    try:
        WorkflowManager._validate_http_url("https://127.0.0.1/admin", allow_private=False)
    except RuntimeError as exc:
        assert "禁止访问私网" in str(exc)
    else:
        raise AssertionError("private address should be rejected")


def test_generator_supplies_new_node_defaults(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    manager = GeneratorManager(store)
    generation = Generation(id="new", workflow_id="flow", base_etag=store.etag(), draft={"id": "flow", "name": "流程", "nodes": [], "edges": []}, prompt="增加定时任务")
    manager._apply(generation, {"action": "add_node", "id": "daily", "type": "schedule", "data": {"description": "每日选品"}})
    assert generation.draft["nodes"][0]["data"]["cron"] == "0 9 * * *"
    assert generation.draft["nodes"][0]["data"]["timezone"] == "Asia/Shanghai"
