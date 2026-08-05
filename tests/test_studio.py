from pathlib import Path
from fastapi.testclient import TestClient
from openagent_studio.app import HarnessManager, create_app
from openagent_studio.generator import Generation, GeneratorManager, SYSTEM_PROMPT, _command_exceeds_limit, _is_command_line_too_long, _with_file_prompt
from openagent_studio.store import SpecStore
from openagent_studio.models import EvaluationAssertion, ProjectSpec, QQIntegrationSpec, WorkflowEvaluation
from openagent_studio.workflow_runner import EvaluationPolicy, WorkflowManager
from openagent_studio.evaluation import SemanticVerdict, WorkflowEvaluator, check_assertion, complexity_metrics
from openagent_studio.workflow_runner import _cron_matches, _cron_valid
from openagent_studio.harness_opencode import build_prompt
from openagent_studio.platform_integrations import PlatformIntegrationManager
import time
from datetime import datetime
import json
import hashlib


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


def test_compile_harness_preserves_setup_command(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    payload = {"version": "1", "name": "Setup", "harness": [{"id": "builder", "name": "Builder", "cwd": ".", "environment": {"setup_command": ["uv", "sync", "--frozen"], "auto_setup_on_drift": True}}]}
    assert client.put("/api/spec", json=payload).status_code == 200
    manifest = client.get("/api/compile/harness/builder").text
    assert "setup_command:" in manifest
    assert "- --frozen" in manifest
    assert "auto_setup_on_drift: true" in manifest


def test_harness_manager_uses_project_vendored_runtime(monkeypatch, tmp_path: Path):
    for key in ("AGENT_HARNESS_ROOT", "AGENT_HARNESS_BIN", "AGENT_HARNESS_HOME", "AGENT_HARNESS_MANIFESTS"):
        monkeypatch.delenv(key, raising=False)
    manager = HarnessManager(tmp_path)
    assert manager.root == tmp_path / "vendor/agent-harness"
    assert manager.source == tmp_path / "vendor/agent-harness/src"
    assert manager.command[1:] == ["-m", "agent_harness"]
    assert manager.home == tmp_path / ".harness/agent-harness"
    assert manager.manifests == tmp_path / ".openagent-agents"
    assert manager.base_url == "http://127.0.0.1:8765"
    assert manager.external_url_configured is False


def test_harness_manager_rejects_unowned_default_runtime(monkeypatch, tmp_path: Path):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"database": {"path": "/tmp/some-other-project/state.db"}}

    monkeypatch.delenv("AGENT_HARNESS_URL", raising=False)
    monkeypatch.setattr("openagent_studio.app.httpx.get", lambda *args, **kwargs: Response())
    assert HarnessManager(tmp_path).reachable() is False
    monkeypatch.setenv("AGENT_HARNESS_URL", "http://127.0.0.1:9999")
    assert HarnessManager(tmp_path).reachable() is True


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


def test_generator_detects_long_commands_and_windows_error(monkeypatch):
    monkeypatch.setenv("OPENAGENT_COMPACT_COMMAND_LENGTH", "1000")

    assert _command_exceeds_limit(["opencode", "run", "x" * 1200]) is True
    assert _command_exceeds_limit(["opencode", "run", "short"]) is False
    assert _is_command_line_too_long("The command line is too long.") is True
    assert _is_command_line_too_long("[WinError 206] filename too long") is True
    assert _is_command_line_too_long("authentication failed") is False


def test_generator_places_prompt_before_greedy_file_option(tmp_path: Path):
    path = tmp_path / "context.md"

    assert _with_file_prompt(["opencode", "run"], "compress this", path) == [
        "opencode", "run", "compress this", "--file", str(path),
    ]


def test_generator_prompt_treats_canvas_as_complete_snapshot():
    assert "完整快照" in SYSTEM_PROMPT
    assert "不要只关注当前会话" in SYSTEM_PROMPT
    assert "以本轮提供的完整快照为准" in SYSTEM_PROMPT


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
        "harness": [{"id": "coding", "name": "Coding", "runtime": "task", "cwd": ".", "task": {"command": ["worker"], "verification": [{"name": "test", "command": ["test"]}]}}],
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
        if path == "/api/v1/tasks":
            return {"id": "task-1", "status": "queued"}
        if path == "/api/v1/tasks/task-1":
            return {"id": "task-1", "status": "completed"}
        if path.startswith("/api/v1/tasks/task-1/logs"):
            return {"items": [{"line": "done"}], "next_cursor": 1}
        if path == "/api/v1/tasks/task-1/result":
            return {"task_id": "task-1", "status": "completed", "output": {"type": "text", "text": "done"}, "logs": {}, "verification": {}, "artifacts": [], "error": None}
        raise AssertionError(path)

    manager._request = scripted_request
    run = manager.start(project, "flow", {"input": "完成任务"})
    _wait_for(lambda: run.node_states["approval"]["status"] == "waiting")
    manager.approve(run.id, "approval", True, "可以发布")
    _wait_for(lambda: run.status == "completed")
    assert calls[0][1] == "/api/v1/tasks"
    assert calls[0][2]["input"]["prompt"] == "完成任务"
    assert calls[0][2]["input"]["metadata"]["workflow_node_id"] == "agent"
    assert calls[0][3]["Idempotency-Key"].startswith("openagent-")
    assert run.outputs["output"]["approved"] is True


def test_harness_environment_drift_runs_setup_and_retries_once():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "环境恢复",
        "harness": [{"id": "coding", "name": "Coding", "cwd": ".", "task": {"command": ["worker"]}}],
        "workflows": [{"id": "flow", "name": "恢复流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "{{input}}"}},
            {"id": "output", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "output"}]}],
    })
    manager = WorkflowManager(poll_interval=0.001)
    calls = []

    def scripted_request(method, path, body=None, headers=None):
        calls.append((method, path, body, headers))
        if path == "/api/tasks" and len([item for item in calls if item[1] == "/api/tasks"]) == 1:
            return {"id": "drifted-task", "status": "queued"}
        if path == "/api/tasks/drifted-task":
            return {"id": "drifted-task", "status": "blocked", "blocked_reason": "setup required", "error_code": "setup_required", "setup_required": True}
        if path == "/api/agents/coding/setup":
            assert headers["Prefer"] == "respond-async"
            assert headers["Idempotency-Key"].startswith("openagent-setup-")
            return {"accepted": True, "setup_operation": {"id": "setup-1", "status": "queued"}}
        if path == "/api/setup-operations/setup-1":
            return {"id": "setup-1", "status": "ready", "fingerprint": "prepared"}
        if path == "/api/tasks" and len([item for item in calls if item[1] == "/api/tasks"]) == 2:
            return {"id": "recovered-task", "status": "queued"}
        if path == "/api/tasks/recovered-task":
            return {"id": "recovered-task", "status": "completed"}
        if path.startswith("/api/tasks/recovered-task/logs"):
            return [{"line": "recovered"}]
        raise AssertionError(path)

    manager._request = scripted_request
    run = manager.start(project, "flow", {"input": "执行"})
    _wait_for(lambda: run.status == "completed")
    assert run.outputs["output"]["text"] == "recovered"
    assert any(path == "/api/agents/coding/setup" for _, path, _, _ in calls)
    keys = [headers["Idempotency-Key"] for method, path, _, headers in calls if path == "/api/tasks"]
    assert keys[1].endswith("-recovery")


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


def test_evaluation_assertions_and_case_immutability(tmp_path: Path):
    assert check_assertion({"items": ["a", "b"]}, EvaluationAssertion(path="items", operator="contains", expected="a")) is None
    assert check_assertion({"code": "OK-42"}, EvaluationAssertion(path="code", operator="matches", expected=r"^OK-\d+$")) is None
    assert check_assertion({}, EvaluationAssertion(path="missing", operator="exists")) == "missing 不存在"
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    old = WorkflowEvaluation.model_validate({"cases": [{"id": "old", "name": "旧标准", "input": 1, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["结果正确"]}]})
    same_plus_one = WorkflowEvaluation.model_validate({"cases": [old.cases[0].model_dump(mode="json"), {"id": "new", "name": "新标准", "input": 2, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["结果正确"]}]})
    manager._validate_case_update(old, same_plus_one)
    changed = WorkflowEvaluation.model_validate({"cases": [{"id": "old", "name": "被弱化", "input": 1, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["结果正确"]}]})
    try:
        manager._validate_case_update(old, changed)
    except RuntimeError as exc:
        assert "删除、修改" in str(exc)
    else:
        raise AssertionError("existing cases must be immutable during optimization")


def test_evaluation_mode_uses_real_logic_and_effect_mocks():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "验收",
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "request", "type": "http_request", "data": {"url": "https://should-not-run.invalid"}},
            {"id": "result", "type": "output", "data": {}},
        ], "edges": [{"source": "start", "target": "request"}, {"source": "request", "target": "result"}], "evaluation": {"cases": [{
            "id": "mocked", "name": "副作用隔离", "input": {"id": 1},
            "assertions": [{"path": "status", "operator": "equals", "expected": 200}],
            "semantic_criteria": ["HTTP mock 结果有效"],
            "mocks": [{"node_id": "request", "response": {"status": 200}}],
        }]}}],
    })
    workflow = project.workflows[0]
    evaluator = WorkflowEvaluator(lambda prompt, value: value, lambda workflow, case, output: SemanticVerdict(True, 100), live_execution=False)
    result = evaluator.evaluate(project, workflow, 0)
    assert result.passed is True
    assert result.cases[0].output == {"status": 200}
    manager = WorkflowManager()
    run = manager.start(project, "flow", {"input": {}}, policy=EvaluationPolicy(), record=False)
    _wait_for(lambda: run.status == "failed")
    assert "缺少 mock" in run.error
    assert run.id not in manager.runs


def test_opencode_must_explicitly_confirm_acceptance():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "明确验证",
        "workflows": [{"id": "flow", "name": "流程", "nodes": [{"id": "result", "type": "output", "data": {"template": "ok"}}], "evaluation": {"cases": [{
            "id": "explicit", "name": "必须明确通过", "input": {},
            "assertions": [{"path": "output", "operator": "equals", "expected": "ok"}],
            "semantic_criteria": ["输出可以交付"],
        }]}}],
    })
    evaluator = WorkflowEvaluator(lambda prompt, value: value, lambda workflow, case, output: SemanticVerdict(False, 95, ["OpenCode 未确认可交付"]))
    result = evaluator.evaluate(project, project.workflows[0], 0)
    assert result.passed is False
    assert result.cases[0].semantic_score == 95
    assert result.cases[0].opencode_verified is False
    assert "OpenCode 未确认可交付" in result.cases[0].errors


def test_complexity_ranking_prefers_shorter_clear_workflow():
    short = ProjectSpec.model_validate({"version": "1", "name": "x", "workflows": [{"id": "flow", "name": "短", "nodes": [{"id": "out", "type": "output", "data": {"description": "结果"}}]}]}).workflows[0]
    long = ProjectSpec.model_validate({"version": "1", "name": "x", "workflows": [{"id": "flow", "name": "长", "nodes": [{"id": "step", "type": "prompt", "data": {}}, {"id": "out", "type": "output", "data": {}}], "edges": [{"source": "step", "target": "out"}]}]}).workflows[0]
    assert complexity_metrics(short, 1.0, 0) < complexity_metrics(long, 0.1, 1)


def test_evaluation_mode_mocks_harness_service():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "服务隔离",
        "harness": [{"id": "service", "name": "Service", "cwd": ".", "service": {"command": ["serve"], "port": 9000}}],
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "service", "prompt": "{{input}}"}},
            {"id": "result", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "result"}]}],
    })
    manager = WorkflowManager()
    run = manager.start(project, "flow", {"input": "x"}, policy=EvaluationPolicy(mocks={"agent": {"safe": True}}, model_inference=lambda *_: (_ for _ in ()).throw(AssertionError("service must not run model"))), record=False)
    _wait_for(lambda: run.status == "completed")
    assert run.outputs["result"] == {"safe": True}


def test_live_acceptance_uses_formal_harness_execution_path():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "真实验收",
        "harness": [{"id": "worker", "name": "Worker", "cwd": ".", "task": {"command": ["worker"]}}],
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "worker", "prompt": "真实执行 {{input}}"}},
            {"id": "result", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "result"}]}],
    })
    manager = WorkflowManager(poll_interval=0.001)
    calls = []

    def scripted_request(method, path, body=None, headers=None):
        calls.append((method, path, body, headers))
        if path == "/api/tasks":
            return {"id": "live-task", "status": "queued"}
        if path == "/api/tasks/live-task":
            return {"id": "live-task", "status": "completed"}
        if path.startswith("/api/tasks/live-task/logs"):
            return [{"line": "real harness result"}]
        raise AssertionError(path)

    manager._request = scripted_request
    policy = EvaluationPolicy(live_execution=True, model_inference=lambda *_: (_ for _ in ()).throw(AssertionError("live validation must not bypass Harness")))
    run = manager.start(project, "flow", {"input": "任务"}, policy=policy, record=False)
    _wait_for(lambda: run.status == "completed")
    assert calls[0][1] == "/api/tasks"
    assert calls[0][2]["prompt"] == "真实执行 任务"
    assert run.outputs["result"]["text"] == "real harness result"
    assert run.id not in manager.runs


def test_generator_builds_three_isolated_candidates_and_saves_shortest(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({"version": "1", "name": "候选", "project_dir": str(tmp_path), "workflows": [{"id": "flow", "name": "原流程", "nodes": [{"id": "old", "type": "output", "data": {"description": "旧结果"}}]}]})
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(id="best", workflow_id="flow", base_etag=store.etag(), draft=project.workflows[0].model_dump(mode="json"), prompt="优化", model="provider/model")
    candidate_prompts = []

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{
                "id": f"case-{index}", "name": f"用例 {index}", "input": index,
                "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["输出有效"],
            } for index in range(1, 4)]
            return f"<result>{json.dumps({'cases': cases}, ensure_ascii=False)}</result>"
        if "工作流架构师" in prompt:
            candidate_prompts.append(prompt)
            count = 1 if "minimal：" in prompt else 2 if "balanced：" in prompt else 3
            nodes = [{"id": f"node-{index}", "type": "output", "data": {"description": f"结果 {index}"}} for index in range(count)]
            return f"<result>{json.dumps({'id': 'flow', 'name': '候选', 'nodes': nodes, 'edges': []}, ensure_ascii=False)}</result>"
        if "独立 OpenCode 验证智能体" in prompt:
            return '<result>{"passed":true,"score":90,"issues":[]}</result>'
        raise AssertionError(prompt)

    manager._invoke = fake_invoke
    manager._run(generation, project)
    assert len(candidate_prompts) == 3
    assert [item["data"]["stage"] for item in generation.events if item["event"] == "generation.stage"] == ["preparing_cases", "generating", "validating", "evaluating", "verifying", "selecting", "saving"]
    assert generation.events[-1]["event"] == "generation.completed", generation.events[-1]
    saved = store.load().workflows[0]
    assert len(saved.nodes) == 1
    assert len(saved.evaluation.cases) == 3


def test_generator_all_failed_candidates_preserve_original(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({"version": "1", "name": "失败保护", "project_dir": str(tmp_path), "workflows": [{"id": "flow", "name": "原流程", "nodes": [{"id": "original", "type": "output", "data": {"description": "不可覆盖"}}]}]})
    store.save(project)
    original_etag = store.etag()
    manager = GeneratorManager(store)
    generation = Generation(id="failed", workflow_id="flow", base_etag=original_etag, draft=project.workflows[0].model_dump(mode="json"), prompt="优化", model="provider/model")

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{"id": f"case-{i}", "name": "失败", "input": i, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["必须优秀"]} for i in range(3)]
            return f"<result>{json.dumps({'cases': cases}, ensure_ascii=False)}</result>"
        if "工作流架构师" in prompt or "工作流修复工程师" in prompt:
            return '<result>{"id":"flow","name":"失败候选","nodes":[{"id":"candidate","type":"output","data":{"description":"候选"}}],"edges":[]}</result>'
        return '<result>{"passed":false,"score":20,"issues":["结果不符合标准"]}</result>'

    manager._invoke = fake_invoke
    manager._run(generation, project)
    assert generation.events[-1]["event"] == "generation.failed"
    assert len([item for item in generation.events if item["event"] == "generation.stage" and item["data"]["stage"] == "repairing"]) == 2
    assert store.etag() == original_etag
    assert store.load().workflows[0].nodes[0].id == "original"


def _platform_project(env_file: Path) -> dict:
    return {
        "version": "1", "name": "平台机器人",
        "workflows": [{"id": "bot-flow", "name": "机器人流程", "nodes": [
            {"id": "result", "type": "output", "data": {}},
        ], "edges": []}],
        "integrations": {
            "feishu": [{"id": "main", "workflow_id": "bot-flow", "env_file": str(env_file), "auto_reply": False}],
            "qq": [{"id": "main", "workflow_id": "bot-flow", "env_file": str(env_file), "auto_reply": False}],
        },
    }


def test_feishu_challenge_and_message_start_workflow(tmp_path: Path):
    env_file = tmp_path / "platform.env"
    env_file.write_text("FEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\nFEISHU_VERIFICATION_TOKEN=verify\n", encoding="utf-8")
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    assert client.put("/api/spec", json=_platform_project(env_file)).status_code == 200
    challenge = client.post("/integrations/feishu/main/events", json={"type": "url_verification", "token": "verify", "challenge": "hello"})
    assert challenge.status_code == 200
    assert challenge.json() == {"challenge": "hello"}
    event = {
        "header": {"token": "verify", "event_id": "evt-1", "event_type": "im.message.receive_v1"},
        "event": {"sender": {"sender_id": {"open_id": "ou-1"}}, "message": {"message_id": "om-1", "chat_id": "oc-1", "message_type": "text", "content": "{\"text\":\"帮我选品\"}"}},
    }
    response = client.post("/integrations/feishu/main/events", json=event)
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    _wait_for(lambda: client.get(f"/api/workflow-runs/{run_id}").json()["status"] == "completed")
    run = client.get(f"/api/workflow-runs/{run_id}").json()
    assert run["input"]["platform"] == "feishu"
    assert run["input"]["content"] == {"text": "帮我选品"}
    duplicate = client.post("/integrations/feishu/main/events", json=event)
    assert duplicate.json()["duplicate"] is True


def test_qq_validation_signature_and_message_start_workflow(tmp_path: Path):
    env_file = tmp_path / "platform.env"
    env_file.write_text("QQ_BOT_APP_ID=1024\nQQ_BOT_SECRET=qq-secret\n", encoding="utf-8")
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    assert client.put("/api/spec", json=_platform_project(env_file)).status_code == 200
    validation = client.post("/integrations/qq/main/events", json={"op": 13, "d": {"plain_token": "plain", "event_ts": "123"}})
    assert validation.status_code == 200
    public_key = PlatformIntegrationManager._qq_private_key("qq-secret").public_key()
    public_key.verify(bytes.fromhex(validation.json()["signature"]), b"123plain")
    event = {"op": 0, "id": "qq-event-1", "t": "C2C_MESSAGE_CREATE", "d": {"id": "msg-1", "content": "帮我选品", "author": {"user_openid": "user-1"}}}
    raw = json.dumps(event, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = PlatformIntegrationManager._qq_private_key("qq-secret").sign(timestamp.encode() + raw).hex()
    response = client.post("/integrations/qq/main/events", content=raw, headers={"content-type": "application/json", "x-signature-timestamp": timestamp, "x-signature-ed25519": signature})
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    _wait_for(lambda: client.get(f"/api/workflow-runs/{run_id}").json()["status"] == "completed")
    assert client.get(f"/api/workflow-runs/{run_id}").json()["input"]["platform"] == "qq"
    bad = client.post("/integrations/qq/main/events", content=raw, headers={"content-type": "application/json", "x-signature-timestamp": timestamp, "x-signature-ed25519": "00" * 64})
    assert bad.status_code == 401


def test_integration_status_never_returns_credentials(tmp_path: Path):
    env_file = tmp_path / "platform.env"
    env_file.write_text("FEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\nFEISHU_VERIFICATION_TOKEN=verify\nQQ_BOT_APP_ID=1024\nQQ_BOT_SECRET=qq-secret\n", encoding="utf-8")
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    client.put("/api/spec", json=_platform_project(env_file))
    body = client.get("/api/integrations/status").json()
    assert body["feishu"][0]["ready"] is True
    assert body["qq"][0]["ready"] is True
    assert "secret" not in json.dumps(body).lower()


def test_feishu_signature_checks_integrity_and_replay_window():
    raw = b'{"header":{"event_id":"evt"}}'
    timestamp = str(int(time.time()))
    nonce, encrypt_key = "nonce", "encrypt-key"
    signature = hashlib.sha256(timestamp.encode() + nonce.encode() + encrypt_key.encode() + raw).hexdigest()
    PlatformIntegrationManager._verify_feishu_signature(raw, {"x-lark-request-timestamp": timestamp, "x-lark-request-nonce": nonce, "x-lark-signature": signature}, encrypt_key)
    try:
        PlatformIntegrationManager._verify_feishu_signature(raw + b" ", {"x-lark-request-timestamp": timestamp, "x-lark-request-nonce": nonce, "x-lark-signature": signature}, encrypt_key)
    except PermissionError:
        pass
    else:
        raise AssertionError("tampered Feishu event should fail signature verification")


def test_qq_reply_uses_platform_specific_paths(monkeypatch):
    manager = PlatformIntegrationManager(WorkflowManager())
    config = QQIntegrationSpec(id="main", workflow_id="flow")
    manager._tokens["qq:main"] = ("token", time.time() + 3600)
    monkeypatch.setattr(manager, "_wait_result", lambda _run_id: "完成")
    calls = []

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("openagent_studio.platform_integrations.httpx.post", fake_post)
    env = {"QQ_BOT_APP_ID": "1024", "QQ_BOT_SECRET": "secret"}
    manager._reply_qq_after_run(config, env, "run", ("groups", "group-1"), "message-1")
    manager._reply_qq_after_run(config, env, "run", ("channels", "channel-1"), "message-2")
    assert calls[0][0].endswith("/v2/groups/group-1/messages")
    assert calls[0][1]["json"]["msg_type"] == 0
    assert calls[1][0].endswith("/channels/channel-1/messages")
    assert "msg_type" not in calls[1][1]["json"]
