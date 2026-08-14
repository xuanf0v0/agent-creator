from pathlib import Path
import threading
import pytest
from fastapi.testclient import TestClient
from openagent_studio.app import create_app
from openagent_studio.generator import Generation, GeneratorManager, SYSTEM_PROMPT, _CompactionTimeoutError, _EmptyCompactionError, _OpenCodeTimeoutError, _apply_creation_step, _apply_incremental_step, _command_exceeds_limit, _compact_prompt_length, _compaction_timeout_seconds, _creation_step_errors, _explicit_delete_request, _incremental_max_iterations, _incremental_probe_timeout_seconds, _incremental_probe_workflow, _invoke_timeout_seconds, _is_command_line_too_long, _normalize_evaluation_result, _normalize_workflow_result, _parse_result, _repair_timeout_seconds, _step_requires_runtime_probe, _with_file_prompt
from openagent_studio.store import SpecStore
from openagent_studio.models import EvaluationAssertion, ProjectSpec, QQIntegrationSpec, WorkflowEvaluation, WorkflowNode, WorkflowSpec
from openagent_studio.workflow_runner import EvaluationPolicy, WorkflowManager, validate_executable_workflow
from openagent_studio.evaluation import CandidateResult, CaseResult, HarnessInfrastructureError, SemanticVerdict, WorkflowEvaluator, check_assertion, complexity_metrics
from openagent_studio.workflow_runner import _cron_matches, _cron_valid
from openagent_studio.platform_integrations import PlatformIntegrationManager
import time
from datetime import datetime
import json
import hashlib
import httpx
from agent_harness_sdk import HarnessAPIError


class FakeHarnessClient:
    def __init__(self, handler):
        self.handler = handler
        self.closed = False

    def submit(self, agent_id, title, prompt, **kwargs):
        return self.handler("submit", agent_id=agent_id, title=title, prompt=prompt, **kwargs)

    def task(self, task_id):
        return self.handler("task", task_id=task_id)

    def logs(self, task_id, cursor=0, limit=500):
        return self.handler("logs", task_id=task_id, cursor=cursor, limit=limit)

    def result(self, task_id):
        return self.handler("result", task_id=task_id)

    def cancel(self, task_id):
        return self.handler("cancel", task_id=task_id)

    def capabilities(self):
        return self.handler("capabilities")

    def close(self):
        self.closed = True


def test_spec_round_trip_and_conflict(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml"))
    response = client.get("/api/spec")
    assert response.status_code == 200
    etag = response.json()["etag"]
    spec = response.json()["spec"]
    spec["name"] = "Demo"
    saved = client.put("/api/spec", headers={"if-match": etag}, json=spec)
    assert saved.status_code == 200
    assert client.put("/api/spec", headers={"if-match": etag}, json=spec).status_code == 409


def test_compile_artifacts(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml"))
    payload = {"version":"1", "name":"Demo", "agents":[{"id":"builder","name":"Builder","model":"deepseek/deepseek-v4-flash","max_steps":12}], "providers":[], "harness":[{"id":"builder","name":"Builder","backend_id":"default","agent_id":"builder"}]}
    assert client.put("/api/spec", json=payload).status_code == 200
    output = client.get("/api/compile/opencode").json()
    assert output["agent"]["builder"]["maxSteps"] == 12
    assert client.get("/api/compile/harness/builder").status_code == 404


def test_harness_spec_rejects_runtime_ownership_fields():
    payload = {"version": "1", "name": "Detached", "harness": [{"id": "builder", "name": "Builder", "cwd": "."}]}
    try:
        ProjectSpec.model_validate(payload)
    except ValueError as exc:
        assert "cwd" in str(exc)
    else:
        raise AssertionError("Studio must not accept Harness runtime manifests")


def test_ui_is_chinese(tmp_path: Path):
    body = TestClient(create_app(tmp_path / "project.yaml")).get("/").text
    assert "智能体工作流画布" in body
    assert 'lang="zh-CN"' in body
    assert 'id="root"' in body
    assert "/assets/" in body


def test_expired_generation_event_stops_stale_sse_reconnect(tmp_path: Path):
    response = TestClient(create_app(tmp_path / "project.yaml")).get(
        "/api/generator/generations/stale-generation/events",
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: generation.failed" in response.text
    assert '"reason": "expired"' in response.text
    assert "请重新发起优化" in response.text


def test_form_options_and_validation(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml"))
    options = client.get("/api/form-options").json()
    assert any(item["label"] == "主智能体" for item in options["agent_modes"])
    assert client.post("/api/spec/validate", json={"version": "1", "name": "示例"}).json()["valid"] is True


def test_workflow_canvas_api(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml"))
    payload = {"version": "1", "name": "示例", "workflows": [{"id": "flow", "name": "测试流程", "nodes": [{"id": "a", "type": "agent", "data": {}, "position": {"x": 1, "y": 2}}], "edges": []}]}
    assert client.put("/api/spec", json=payload).status_code == 200
    assert client.get("/api/workflows").json()[0]["id"] == "flow"
    validation = client.post("/api/workflows/flow/validate").json()
    assert validation["valid"] is False
    assert "output" in " ".join(validation["errors"])


def test_runtime_validation_rejects_disconnected_or_incomplete_graphs():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "严格校验",
        "harness": [{"id": "coding", "name": "Coding"}],
        "workflows": [
            {"id": "agent-only", "name": "没有输出", "nodes": [
                {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "执行任务"}},
            ]},
            {"id": "disconnected", "name": "断开的输出", "nodes": [
                {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "执行任务"}},
                {"id": "output", "type": "output", "data": {}},
            ]},
            {"id": "blank-prompt", "name": "空提示词", "nodes": [
                {"id": "agent", "type": "agent", "data": {"agent_id": "coding"}},
                {"id": "output", "type": "output", "data": {}},
            ], "edges": [{"source": "agent", "target": "output"}]},
        ],
    })
    assert "output" in " ".join(validate_executable_workflow(project, project.workflows[0], runtime=True))
    disconnected = " ".join(validate_executable_workflow(project, project.workflows[1], runtime=True))
    assert "后续节点" in disconnected
    assert "没有上游输入" in disconnected
    assert "prompt" in " ".join(validate_executable_workflow(project, project.workflows[2], runtime=True))


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


def test_public_generation_entries_default_to_agent_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """create/start/optimize must all enter LLM-controlled Agent Loop by default."""
    monkeypatch.delenv("OPENAGENT_GENERATOR_MODE", raising=False)
    store = SpecStore(tmp_path / "project.yaml")
    store.save(ProjectSpec.model_validate({
        "version": "1",
        "name": "入口选择",
        "workflows": [{"id": "existing", "name": "已有流程", "nodes": [], "edges": []}],
    }))
    manager = GeneratorManager(store)
    monkeypatch.setattr(manager, "ensure_ready", lambda _spec: {"model": "test-model"})
    launched: list[Generation] = []
    monkeypatch.setattr(manager, "_launch", lambda generation, _spec: launched.append(generation))

    created = manager.create("创建新流程", workflow_id="created")
    started = manager.start("existing", "修改已有流程")
    started.completed = True
    optimized = manager.optimize("existing")

    assert [item.build_mode for item in (created, started, optimized)] == [
        "agent_loop", "agent_loop", "agent_loop",
    ]
    assert launched == [created, started, optimized]


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


def test_generator_chat_reply_does_not_modify_workflow(monkeypatch, tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "对话", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "原流程", "nodes": [
            {"id": "result", "type": "output", "data": {"description": "返回结果"}},
        ]}],
    })
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="chat", workflow_id="flow", base_etag=store.etag(),
        draft=project.workflows[0].model_dump(mode="json"), prompt="这个输出节点有什么作用？",
        model="provider/model", chat_routing=True,
    )
    manager.history["flow"] = [{"role": "user", "content": generation.prompt}]
    monkeypatch.setattr("openagent_studio.generator.resolve_executable", lambda *_: "opencode")
    prompts = []

    def fake_invoke(*args, **_kwargs):
        prompts.append(args[4])
        return '<result>{"action":"reply","answer":"输出节点负责定义工作流最终返回值。","options":[]}</result>'

    manager._invoke = fake_invoke
    original_etag = store.etag()
    manager._run(generation, project)

    assert "当前工作流" in prompts[0]
    assert "用户本轮消息：这个输出节点有什么作用？" in prompts[0]
    assert store.etag() == original_etag
    assert [item["event"] for item in generation.events] == [
        "generation.started", "generation.stage", "chat.assistant.delta", "chat.completed",
    ]
    assert generation.events[-1]["data"]["message"] == "输出节点负责定义工作流最终返回值。"
    assert manager.history["flow"][-1] == {"role": "assistant", "content": "输出节点负责定义工作流最终返回值。"}


def test_generator_chat_reply_exposes_three_clarification_options(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    workflow = WorkflowSpec.model_validate({"id": "flow", "name": "流程", "nodes": [], "edges": []})
    generation = Generation(id="choices", workflow_id="flow", base_etag="etag", draft={}, prompt="帮我做一个流程", model="provider/model")
    manager._invoke_result = lambda *_args, **_kwargs: {
        "action": "reply", "answer": "你希望优先实现哪类流程？",
        "options": ["内容生产流程", "数据分析流程", "审批自动化流程"],
    }

    decision = manager._route_chat_turn(generation, ProjectSpec(name="测试"), ["opencode"], tmp_path, workflow)

    assert decision["options"] == ["内容生产流程", "数据分析流程", "审批自动化流程"]


def test_generator_chat_router_can_return_complete_modify_request(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    workflow = WorkflowSpec.model_validate({"id": "flow", "name": "流程", "nodes": [], "edges": []})
    generation = Generation(id="chat-modify", workflow_id="flow", base_etag="etag", draft={}, prompt="加一个审批", model="provider/model")
    manager.history["flow"] = [
        {"role": "assistant", "content": "审批放在哪里？"},
        {"role": "user", "content": "验证之前"},
    ]
    manager._invoke_result = lambda *_args, **_kwargs: {
        "action": "modify", "request": "在验证节点之前增加人工审批节点，并保持其他节点不变。",
    }

    decision = manager._route_chat_turn(generation, ProjectSpec(name="测试"), ["opencode"], tmp_path, workflow)

    assert decision == {
        "action": "modify", "request": "在验证节点之前增加人工审批节点，并保持其他节点不变。",
    }


def test_generator_normalizes_generic_task_nodes_and_edges():
    raw = {
        "id": "flow", "name": "选品流程",
        "nodes": [
            {"id": "research", "type": "task", "name": "市场调研", "instructions": "调研市场需求"},
            {"id": "result", "type": "output", "data": {"template": "{{latest}}"}},
        ],
        "edges": [{"from": "research", "to": "result"}],
    }
    normalized = _normalize_workflow_result(raw, {"coding"})
    workflow = ProjectSpec.model_validate({"version": "1", "name": "x", "workflows": [normalized]}).workflows[0]
    assert workflow.nodes[0].type == "agent"
    assert workflow.nodes[0].data["agent_id"] == "coding"
    assert workflow.nodes[0].data["prompt"] == "调研市场需求"
    assert workflow.edges[0].source == "research"
    assert workflow.edges[0].target == "result"


def test_generator_normalizes_common_model_node_type_aliases():
    raw = {
        "id": "flow", "name": "选品流程",
        "nodes": [
            {"id": "ask", "type": "human", "name": "收集输入"},
            {"id": "research", "type": "coding", "name": "调研", "prompt": "完成调研"},
            {"id": "done", "type": "end", "name": "结束"},
        ],
        "edges": [{"source": "ask", "target": "research"}, {"source": "research", "target": "done"}],
    }
    normalized = _normalize_workflow_result(raw, {"coding"})
    workflow = ProjectSpec.model_validate({"version": "1", "name": "x", "workflows": [normalized]}).workflows[0]
    assert [node.type for node in workflow.nodes] == ["manual_trigger", "agent", "output"]
    assert workflow.nodes[1].data["agent_id"] == "coding"


def test_generator_normalizes_workflow_name_and_config_alias():
    raw = {
        "id": "flow",
        "nodes": [{"id": "worker", "type": "agent", "config": {"agent_id": "coding"}}],
        "edges": [],
    }
    normalized = _normalize_workflow_result(raw, {"coding"})
    workflow = ProjectSpec.model_validate({"version": "1", "name": "x", "workflows": [normalized]}).workflows[0]
    assert workflow.name == "flow"
    assert workflow.nodes[0].data["agent_id"] == "coding"


def test_generator_normalizes_task_and_input_mapping_data_aliases():
    raw = {
        "id": "flow", "name": "代码审查",
        "nodes": [
            {"id": "agent", "type": "agent", "data": {
                "agent_id": "coding", "description": "审查",
                "task": "审查 {{input}}", "input_mapping": {"code": "{{input}}"},
            }},
            {"id": "done", "type": "output", "data": {
                "input_mapping": {"conclusion": "{{agent.output}}"},
            }},
        ],
        "edges": [{"source": "agent", "target": "done"}],
    }
    normalized = _normalize_workflow_result(raw, {"coding"})
    workflow = ProjectSpec.model_validate({"version": "1", "name": "x", "workflows": [normalized]}).workflows[0]
    assert workflow.nodes[0].data["prompt"] == "审查 {{input}}"
    assert workflow.nodes[0].data["inputs"] == {"code": "{{input}}"}
    assert workflow.nodes[1].data["template"] == "{{agent.output}}"


def test_incremental_rebuild_rejects_delete_all_nodes():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "旧流程",
        "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "done", "type": "output", "data": {}},
        ],
        "edges": [{"source": "start", "target": "done"}],
    })
    with pytest.raises(RuntimeError, match="不能删除全部当前节点"):
        _apply_incremental_step(
            workflow,
            {"action": "delete_node", "node_ids": ["start", "done"]},
            set(),
        )


def test_parse_result_extracts_largest_json_from_prose_and_fence():
    text = '示例：{"id":"short"}\n```json\n{"id":"flow","nodes":[],"edges":[]}\n```\n以上是结果。'
    assert _parse_result(text) == {"id": "flow", "nodes": [], "edges": []}


def test_generator_retries_invalid_structured_result_once(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    responses = iter(("这不是 JSON", '<result>{"ok":true}</result>'))
    prompts = []

    def fake_invoke(*args):
        prompts.append(args[-1])
        return next(responses)

    manager._invoke = fake_invoke
    generation = Generation(id="retry", workflow_id="flow", base_etag="etag", draft={}, prompt="x")
    assert manager._invoke_result(generation, None, [], tmp_path, "生成结果", "候选工作流") == {"ok": True}
    assert len(prompts) == 2
    assert "上一次没有返回可解析" in prompts[1]


def test_generator_writes_redacted_opencode_jsonl(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "logs" / "opencode.jsonl"
    monkeypatch.setenv("OPENAGENT_OPENCODE_LOG", str(log_path))
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="log-call", workflow_id="flow", base_etag="etag", draft={}, prompt="x")

    manager._write_opencode_log(generation, {
        "purpose": "候选工作流 1",
        "status": "failed",
        "diagnostics": ["Authorization: Bearer secret-value", "api_key=sk-12345678901234567890"],
    })

    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record["generation_id"] == "log-call"
    assert record["purpose"] == "候选工作流 1"
    assert "secret-value" not in json.dumps(record)
    assert "sk-12345678901234567890" not in json.dumps(record)


def test_generator_opencode_log_records_activity_metrics(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "logs" / "opencode.jsonl"
    monkeypatch.setenv("OPENAGENT_OPENCODE_LOG", str(log_path))
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="activity-call", workflow_id="flow", base_etag="etag", draft={}, prompt="x")

    manager._write_opencode_log(generation, {
        "purpose": "增量构建第 1 层（迭代 1）",
        "status": "timeout",
        "protocol_events": 20,
        "event_counts": {"step-start": 3, "tool": 4, "reasoning": 3},
        "tool_counts": {"read": 3, "grep": 1},
        "reasoning_chars": 1200,
        "text_events": 0,
        "tool_events": 4,
        "last_event_type": "step-start",
        "last_tool": "grep",
        "last_activity_ms": 75000,
        "idle_at_exit_ms": 45000,
    })

    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record["protocol_events"] == 20
    assert record["tool_counts"] == {"read": 3, "grep": 1}
    assert record["reasoning_chars"] == 1200
    assert record["last_tool"] == "grep"
    assert record["idle_at_exit_ms"] == 45000


def _timeout_error(call_id: str, *, silent: bool = True) -> _OpenCodeTimeoutError:
    activity = {
        "output_chars": 0 if silent else 12,
        "diagnostics": [],
        "protocol_events": 1 if silent else 2,
        "event_counts": {"step_start": 1} if silent else {"step_start": 1, "text": 1},
        "tool_counts": {},
        "reasoning_chars": 0,
        "text_events": 0 if silent else 1,
        "tool_events": 0,
        "last_event_type": "step_start" if silent else "text",
        "last_tool": None,
        "last_activity_ms": 4800,
        "idle_at_exit_ms": 55200,
    }
    return _OpenCodeTimeoutError(
        f"OpenCode 单次调用超时（60 秒）：{call_id}",
        call_id=call_id,
        purpose="增量构建第 2 层（迭代 3）",
        timeout_seconds=60,
        activity=activity,
    )


def test_repair_silent_timeout_retries_same_prompt_once(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="timeout-retry", workflow_id="flow", base_etag="etag", draft={}, prompt="x")
    calls = []

    def fake_invoke(*args, **kwargs):
        calls.append((args[4], kwargs))
        if len(calls) == 1:
            raise _timeout_error("call-1")
        return '<result>{"action":"complete"}</result>'

    manager._invoke = fake_invoke
    result = manager._invoke_result(
        generation, None, [], tmp_path, "同一修复提示", "增量修复",
        timeout_seconds=60, retry_silent_timeout=True, retry_node_id="worker",
    )

    assert result == {"action": "complete"}
    assert [item[0] for item in calls] == ["同一修复提示", "同一修复提示"]
    assert calls[1][1]["call_attempt"] == 2
    assert calls[1][1]["previous_call_id"] == "call-1"
    retry = next(item for item in generation.events if item["event"] == "generation.repairing")
    assert retry["data"]["phase"] == "model_timeout"
    assert retry["data"]["attempt"] == 2


def test_model_timeout_keeps_retrying_until_result_without_stalling(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="timeout-until-success", workflow_id="flow", base_etag="etag", draft={}, prompt="x")
    calls = 0

    def fake_invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _timeout_error(f"call-{calls}")
        return '<result>{"action":"complete"}</result>'

    manager._invoke = fake_invoke
    result = manager._invoke_result(
        generation, None, [], tmp_path, "同一修复提示", "增量修复",
        timeout_seconds=60, retry_silent_timeout=True, retry_node_id="worker",
    )

    assert result == {"action": "complete"}
    assert calls == 3
    assert not any(item["event"] == "generation.stalled" for item in generation.events)
    retries = [item for item in generation.events if item["event"] == "generation.repairing"]
    assert [item["data"]["attempt"] for item in retries] == [2, 3]


def test_model_timeout_retry_honors_user_cancellation(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="timeout-cancel", workflow_id="flow", base_etag="etag", draft={}, prompt="x")

    def fake_invoke(*_args, **_kwargs):
        generation.cancelled = True
        raise _timeout_error("cancelled-call")

    manager._invoke = fake_invoke
    with pytest.raises(RuntimeError, match="生成已取消"):
        manager._invoke_result(generation, None, [], tmp_path, "提示", "修复")


def test_retry_call_logs_attempt_and_previous_call_id(monkeypatch, tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(
        id="retry-log", workflow_id="flow", base_etag="etag", draft={},
        prompt="x", model="provider/model",
    )
    records = []

    class FakeStdin:
        def write(self, _value):
            return None

        def close(self):
            return None

    class FakeProcess:
        pid = 1234
        stdin = FakeStdin()
        stdout = []

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr("openagent_studio.generator.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess())
    manager._write_opencode_log = lambda _generation, data: records.append(data)

    assert manager._invoke(
        generation, ProjectSpec(name="测试"), ["opencode", "run"], tmp_path, "提示",
        timeout_seconds=60, purpose="修复重试", call_attempt=2, previous_call_id="call-1",
    ) == ""

    assert [record["status"] for record in records] == ["started", "completed"]
    assert all(record["attempt"] == 2 for record in records)
    assert all(record["previous_call_id"] == "call-1" for record in records)


def test_two_repair_timeouts_fail_without_stall_dialog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("OPENAGENT_MODEL_TIMEOUT_RETRIES", "1")
    monkeypatch.setenv("OPENAGENT_INCREMENTAL_MAX_ITERATIONS", "1")
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "超时暂停", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "done", "type": "output", "data": {"template": "旧结果"}},
        ]}],
    })
    store.save(project)
    original_etag = store.etag()
    manager = GeneratorManager(store)
    generation = Generation(
        id="timeout-stall", workflow_id="flow", base_etag=original_etag,
        draft=project.workflows[0].model_dump(mode="json"), prompt="修复输出", model="provider/model",
        initial_failures=[{"phase": "full_evaluation", "node_id": "done", "message": "结果错误"}],
    )
    calls = 0

    def fake_invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _timeout_error(f"call-{calls}")

    manager._invoke = fake_invoke
    manager._run(generation, project)

    assert calls == 2
    assert generation.events[-1]["event"] == "generation.failed"
    assert not any(item["event"] == "generation.stalled" for item in generation.events)
    timeout_failure = next(item for item in generation.events if item["event"] == "generation.layer_failed")
    assert timeout_failure["data"]["reason"] == "model_timeout"
    assert timeout_failure["data"]["node_id"] == "done"
    assert timeout_failure["data"]["call_ids"] == ["call-1", "call-2"]
    assert timeout_failure["data"]["timeout_seconds"] == 60
    assert store.etag() == original_etag
    assert store.load().workflows[0].nodes[0].data["template"] == "旧结果"


def test_second_active_repair_timeout_keeps_activity_without_stall_dialog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("OPENAGENT_MODEL_TIMEOUT_RETRIES", "1")
    monkeypatch.setenv("OPENAGENT_INCREMENTAL_MAX_ITERATIONS", "1")
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "活动超时", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "done", "type": "output", "data": {}},
        ]}],
    })
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="active-timeout-stall", workflow_id="flow", base_etag=store.etag(),
        draft=project.workflows[0].model_dump(mode="json"), prompt="修复输出", model="provider/model",
        initial_failures=[{"phase": "full_evaluation", "node_id": "done"}],
    )
    calls = 0

    def fake_invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _timeout_error(f"call-{calls}", silent=calls == 1)

    manager._invoke = fake_invoke
    manager._run(generation, project)

    assert generation.events[-1]["event"] == "generation.failed"
    assert not any(item["event"] == "generation.stalled" for item in generation.events)
    timeout_failure = next(item for item in generation.events if item["event"] == "generation.layer_failed")
    assert timeout_failure["data"]["timeout_attempts"][0]["silent"] is True
    assert timeout_failure["data"]["timeout_attempts"][1]["silent"] is False
    assert timeout_failure["data"]["last_activity"]["output_chars"] == 12


def test_planning_timeout_retries_then_fails_without_stall_dialog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("OPENAGENT_MODEL_TIMEOUT_RETRIES", "1")
    monkeypatch.setenv("OPENAGENT_INCREMENTAL_MAX_ITERATIONS", "1")
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "规划超时", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "done", "type": "output", "data": {}},
        ]}],
    })
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="planning-timeout", workflow_id="flow", base_etag=store.etag(),
        draft=project.workflows[0].model_dump(mode="json"), prompt="调整输出", model="provider/model",
    )
    calls = 0

    def fake_invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _timeout_error("planning-call")

    manager._invoke = fake_invoke
    manager._run(generation, project)

    assert calls == 2
    assert generation.events[-1]["event"] == "generation.failed"
    assert any("planning-call" in str(item["data"].get("message")) for item in generation.events if item["event"] == "generation.layer_failed")
    assert not any(item["event"] == "generation.stalled" for item in generation.events)


def test_generator_normalizes_evaluation_collection_fields():
    normalized = _normalize_evaluation_result({"cases": [{
        "id": "normal", "name": "正常流程", "input": {},
        "assertions": {"path": "output", "operator": "exists"},
        "semantic_criteria": [
            {"description": "输出必须完整"},
            {"criterion": "关键数据注明来源"},
            "不得生成最终 Listing",
        ],
        "approvals": [], "mocks": {"research": {"ok": True}},
    }]})
    evaluation = WorkflowEvaluation.model_validate(normalized)
    assert evaluation.cases[0].semantic_criteria == ["输出必须完整", "关键数据注明来源", "不得生成最终 Listing"]
    assert evaluation.cases[0].approvals == {}
    assert evaluation.cases[0].assertions[0].path == "output"
    assert evaluation.cases[0].mocks[0].node_id == "research"


def test_generator_normalizes_evaluation_case_ids_to_slugs():
    normalized = _normalize_evaluation_result({"cases": [
        {"id": "pc_normal_full_flow", "name": "正常流程"},
        {"id": "PC boundary title length", "name": "边界"},
        {"id": "!!!", "name": "风险"},
        {"id": "pc-normal-full-flow", "name": "重复"},
    ]})
    assert [case["id"] for case in normalized["cases"]] == [
        "pc-normal-full-flow", "pc-boundary-title-length", "case-3", "pc-normal-full-flow-2",
    ]


def test_generator_normalizes_single_semantic_criterion_string():
    normalized = _normalize_evaluation_result({"cases": [{
        "id": "normal", "name": "正常流程", "input": {},
        "assertions": [{"path": "output", "operator": "exists"}],
        "semantic_criteria": "输出必须完整",
    }]})
    evaluation = WorkflowEvaluation.model_validate(normalized)
    assert evaluation.cases[0].semantic_criteria == ["输出必须完整"]


def test_generator_normalizes_evaluation_mock_target_and_timeout_bounds():
    normalized = _normalize_evaluation_result({"cases": [{
        "id": "failure-risk", "name": "失败风险", "input": {},
        "assertions": [{"path": "output", "operator": "exists"}],
        "semantic_criteria": ["输出必须完整"],
        "mocks": [
            {"target": "product-info", "response": {"material_ratio": "UNKNOWN"}},
            {"target": "competitor-list", "response": {"claim": "patented structure"}},
            {"target": "market-sizing", "response": {"note": "估算并说明依据"}},
        ],
        "timeout_seconds": 3600,
    }]})
    evaluation = WorkflowEvaluation.model_validate(normalized)
    case = evaluation.cases[0]
    assert case.timeout_seconds == 1800
    assert [mock.node_id for mock in case.mocks] == ["product-info", "competitor-list", "market-sizing"]


def test_generator_normalizes_string_evaluation_timeout_bounds():
    normalized = _normalize_evaluation_result({"cases": [{
        "id": "normal", "name": "正常流程", "input": {},
        "assertions": [{"path": "output", "operator": "exists"}],
        "semantic_criteria": ["输出必须完整"],
        "timeout_seconds": "3600",
    }]})
    assert WorkflowEvaluation.model_validate(normalized).cases[0].timeout_seconds == 1800


def test_generator_retries_evaluation_with_missing_assertions_and_criteria(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="evaluation-repair", workflow_id="flow", base_etag="etag", draft={}, prompt="优化")
    invalid = {"cases": [
        {"id": "normal", "name": "正常", "assertions": [], "semantic_criteria": ["输出有效"]},
        {"id": "boundary", "name": "边界", "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": []},
        {"id": "failure", "name": "失败", "assertions": [], "semantic_criteria": []},
    ]}
    valid = {"cases": [
        {"id": case_id, "name": case_id, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["输出有效"]}
        for case_id in ("normal", "boundary", "failure")
    ]}
    responses = iter((invalid, valid))
    prompts = []

    def fake_invoke_result(*args):
        prompts.append(args[-2])
        return next(responses)

    manager._invoke_result = fake_invoke_result
    evaluation = manager._generate_evaluation(
        generation, ProjectSpec(name="测试"), ["opencode", "run"], tmp_path,
        "生成验收用例", WorkflowEvaluation(),
    )
    assert len(evaluation.cases) == 3
    assert all(case.assertions and case.semantic_criteria for case in evaluation.cases)
    assert len(prompts) == 2
    assert "normal 缺少 assertions 确定性断言" in prompts[1]
    assert "boundary 缺少 semantic_criteria 语义质量标准" in prompts[1]


def test_generator_retries_evaluation_when_comparison_expected_is_missing(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="evaluation-expected-repair", workflow_id="flow", base_etag="etag", draft={}, prompt="优化")
    invalid = {"cases": [
        {
            "id": case_id, "name": case_id, "input": value,
            "assertions": [{"path": "output", "operator": "equals"}],
            "semantic_criteria": ["输出与输入一致"],
        }
        for case_id, value in (("normal", "hello"), ("boundary", ""), ("failure", "特殊字符"))
    ]}
    valid = {"cases": [
        {
            "id": case_id, "name": case_id, "input": value,
            "assertions": [{"path": "output", "operator": "equals", "expected": value}],
            "semantic_criteria": ["输出与输入一致"],
        }
        for case_id, value in (("normal", "hello"), ("boundary", ""), ("failure", "特殊字符"))
    ]}
    responses = iter((invalid, valid))
    prompts = []

    def fake_invoke_result(*args):
        prompts.append(args[-2])
        return next(responses)

    manager._invoke_result = fake_invoke_result
    evaluation = manager._generate_evaluation(
        generation, ProjectSpec(name="测试"), ["opencode", "run"], tmp_path,
        "生成验收用例", WorkflowEvaluation(),
    )
    assert [case.assertions[0].expected for case in evaluation.cases] == ["hello", "", "特殊字符"]
    assert len(prompts) == 2
    assert "normal 缺少 非 exists 断言缺少 expected（output:equals）" in prompts[1]


def test_generator_reports_case_ids_when_evaluation_requirements_are_missing():
    evaluation = WorkflowEvaluation.model_validate({"cases": [
        {"id": "missing-check", "name": "缺断言", "semantic_criteria": ["输出有效"]},
        {"id": "missing-quality", "name": "缺标准", "assertions": [{"path": "output", "operator": "exists"}]},
        {"id": "complete", "name": "完整", "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["输出有效"]},
    ]})
    try:
        GeneratorManager._validate_case_update(WorkflowEvaluation(), evaluation)
    except RuntimeError as exc:
        assert "missing-check 缺少 assertions 确定性断言" in str(exc)
        assert "missing-quality 缺少 semantic_criteria 语义质量标准" in str(exc)
    else:
        raise AssertionError("缺少验收要求的用例不应通过")


def test_generator_call_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv("OPENCODE_GENERATOR_CALL_TIMEOUT", "5")
    assert _invoke_timeout_seconds() == 30
    monkeypatch.setenv("OPENCODE_GENERATOR_CALL_TIMEOUT", "9999")
    assert _invoke_timeout_seconds() == 1800


def test_generator_call_timeout_default_is_unbounded(monkeypatch):
    monkeypatch.delenv("OPENCODE_GENERATOR_CALL_TIMEOUT", raising=False)
    assert _invoke_timeout_seconds() == 0


def test_generator_repair_timeout_default_is_unbounded(monkeypatch):
    monkeypatch.delenv("OPENCODE_REPAIR_CALL_TIMEOUT", raising=False)
    monkeypatch.delenv("OPENAGENT_INCREMENTAL_PROBE_TIMEOUT", raising=False)
    monkeypatch.delenv("OPENAGENT_INCREMENTAL_MAX_ITERATIONS", raising=False)
    assert _repair_timeout_seconds() == 0
    assert _incremental_probe_timeout_seconds() == 120
    assert _incremental_max_iterations() == 100


def test_windows_startup_forces_agent_loop_when_mode_is_not_explicit():
    script = (Path(__file__).parents[1] / "scripts" / "start-studio.ps1").read_text(encoding="utf-8")
    assert '$env:OPENAGENT_GENERATOR_MODE = if ($GeneratorMode) { $GeneratorMode } else { "agent_loop" }' in script


def test_shell_startup_defaults_to_agent_loop():
    script = (Path(__file__).parents[1] / "scripts" / "start-studio.sh").read_text(encoding="utf-8")
    assert 'export OPENAGENT_GENERATOR_MODE="${OPENAGENT_GENERATOR_MODE:-agent_loop}"' in script


def test_generator_compaction_limits_are_bounded(monkeypatch):
    monkeypatch.setenv("OPENCODE_COMPACTION_TIMEOUT", "5")
    monkeypatch.setenv("OPENCODE_COMPACT_PROMPT_LENGTH", "10")
    assert _compaction_timeout_seconds() == 30
    assert _compact_prompt_length() == 4000


def test_generator_prepares_long_prompt_with_real_compaction_path(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENCODE_COMPACT_PROMPT_LENGTH", "4000")
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="compact", workflow_id="flow", base_etag="etag", draft={}, prompt="x", model="provider/model")
    calls = []

    def fake_compact(*args):
        calls.append(args[3])
        return "保留重点后的提示词"

    manager._compact_prompt = fake_compact
    prompt = "原始上下文" * 1000
    prepared = manager._prepare_prompt(generation, ProjectSpec(name="测试"), ["opencode", "run"], tmp_path, prompt)
    assert prepared == "保留重点后的提示词"
    assert calls == [prompt]
    assert [item["event"] for item in generation.events] == ["generation.context_compacting", "generation.context_compacted"]
    assert generation.events[-1]["data"] == {"before_chars": len(prompt), "after_chars": len(prepared), "used": True}


def test_generator_surfaces_compaction_failure_without_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENCODE_COMPACT_PROMPT_LENGTH", "4000")
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="compact-failure", workflow_id="flow", base_etag="etag", draft={}, prompt="x", model="provider/model")
    manager._compact_prompt = lambda *_args: (_ for _ in ()).throw(RuntimeError("提炼服务失败"))

    try:
        manager._prepare_prompt(generation, ProjectSpec(name="测试"), ["opencode", "run"], tmp_path, "长上下文" * 1000)
    except RuntimeError as exc:
        assert str(exc) == "提炼服务失败"
    else:
        raise AssertionError("上下文提炼错误不应被兜底吞掉")
    assert [item["event"] for item in generation.events] == ["generation.context_compacting"]


def test_generator_retries_empty_compaction_once_with_same_path(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENCODE_COMPACT_PROMPT_LENGTH", "4000")
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="compact-empty", workflow_id="flow", base_etag="etag", draft={}, prompt="x", model="provider/model")
    calls = []

    def compact(*args):
        calls.append(args[3])
        if len(calls) == 1:
            raise _EmptyCompactionError("OpenCode 内部上下文提炼没有返回内容")
        return "严格重试后的非空摘要"

    manager._compact_prompt = compact
    prompt = "必须完整保留的工作流上下文" * 1000

    assert manager._prepare_prompt(generation, ProjectSpec(name="测试"), ["opencode"], tmp_path, prompt) == "严格重试后的非空摘要"
    assert calls[0] == prompt
    assert "上一次提炼进程成功退出但返回了空内容" in calls[1]
    assert prompt in calls[1]
    assert [item["event"] for item in generation.events] == [
        "generation.context_compacting", "generation.context_compaction_retry", "generation.context_compacted",
    ]


def test_generator_surfaces_second_empty_compaction(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENCODE_COMPACT_PROMPT_LENGTH", "4000")
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="compact-empty-twice", workflow_id="flow", base_etag="etag", draft={}, prompt="x", model="provider/model")
    manager._compact_prompt = lambda *_args: (_ for _ in ()).throw(_EmptyCompactionError("empty"))

    try:
        manager._prepare_prompt(generation, ProjectSpec(name="测试"), ["opencode"], tmp_path, "长上下文" * 1000)
    except _EmptyCompactionError as exc:
        assert "自动严格重试 1 次后仍没有返回内容" in str(exc)
    else:
        raise AssertionError("连续两次空提炼不应被兜底吞掉")


def test_generator_keeps_full_prompt_after_compaction_timeout(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENCODE_COMPACT_PROMPT_LENGTH", "4000")
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    generation = Generation(id="compact-timeout", workflow_id="flow", base_etag="etag", draft={}, prompt="x", model="provider/model")
    calls = []

    def time_out(*args):
        calls.append(args[3])
        raise _CompactionTimeoutError("OpenCode 上下文提炼超时（30 秒）")

    manager._compact_prompt = time_out
    prompt = "必须原样保留的完整上下文" * 1000
    assert manager._prepare_prompt(generation, ProjectSpec(name="测试"), ["opencode", "run"], tmp_path, prompt) == prompt
    assert generation.compaction_disabled is True
    assert calls == [prompt]
    assert [item["event"] for item in generation.events] == [
        "generation.context_compacting", "generation.context_compaction_failed",
    ]
    assert generation.events[-1]["data"] == {
        "before_chars": len(prompt),
        "message": "OpenCode 上下文提炼超时（30 秒）",
        "fallback": "original",
    }

    second_prompt = prompt + "第二个候选"
    assert manager._prepare_prompt(generation, ProjectSpec(name="测试"), ["opencode", "run"], tmp_path, second_prompt) == second_prompt
    assert calls == [prompt]


def _wait_for(predicate, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for workflow run")


def test_workflow_run_api_executes_non_agent_nodes(tmp_path: Path):
    app = create_app(tmp_path / "project.yaml")
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
        "harness": [{"id": "coding", "name": "Coding", "backend_id": "default", "agent_id": "coding"}],
        "workflows": [{"id": "flow", "name": "测试流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "{{input}}"}},
            {"id": "approval", "type": "approval", "data": {"description": "确认结果"}},
            {"id": "output", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "approval"}, {"source": "approval", "target": "output"}]}],
    })
    calls = []

    def handler(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "submit":
            return {"id": "task-1", "status": "queued"}
        if operation == "task":
            return {"id": "task-1", "status": "completed"}
        if operation == "logs":
            return {"items": [{"line": "done"}], "next_cursor": 1}
        if operation == "result":
            return {"task_id": "task-1", "status": "completed", "output": {"type": "text", "text": "done"}, "logs": {}, "verification": {}, "artifacts": [], "error": None}
        raise AssertionError(operation)

    manager = WorkflowManager(poll_interval=0.001, client_factory=lambda _backend_id: FakeHarnessClient(handler))
    run = manager.start(project, "flow", {"input": "完成任务"})
    _wait_for(lambda: run.node_states["approval"]["status"] == "waiting")
    manager.approve(run.id, "approval", True, "可以发布")
    _wait_for(lambda: run.status == "completed")
    assert calls[0][0] == "submit"
    assert calls[0][1]["prompt"] == "完成任务"
    assert calls[0][1]["metadata"]["workflow_node_id"] == "agent"
    assert calls[0][1]["idempotency_key"].startswith("openagent-")
    assert run.outputs["output"]["approved"] is True


def test_code_review_does_not_forward_untrusted_code_to_test_runner():
    project = SpecStore(Path(__file__).parents[1] / "project.yaml").load()
    workflow = next(item for item in project.workflows if item.id == "workflow-78d4b5")
    assert workflow.nodes[1].data["agent_id"] == "coding"
    assert workflow.nodes[4].data["agent_id"] == "test-runner"
    submitted: list[dict] = []
    untrusted_code = "print('SHOULD_NOT_EXECUTE')\n__import__('os').system('echo PWNED')"

    def handler(operation, **kwargs):
        if operation == "submit":
            submitted.append(kwargs)
            return {"id": f"task-{len(submitted)}", "status": "completed"}
        if operation == "logs":
            return {"items": [], "next_cursor": 0}
        if operation == "result":
            return {"output": {"type": "text", "text": "完成"}}
        raise AssertionError(operation)

    manager = WorkflowManager(poll_interval=0.001, client_factory=lambda _backend_id: FakeHarnessClient(handler))
    run = manager.start(project, workflow.id, {"input": untrusted_code})
    _wait_for(lambda: run.node_states["human_approval"]["status"] == "waiting")
    assert untrusted_code in submitted[0]["prompt"]

    manager.approve(run.id, "human_approval", True)
    _wait_for(lambda: run.status == "completed")
    assert untrusted_code not in submitted[1]["prompt"]


def test_workflow_runner_rejected_approval_uses_false_branch_without_failing():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "审批拒绝分支",
        "workflows": [{"id": "flow", "name": "审批", "nodes": [
            {"id": "input", "type": "manual_trigger", "data": {}},
            {"id": "approval", "type": "approval", "data": {"description": "确认结果"}},
            {"id": "tests", "type": "prompt", "data": {"template": "TESTS_RAN"}},
            {"id": "approved-output", "type": "output", "data": {}},
            {"id": "rejected-output", "type": "output", "data": {"template": "{{latest}}"}},
        ], "edges": [
            {"source": "input", "target": "approval"},
            {"source": "approval", "target": "tests"},
            {"source": "tests", "target": "approved-output"},
            {"source": "approval", "target": "rejected-output", "condition": "false"},
        ]}],
    })
    manager = WorkflowManager()
    run = manager.start(project, "flow", {"input": "待审内容"})
    _wait_for(lambda: run.node_states["approval"]["status"] == "waiting")
    manager.approve(run.id, "approval", False, "需要修改")
    _wait_for(lambda: run.status == "completed")
    assert run.node_states["tests"]["status"] == "skipped"
    rejected = json.loads(run.outputs["rejected-output"])
    assert rejected["approved"] is False
    assert rejected["comment"] == "需要修改"


def test_workflow_runner_rejected_approval_reaches_downstream_condition():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "审批后条件分支",
        "workflows": [{"id": "flow", "name": "审批后条件分支", "nodes": [
            {"id": "input", "type": "manual_trigger", "data": {}},
            {"id": "approval", "type": "approval", "data": {"description": "确认结果"}},
            {"id": "condition", "type": "condition", "data": {"expression": "latest.approved == true"}},
            {"id": "approved-output", "type": "output", "data": {}},
            {"id": "rejected-output", "type": "output", "data": {}},
        ], "edges": [
            {"source": "input", "target": "approval"},
            {"source": "approval", "target": "condition"},
            {"source": "condition", "target": "approved-output", "condition": "true"},
            {"source": "condition", "target": "rejected-output", "condition": "false"},
        ]}],
    })
    manager = WorkflowManager()
    run = manager.start(project, "flow", {"input": "待审内容"})
    _wait_for(lambda: run.node_states["approval"]["status"] == "waiting")
    manager.approve(run.id, "approval", False, "证据不足")
    _wait_for(lambda: run.status == "completed")
    assert run.node_states["condition"]["status"] == "completed"
    assert run.outputs["condition"] is False
    assert run.node_states["approved-output"]["status"] == "skipped"
    assert run.node_states["rejected-output"]["status"] == "completed"


def test_workflow_runner_allows_intermediate_probe_without_output_node():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "中间层探测",
        "harness": [{"id": "coding", "name": "Coding", "agent_id": "coding"}],
        "workflows": [{"id": "flow", "name": "首节点", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "执行探测"}},
        ], "edges": []}],
    })

    def handler(operation, **kwargs):
        if operation == "submit":
            return {"id": "probe-task", "status": "queued"}
        if operation == "task":
            return {"id": "probe-task", "status": "completed"}
        if operation == "logs":
            return {"items": [], "next_cursor": 0}
        if operation == "result":
            return {"output": {"type": "text", "text": "probe ok"}}
        raise AssertionError(operation)

    manager = WorkflowManager(poll_interval=0.001, client_factory=lambda _backend_id: FakeHarnessClient(handler))
    run = manager.start(project, "flow", {"input": "probe"}, record=False, require_output=False)
    _wait_for(lambda: run.status == "completed")
    assert run.outputs["agent"]["text"] == "probe ok"


def test_workflow_runner_renders_generator_node_references_and_agent_inputs():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "生成器引用兼容",
        "harness": [{"id": "coding", "name": "Coding", "agent_id": "coding"}],
        "workflows": [{"id": "review", "name": "代码审查", "nodes": [
            {"id": "manual-trigger", "type": "manual_trigger", "data": {}},
            {"id": "code-review-agent", "type": "agent", "data": {
                "agent_id": "coding", "prompt": "审查代码",
                "inputs": {"code": "{{manual-trigger.code}}"},
            }},
            {"id": "review-output", "type": "output", "data": {
                "template": "{{code-review-agent.output}}",
            }},
        ], "edges": [
            {"source": "manual-trigger", "target": "code-review-agent"},
            {"source": "code-review-agent", "target": "review-output"},
        ]}],
    })
    calls = []

    def handler(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "submit":
            return {"id": "task-1", "status": "completed"}
        if operation == "logs":
            return {"items": [], "next_cursor": 0}
        if operation == "result":
            return {"output": {"type": "text", "text": "总体结论：不通过"}}
        raise AssertionError(operation)

    manager = WorkflowManager(client_factory=lambda _backend_id: FakeHarnessClient(handler))
    run = manager.start(project, "review", {"input": {"code": "result = total / 0"}})
    _wait_for(lambda: run.status == "completed")
    submitted = next(kwargs for operation, kwargs in calls if operation == "submit")
    assert '"code": "result = total / 0"' in submitted["prompt"]
    assert run.outputs["review-output"] == "总体结论：不通过"


def test_workflow_runner_uses_backend_and_catalog_agent_after_spec_round_trip(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "独立 Harness",
        "harness": [{"id": "coding", "name": "Coding", "backend_id": "remote", "agent_id": "catalog-coding"}],
        "workflows": [{"id": "flow", "name": "测试流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "{{input}}"}},
            {"id": "output", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "output"}]}],
    })
    store.save(project)
    reloaded = store.load()
    calls = []
    backends = []

    def handler(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "submit":
            return {"id": "task-1", "status": "queued"}
        if operation == "task":
            return {"id": "task-1", "status": "completed"}
        if operation == "logs":
            return {"items": [{"line": "done"}], "next_cursor": 1}
        if operation == "result":
            return {"output": {"type": "text", "text": "done"}}
        raise AssertionError(operation)

    def client_factory(backend_id):
        backends.append(backend_id)
        return FakeHarnessClient(handler)

    manager = WorkflowManager(poll_interval=0.001, client_factory=client_factory)
    run = manager.start(reloaded, "flow", {"input": "完成任务"})
    _wait_for(lambda: run.status == "completed")
    assert backends == ["remote"]
    assert calls[0][1]["agent_id"] == "catalog-coding"
    assert calls[0][1]["prompt"] == "完成任务"
    assert run.outputs["output"] == "done"


def test_evaluator_stops_repairs_for_missing_harness_task_contract(monkeypatch):
    project = ProjectSpec.model_validate({
        "version": "1", "name": "外部 Harness",
        "harness": [{"id": "coding", "name": "Coding", "backend_id": "default", "agent_id": "coding"}],
        "workflows": [{
            "id": "flow", "name": "测试流程",
            "nodes": [
                {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "{{input}}"}},
                {"id": "output", "type": "output", "data": {}},
            ],
            "edges": [{"source": "agent", "target": "output"}],
            "evaluation": {"cases": [{
                "id": "normal", "name": "正常", "input": "x",
                "assertions": [{"path": "output", "operator": "exists"}],
                "semantic_criteria": ["输出有效"],
            }]},
        }],
    })

    def missing_v1_contract(operation, **_kwargs):
        assert operation == "submit"
        raise HarnessAPIError("invalid_error_response", "not found", True, None, 404)

    monkeypatch.setattr(WorkflowEvaluator, "ensure_harness_ready", lambda self, *_args: None)
    monkeypatch.setattr(
        "openagent_studio.workflow_runner.create_harness_client",
        lambda *_args, **_kwargs: FakeHarnessClient(missing_v1_contract),
    )
    evaluator = WorkflowEvaluator(
        lambda prompt, value: value,
        lambda workflow, case, output: SemanticVerdict(True, 100, []),
        live_execution=True,
    )
    try:
        evaluator.evaluate(project, project.workflows[0], 0)
    except HarnessInfrastructureError as exc:
        assert "Harness 任务 API 契约不兼容" in str(exc)
        assert "候选工作流未进入无效修复" in str(exc)
    else:
        raise AssertionError("缺少任务 API 契约时不应进入候选修复")


def test_harness_blocked_task_is_not_repaired_through_management_api():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "环境归 Harness 管理",
        "harness": [{"id": "coding", "name": "Coding", "backend_id": "default", "agent_id": "coding"}],
        "workflows": [{"id": "flow", "name": "恢复流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "{{input}}"}},
            {"id": "output", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "output"}]}],
    })
    calls = []

    def handler(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "submit":
            return {"id": "drifted-task", "status": "queued"}
        if operation == "task":
            return {"id": "drifted-task", "status": "blocked", "blocked_reason": "setup required", "error_code": "setup_required", "setup_required": True}
        raise AssertionError(operation)

    manager = WorkflowManager(poll_interval=0.001, client_factory=lambda _backend_id: FakeHarnessClient(handler))
    run = manager.start(project, "flow", {"input": "执行"})
    _wait_for(lambda: run.status == "failed")
    assert run.error == "setup required"
    assert [operation for operation, _ in calls] == ["submit", "task"]


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
    app = create_app(tmp_path / "project.yaml")
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


def test_manual_trigger_requires_non_blank_input_but_webhook_trigger_does_not():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "触发输入",
        "workflows": [{"id": "flow", "name": "触发流程", "nodes": [
            {"id": "manual", "type": "manual_trigger", "data": {}},
            {"id": "hook", "type": "webhook", "data": {"path": "/hooks/trigger-input", "method": "POST"}},
            {"id": "manual-output", "type": "output", "data": {"template": "manual"}},
            {"id": "hook-output", "type": "output", "data": {"template": "hook"}},
        ], "edges": [
            {"source": "manual", "target": "manual-output"},
            {"source": "hook", "target": "hook-output"},
        ]}],
    })
    manager = WorkflowManager()

    with pytest.raises(ValueError, match="手动触发器.*输入"):
        manager.start(project, "flow", {"input": ""})
    with pytest.raises(ValueError, match="手动触发器.*输入"):
        manager.start(project, "flow", {"input": " \n\t"})
    with pytest.raises(ValueError, match="手动触发器.*输入"):
        manager.start(project, "flow", {"input": {}})

    manual_run = manager.start(project, "flow", {"input": "开始"})
    _wait_for(lambda: manual_run.status == "completed")
    assert manual_run.outputs["manual-output"] == "manual"

    webhook_run = manager.start(project, "flow", {"input": {}, "_trigger_node_id": "hook"})
    _wait_for(lambda: webhook_run.status == "completed")
    assert webhook_run.outputs["hook-output"] == "hook"


def test_manual_trigger_api_rejects_empty_input(tmp_path: Path):
    app = create_app(tmp_path / "project.yaml")
    client = TestClient(app)
    payload = {"version": "1", "name": "人工触发", "workflows": [{
        "id": "flow", "name": "人工流程",
        "nodes": [
            {"id": "manual", "type": "manual_trigger", "data": {}},
            {"id": "output", "type": "output", "data": {}},
        ],
        "edges": [{"source": "manual", "target": "output"}],
    }]}
    assert client.put("/api/spec", json=payload).status_code == 200

    response = client.post("/api/workflows/flow/runs", json={"input": "  \n"})

    assert response.status_code == 422
    assert "手动触发器" in response.json()["detail"]


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


def test_nested_subworkflows_do_not_starve_shared_executor():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "嵌套",
        "workflows": [
            {"id": "leaf", "name": "叶子", "nodes": [{"id": "result", "type": "output", "data": {"template": "{{input}}"}}]},
            {"id": "middle", "name": "中间", "nodes": [{"id": "child", "type": "subworkflow", "data": {"workflow_id": "leaf"}}, {"id": "result", "type": "output", "data": {}}], "edges": [{"source": "child", "target": "result"}]},
            {"id": "parent", "name": "父级", "nodes": [{"id": "left", "type": "subworkflow", "data": {"workflow_id": "middle"}}, {"id": "right", "type": "subworkflow", "data": {"workflow_id": "middle"}}, {"id": "result", "type": "output", "data": {}}], "edges": [{"source": "left", "target": "result"}, {"source": "right", "target": "result"}]},
        ],
    })
    manager = WorkflowManager(max_workers=2)
    run = manager.start(project, "parent", {"input": "ok"})
    _wait_for(lambda: run.status in {"completed", "failed"})
    assert run.status == "completed"
    manager.stop_scheduler()


def test_creation_step_rejects_template_braces_in_condition_expression():
    previous = WorkflowSpec.model_validate({
        "id": "flow", "name": "条件表达式", "nodes": [
            {"id": "approval", "type": "approval", "data": {}},
        ], "edges": [],
    })
    candidate = WorkflowSpec.model_validate({
        "id": "flow", "name": "条件表达式", "nodes": [
            {"id": "approval", "type": "approval", "data": {}},
            {"id": "condition", "type": "condition", "data": {
                "expression": "{{approval.approved}} == true",
            }},
        ], "edges": [{"source": "approval", "target": "condition"}],
    })
    errors = _creation_step_errors(previous, candidate, "add_node", "condition")
    assert any("不支持模板大括号" in error for error in errors)


def test_http_node_rejects_private_address_before_request():
    try:
        WorkflowManager._validate_http_url("https://127.0.0.1/admin", allow_private=False)
    except RuntimeError as exc:
        assert "禁止访问私网" in str(exc)
    else:
        raise AssertionError("private address should be rejected")


def test_http_node_streaming_response_limit():
    class ChunkStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"1234"
            yield b"5678"

    class StreamingClient:
        def stream(self, *_args, **_kwargs):
            request = httpx.Request("GET", "https://example.com/data")

            class ResponseContext:
                def __enter__(self):
                    self.response = httpx.Response(
                        200,
                        headers={"content-type": "text/plain"},
                        stream=ChunkStream(),
                        request=request,
                    )
                    return self.response

                def __exit__(self, *_exc):
                    self.response.close()

            return ResponseContext()

        def close(self):
            pass

    manager = WorkflowManager(max_http_response_bytes=6)
    manager._http_client.close()
    manager._http_client = StreamingClient()
    workflow_run = type("Run", (), {"input": "", "outputs": {}})()
    node = WorkflowNode(id="http", type="http_request", data={"url": "https://example.com/data"})
    manager._validate_http_url = lambda *_args: None
    with pytest.raises(RuntimeError, match="超过 6 字节限制"):
        manager._http_request(workflow_run, node, "")
    manager.stop_scheduler()


def test_http_node_renders_nested_structured_body_and_preserves_types():
    class ResponseContext:
        def __enter__(self):
            self.response = httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"ok":true}',
                request=httpx.Request("POST", "https://example.com/data"),
            )
            return self.response

        def __exit__(self, *_exc):
            self.response.close()

    class CapturingClient:
        def __init__(self):
            self.kwargs = None

        def stream(self, *_args, **kwargs):
            self.kwargs = kwargs
            return ResponseContext()

        def close(self):
            pass

    manager = WorkflowManager()
    client = CapturingClient()
    manager._http_client.close()
    manager._http_client = client
    workflow_run = type("Run", (), {
        "input": {"query": "test-query", "enabled": True},
        "outputs": {},
    })()
    node = WorkflowNode(id="http", type="http_request", data={
        "url": "https://example.com/data",
        "method": "POST",
        "body": {
            "query": "{{input.query}}",
            "enabled": "{{input.enabled}}",
            "count": 3,
            "items": ["prefix-{{input.query}}", "{{input}}", False, None],
        },
    })
    manager._validate_http_url = lambda *_args: None

    manager._http_request(workflow_run, node, workflow_run.input)

    assert client.kwargs["json"] == {
        "query": "test-query",
        "enabled": True,
        "count": 3,
        "items": ["prefix-test-query", {"query": "test-query", "enabled": True}, False, None],
    }
    assert client.kwargs["content"] is None
    assert node.data["body"]["query"] == "{{input.query}}"
    manager.stop_scheduler()


def test_workflow_and_generation_events_are_bounded():
    manager = WorkflowManager(max_retained_runs=1, max_run_events=2)
    project = ProjectSpec.model_validate({
        "version": "1", "name": "边界",
        "workflows": [{"id": "flow", "name": "流程", "nodes": [{"id": "done", "type": "output", "data": {}}]}],
    })
    first = manager.start(project, "flow")
    _wait_for(lambda: first.status == "completed")
    second = manager.start(project, "flow")
    _wait_for(lambda: second.status == "completed")
    assert len(first.events) == 2
    assert [item["sequence"] for item in first.events] == [first.next_event_sequence - 2, first.next_event_sequence - 1]
    assert list(manager.runs) == [second.id]
    generation = Generation(id="bounded", workflow_id="flow", base_etag="etag", draft={}, prompt="x", max_events=2)
    for index in range(4):
        generation.emit("tick", {"index": index})
    assert [item["sequence"] for item in generation.events] == [2, 3]
    manager.stop_scheduler()


def test_harness_payloads_are_bounded():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "有界 Harness",
        "harness": [{"id": "coding", "name": "Coding", "agent_id": "coding"}],
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "coding", "prompt": "run"}},
            {"id": "output", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "output"}]}],
    })

    def handler(operation, **kwargs):
        if operation == "submit":
            return {"id": "large", "status": "completed"}
        if operation == "logs":
            return {"items": [{"line": "日志" * 20}, {"line": "尾部"}]}
        if operation == "result":
            return {"output": {"type": "text", "text": "结果" * 30}, "extra": "x" * 200}
        raise AssertionError(operation)

    manager = WorkflowManager(
        client_factory=lambda _backend: FakeHarnessClient(handler),
        max_harness_log_items=1,
        max_harness_log_bytes=100,
        max_harness_log_line_bytes=12,
        max_harness_result_bytes=80,
        max_harness_text_bytes=15,
    )
    run = manager.start(project, "flow")
    _wait_for(lambda: run.status == "completed")
    output = run.outputs["agent"]
    assert output["truncated"] == {"logs": True, "result": True, "text": True}
    assert len(output["logs"]) == 1
    assert output["logs"][0]["truncated"] is True
    assert output["result"]["truncated"] is True
    assert len(output["text"].encode("utf-8")) <= 15
    manager.stop_scheduler()


def test_workflow_run_concurrency_limit_rejects_excess_runs():
    manager = WorkflowManager(max_concurrent_runs=1)
    project = ProjectSpec.model_validate({
        "version": "1", "name": "并发",
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "approval", "type": "approval", "data": {}},
            {"id": "output", "type": "output", "data": {}},
        ], "edges": [{"source": "approval", "target": "output"}]}],
    })
    first = manager.start(project, "flow")
    _wait_for(lambda: first.node_states["approval"]["status"] == "waiting")
    with pytest.raises(RuntimeError, match="并发上限"):
        manager.start(project, "flow")
    manager.cancel(first.id)
    _wait_for(lambda: first.status == "cancelled")
    manager.stop_scheduler()


def test_workflow_sse_resumes_by_sequence(tmp_path: Path):
    app = create_app(tmp_path / "project.yaml")
    manager = app.state.workflow_manager
    project = ProjectSpec.model_validate({
        "version": "1", "name": "恢复",
        "workflows": [{"id": "flow", "name": "流程", "nodes": [{"id": "done", "type": "output", "data": {}}]}],
    })
    run = manager.start(project, "flow")
    _wait_for(lambda: run.status == "completed")
    response = TestClient(app).get(
        f"/api/workflow-runs/{run.id}/events",
        headers={"Last-Event-ID": "0"},
    )
    assert "id: 1" in response.text
    assert "id: 0" not in response.text


def test_spec_store_cache_invalidates_after_external_write(tmp_path: Path):
    path = tmp_path / "project.yaml"
    first = SpecStore(path)
    second = SpecStore(path)
    first.save(ProjectSpec(name="初始"))
    assert first.etag() == second.etag()
    assert first.load().name == "初始"
    second.save(ProjectSpec(name="外部更新"), expected=second.etag())
    assert first.load().name == "外部更新"
    assert first.etag() == second.etag()


def test_generator_retention_and_history_are_bounded(tmp_path: Path):
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"), max_generations=1, max_history_messages=2)
    old = Generation(id="old", workflow_id="flow", base_etag="x", draft={}, prompt="x", completed=True)
    old.emit("generation.completed", {})
    current = Generation(id="current", workflow_id="flow", base_etag="x", draft={}, prompt="x", completed=True)
    current.emit("generation.completed", {})
    manager.generations = {old.id: old, current.id: current}
    manager.active["flow"] = current.id
    manager.history["flow"] = [{"role": "user", "content": str(index)} for index in range(3)]
    manager.history["flow"] = manager.history["flow"][-manager.max_history_messages:]
    with manager._lock:
        manager._trim_generations_locked()
    assert list(manager.generations) == [current.id]
    assert [item["content"] for item in manager.history["flow"]] == ["1", "2"]



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


def test_evaluation_cases_run_in_parallel():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "并发验收", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "result", "type": "output", "data": {"template": "{{input}}"}},
        ],
        "edges": [{"source": "start", "target": "result"}],
        "evaluation": {"cases": [
            {
                "id": "case-one", "name": "用例一", "input": "one",
                "assertions": [{"path": "output", "operator": "equals", "expected": "one"}],
                "semantic_criteria": ["输出正确"],
            },
            {
                "id": "case-two", "name": "用例二", "input": "two",
                "assertions": [{"path": "output", "operator": "equals", "expected": "two"}],
                "semantic_criteria": ["输出正确"],
            },
        ]},
    })
    project = ProjectSpec.model_validate({"version": "1", "name": "并发验收", "workflows": [workflow]})
    lock = threading.Lock()
    active = 0
    peak = 0

    def semantic_judge(_workflow, _case, _output):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)
            return SemanticVerdict(True, 100)
        finally:
            with lock:
                active -= 1

    events = []
    evaluator = WorkflowEvaluator(lambda _prompt, value: value, semantic_judge, live_execution=True)
    result = evaluator.evaluate(project, workflow, 0, on_case=events.append)

    assert result.passed is True
    assert peak == 2
    assert [item["phase"] for item in events].count("started") == 2
    assert [item["phase"] for item in events].count("finished") == 2


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


def test_evaluation_mode_mocks_harness_tool():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "工具隔离",
        "harness": [{"id": "tool-agent", "name": "Tool", "backend_id": "default", "agent_id": "tool-agent"}],
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "agent", "type": "tool", "data": {"agent_id": "tool-agent", "prompt": "{{input}}"}},
            {"id": "result", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "result"}]}],
    })
    manager = WorkflowManager()
    run = manager.start(project, "flow", {"input": "x"}, policy=EvaluationPolicy(mocks={"agent": {"safe": True}}, model_inference=lambda *_: (_ for _ in ()).throw(AssertionError("tool must not run model"))), record=False)
    _wait_for(lambda: run.status == "completed")
    assert run.outputs["result"] == {"safe": True}


def test_live_acceptance_uses_formal_harness_execution_path():
    project = ProjectSpec.model_validate({
        "version": "1", "name": "真实验收",
        "harness": [{"id": "worker", "name": "Worker", "backend_id": "default", "agent_id": "worker"}],
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "agent", "type": "agent", "data": {"agent_id": "worker", "prompt": "真实执行 {{input}}"}},
            {"id": "result", "type": "output", "data": {}},
        ], "edges": [{"source": "agent", "target": "result"}]}],
    })
    calls = []

    def handler(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "submit":
            return {"id": "live-task", "status": "queued"}
        if operation == "task":
            return {"id": "live-task", "status": "completed"}
        if operation == "logs":
            return {"items": [{"line": "real harness result"}], "next_cursor": 1}
        if operation == "result":
            return {"output": {"type": "text", "text": "real harness result"}}
        raise AssertionError(operation)

    manager = WorkflowManager(poll_interval=0.001, client_factory=lambda _backend_id: FakeHarnessClient(handler))
    policy = EvaluationPolicy(live_execution=True, model_inference=lambda *_: (_ for _ in ()).throw(AssertionError("live validation must not bypass Harness")))
    run = manager.start(project, "flow", {"input": "任务"}, policy=policy, record=False)
    _wait_for(lambda: run.status == "completed")
    assert calls[0][0] == "submit"
    assert calls[0][1]["prompt"] == "真实执行 任务"
    assert run.outputs["result"] == "real harness result"
    assert run.id not in manager.runs


def test_creation_runtime_probe_uses_unsaved_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = ProjectSpec.model_validate({"version": "1", "name": "创建探测"})
    workflow = WorkflowSpec.model_validate({
        "id": "new-flow", "name": "新流程",
        "nodes": [{"id": "gate", "type": "condition", "data": {"expression": "true"}}],
    })
    generation = Generation(
        id="creation-probe", workflow_id="new-flow", base_etag="etag",
        draft=workflow.model_dump(mode="json"), prompt="创建", mode="create",
    )

    class CompletedRun:
        status = "completed"
        error = None
        error_code = None
        node_states = {"gate": {"status": "completed"}}

    class ProbeManager:
        def __init__(self, **_kwargs):
            pass

        def start(self, candidate_project, workflow_id, *_args, **_kwargs):
            assert workflow_id == "new-flow"
            assert [item.id for item in candidate_project.workflows] == ["new-flow"]
            return CompletedRun()

        def stop_scheduler(self):
            pass

    monkeypatch.setattr("openagent_studio.generator.WorkflowManager", ProbeManager)
    manager = GeneratorManager(SpecStore(tmp_path / "project.yaml"))
    evaluator = WorkflowEvaluator(
        lambda *_args: None,
        lambda *_args: SemanticVerdict(passed=True, score=100),
        live_execution=True,
    )

    assert manager._probe_incremental_workflow(
        generation, project, workflow, "gate", {}, {}, evaluator,
    ) == []


def test_incremental_probe_workflow_does_not_repeat_agent_before_approval():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "分层探测", "nodes": [
            {"id": "input", "type": "manual_trigger", "data": {}},
            {"id": "analysis", "type": "agent", "data": {"agent_id": "repository-analysis", "prompt": "分析"}},
            {"id": "approval", "type": "approval", "data": {}},
            {"id": "condition", "type": "condition", "data": {"expression": "latest.approved == true"}},
            {"id": "tests", "type": "agent", "data": {"agent_id": "test-runner", "prompt": "测试"}},
        ], "edges": [
            {"source": "input", "target": "analysis"},
            {"source": "analysis", "target": "approval"},
            {"source": "approval", "target": "condition"},
            {"source": "condition", "target": "tests", "condition": "true"},
        ],
    })
    probe = _incremental_probe_workflow(workflow, "tests")
    assert [node.id for node in probe.nodes] == ["approval", "condition", "tests"]
    assert [(edge.source, edge.target) for edge in probe.edges] == [
        ("approval", "condition"), ("condition", "tests"),
    ]


def test_incremental_delete_batches_nodes_and_removes_incident_edges():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "批量删除", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "middle-a", "type": "prompt", "data": {"template": "a"}},
            {"id": "middle-b", "type": "prompt", "data": {"template": "b"}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [
            {"source": "start", "target": "middle-a"},
            {"source": "middle-a", "target": "middle-b"},
            {"source": "middle-b", "target": "done"},
        ],
    })
    candidate, action, touched, *_ = _apply_incremental_step(
        workflow, {"action": "delete_node", "node_ids": ["middle-a", "middle-b"]}, set(),
    )
    assert action == "delete_node"
    assert touched == "middle-a,middle-b"
    assert [node.id for node in candidate.nodes] == ["start", "done"]
    assert candidate.edges == []


def test_incremental_delete_accepts_legacy_single_node_id():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "删除", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [{"source": "start", "target": "done"}],
    })
    candidate, action, touched, *_ = _apply_incremental_step(
        workflow, {"action": "delete_node", "node_id": "done"}, set(),
    )
    assert action == "delete_node"
    assert touched == "done"
    assert [node.id for node in candidate.nodes] == ["start"]
    assert candidate.edges == []



def test_generator_builds_one_node_per_layer_and_saves_after_full_verification(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({"version": "1", "name": "增量", "project_dir": str(tmp_path), "workflows": [{"id": "flow", "name": "原流程", "nodes": []}]})
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(id="incremental", workflow_id="flow", base_etag=store.etag(), draft=project.workflows[0].model_dump(mode="json"), prompt="创建输入后输出的流程", model="provider/model")
    steps = iter([
        {"action": "add_node", "node": {"id": "start", "type": "manual_trigger", "data": {"description": "接收输入"}}, "edges": [], "summary": "创建入口"},
        {"action": "add_node", "node": {"id": "done", "type": "output", "data": {"description": "输出结果"}}, "edges": [{"source": "start", "target": "done"}], "summary": "创建出口"},
        {"action": "complete", "summary": "入口和出口已经连通"},
    ])
    planning_prompts = []

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{
                "id": f"case-{index}", "name": f"用例 {index}", "input": index,
                "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["输出有效"],
            } for index in range(1, 4)]
            return f"<result>{json.dumps({'cases': cases}, ensure_ascii=False)}</result>"
        if "增量工作流构建器" in prompt:
            planning_prompts.append(prompt)
            return f"<result>{json.dumps(next(steps), ensure_ascii=False)}</result>"
        if "独立 OpenCode 验证智能体" in prompt:
            return '<result>{"passed":true,"score":90,"issues":[]}</result>'
        raise AssertionError(prompt)

    manager._invoke = fake_invoke
    manager._probe_incremental_workflow = lambda *_args: []
    manager._run(generation, project)
    assert len(planning_prompts) == 3
    assert all("一次新增一个节点" in prompt for prompt in planning_prompts)
    assert len([item for item in generation.events if item["event"] == "generation.layer_completed"]) == 2
    previews = [item for item in generation.events if item["event"] == "workflow.preview"]
    updates = [item for item in generation.events if item["event"] == "workflow.updated"]
    assert [item["data"]["workflow"]["nodes"][-1]["id"] for item in previews[:2]] == ["start", "done"]
    assert [item["data"]["workflow"]["nodes"][-1]["id"] for item in updates] == ["start", "done"]
    assert generation.events[-1]["event"] == "generation.completed", generation.events[-1]
    saved = store.load().workflows[0]
    assert [node.id for node in saved.nodes] == ["start", "done"]
    assert [(edge.source, edge.target) for edge in saved.edges] == [("start", "done")]
    assert len(saved.evaluation.cases) == 3


def test_generator_fails_before_model_calls_when_harness_is_unavailable(monkeypatch, tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "运行时预检", "project_dir": str(tmp_path),
        "harness": [{"id": "coding", "name": "Coding", "backend_id": "default", "agent_id": "coding"}],
        "workflows": [{"id": "flow", "name": "原流程", "nodes": []}],
    })
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="preflight", workflow_id="flow", base_etag=store.etag(),
        draft=project.workflows[0].model_dump(mode="json"), prompt="创建节点", model="provider/model",
    )
    monkeypatch.setattr("openagent_studio.generator.resolve_executable", lambda *_: "opencode")
    monkeypatch.setattr(
        "openagent_studio.evaluation.create_harness_client",
        lambda *_args, **_kwargs: FakeHarnessClient(
            lambda operation, **_values: (_ for _ in ()).throw(httpx.ConnectError("connection refused"))
        ),
    )
    manager._invoke = lambda *_args: (_ for _ in ()).throw(AssertionError("模型不应在 Harness 预检失败后被调用"))

    manager._run(generation, project)

    stages = [item["data"]["stage"] for item in generation.events if item["event"] == "generation.stage"]
    assert stages == ["checking_runtime"]
    assert generation.events[-1]["event"] == "generation.failed"
    assert "Harness 验收运行时不可用" in generation.events[-1]["data"]["message"]


def test_generator_invalid_incremental_layers_preserve_original(monkeypatch, tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({"version": "1", "name": "失败保护", "project_dir": str(tmp_path), "workflows": [{"id": "flow", "name": "原流程", "nodes": [{"id": "original", "type": "output", "data": {"description": "不可覆盖"}}]}]})
    store.save(project)
    original_etag = store.etag()
    manager = GeneratorManager(store)
    generation = Generation(id="failed", workflow_id="flow", base_etag=original_etag, draft=project.workflows[0].model_dump(mode="json"), prompt="优化", model="provider/model")

    monkeypatch.setenv("OPENAGENT_INCREMENTAL_MAX_ITERATIONS", "3")
    attempts = 0

    def fake_invoke(generation, spec, command, workdir, prompt):
        nonlocal attempts
        attempts += 1
        step = {"action": "add_node", "node": {"id": f"orphan-{attempts}", "type": "output", "data": {}}, "edges": []}
        return f"<result>{json.dumps(step)}</result>"

    manager._invoke = fake_invoke
    manager._run(generation, project)
    assert generation.events[-1]["event"] == "generation.failed"
    assert len([item for item in generation.events if item["event"] == "generation.layer_failed"]) == 3
    assert store.etag() == original_etag
    assert store.load().workflows[0].nodes[0].id == "original"


def test_generator_retries_same_layer_after_runtime_probe_failure(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({"version": "1", "name": "探测重试", "project_dir": str(tmp_path), "workflows": [{"id": "flow", "name": "原流程", "nodes": []}]})
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(id="probe-retry", workflow_id="flow", base_etag=store.etag(), draft=project.workflows[0].model_dump(mode="json"), prompt="创建输出流程", model="provider/model")
    steps = iter([
        {"action": "add_node", "node": {"id": "gate", "type": "approval", "data": {}}, "edges": []},
        {"action": "add_node", "node": {"id": "gate", "type": "approval", "data": {"instructions": "确认"}}, "edges": []},
        {"action": "add_node", "node": {"id": "done", "type": "output", "data": {"template": "{{latest}}"}}, "edges": [{"source": "gate", "target": "done"}]},
        {"action": "complete"},
    ])

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{"id": f"case-{i}", "name": "用例", "input": i, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["有效"], "approvals": {"gate": True}} for i in range(3)]
            return f"<result>{json.dumps({'cases': cases}, ensure_ascii=False)}</result>"
        if "增量工作流构建器" in prompt:
            return f"<result>{json.dumps(next(steps), ensure_ascii=False)}</result>"
        return '<result>{"passed":true,"score":90,"issues":[]}</result>'

    manager._invoke = fake_invoke
    probes = iter((["模拟的本层执行失败"], []))
    manager._probe_incremental_workflow = lambda *_args: next(probes)
    manager._run(generation, project)
    assert generation.events[-1]["event"] == "generation.completed", generation.events[-1]
    failures = [item for item in generation.events if item["event"] == "generation.layer_failed"]
    assert len(failures) == 1
    assert failures[0]["data"]["phase"] == "runtime_probe"
    assert len([item for item in generation.events if item["event"] == "generation.layer_completed"]) == 2


def test_generator_batch_delete_probes_once_then_repairs(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "批量删除探测", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "mid", "type": "prompt", "data": {"template": "处理中"}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [
            {"source": "start", "target": "mid"}, {"source": "mid", "target": "done"},
        ]}],
    })
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="batch-delete-repair", workflow_id="flow", base_etag=store.etag(),
        draft=project.workflows[0].model_dump(mode="json"), prompt="删除中间节点 mid", model="provider/model",
    )
    steps = iter([
        {"action": "delete_node", "node_ids": ["mid"], "summary": "删除中间节点"},
        {"action": "update_node", "node": {"id": "done", "type": "output", "data": {}}, "edges": [{"source": "mid", "target": "done"}, {"source": "start", "target": "done"}]},
        {"action": "complete"},
    ])
    planning_prompts = []

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{"id": f"case-{i}", "name": "用例", "input": i, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["有效"]} for i in range(3)]
            return f"<result>{json.dumps({'cases': cases}, ensure_ascii=False)}</result>"
        if "增量工作流构建器" in prompt:
            planning_prompts.append(prompt)
            return f"<result>{json.dumps(next(steps), ensure_ascii=False)}</result>"
        return '<result>{"passed":true,"score":90,"issues":[]}</result>'

    manager._invoke = fake_invoke
    probe_calls = []
    manager._probe_incremental_workflow = lambda *args: (probe_calls.append(args) or (["运行探测失败"] if len(probe_calls) == 1 else []))
    manager._run(generation, project)

    assert generation.events[-1]["event"] == "generation.completed", generation.events[-5:]
    assert len(probe_calls) == 1
    failures = [item for item in generation.events if item["event"] == "generation.layer_failed"]
    assert failures[0]["data"]["phase"] == "structural_runtime"
    assert any("未连通" in error for error in failures[0]["data"]["errors"])
    assert "运行探测失败" in failures[0]["data"]["errors"]
    assert "禁止返回 delete_node" in planning_prompts[1]
    saved = store.load().workflows[0]
    assert [(edge.source, edge.target) for edge in saved.edges] == [("start", "mid"), ("mid", "done"), ("start", "done")]



    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "原地修复", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "gate", "type": "approval", "data": {"instructions": "旧审批"}},
            {"id": "done", "type": "output", "data": {"template": "{{latest}}"}},
        ], "edges": [{"source": "gate", "target": "done"}]}],
    })
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="repair-without-delete", workflow_id="flow", base_etag=store.etag(),
        draft=project.workflows[0].model_dump(mode="json"), prompt="修复审批流程", model="provider/model",
    )
    steps = iter([
        {"action": "update_node", "node": {"id": "gate", "type": "approval", "data": {"instructions": "错误审批"}}},
        {"action": "delete_node", "node_id": "gate", "summary": "尝试规避失败"},
        {"action": "update_node", "node": {"id": "gate", "type": "approval", "data": {"instructions": "修复审批"}}},
        {"action": "complete"},
    ])
    planning_prompts = []

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{"id": f"case-{i}", "name": "用例", "input": i, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["有效"], "approvals": {"gate": True}} for i in range(3)]
            return f"<result>{json.dumps({'cases': cases}, ensure_ascii=False)}</result>"
        if "增量工作流构建器" in prompt:
            planning_prompts.append(prompt)
            return f"<result>{json.dumps(next(steps), ensure_ascii=False)}</result>"
        return '<result>{"passed":true,"score":90,"issues":[]}</result>'

    manager._invoke = fake_invoke
    probes = iter((["节点参数错误"], []))
    manager._probe_incremental_workflow = lambda *_args: next(probes)
    manager._run(generation, project)

    assert generation.events[-1]["event"] == "generation.completed", generation.events[-1]
    assert any(item["data"]["phase"] == "repair_policy" for item in generation.events if item["event"] == "generation.layer_failed")
    assert "禁止返回 delete_node" in planning_prompts[1]
    saved = store.load().workflows[0]
    assert [node.id for node in saved.nodes] == ["gate", "done"]
    assert saved.nodes[0].data["instructions"] == "修复审批"


def test_generator_stalls_after_two_same_failures_and_preserves_store(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "停滞保护", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "done", "type": "output", "data": {"template": "旧结果"}},
        ]}],
    })
    store.save(project)
    original_etag = store.etag()
    manager = GeneratorManager(store)
    generation = Generation(
        id="same-failure", workflow_id="flow", base_etag=original_etag,
        draft=project.workflows[0].model_dump(mode="json"),
        prompt="增加结果节点", model="provider/model",
    )
    attempts = 0

    def fake_invoke(*_args):
        nonlocal attempts
        attempts += 1
        step = {
            "action": "add_node",
            "node": {
                "id": "orphan", "type": "output",
                "data": {"description": f"措辞 {attempts}"},
                "position": {"x": attempts * 100, "y": attempts * 50},
            },
            "edges": [], "summary": f"摘要 {attempts}",
        }
        return f"<result>{json.dumps(step, ensure_ascii=False)}</result>"

    manager._invoke = fake_invoke
    manager._probe_incremental_workflow = lambda *_args: (_ for _ in ()).throw(
        AssertionError("静态失败不应调用 Harness 探测")
    )
    manager._run(generation, project)

    assert attempts == 2
    assert generation.events[-1]["event"] == "generation.stalled"
    stalled = generation.events[-1]["data"]
    assert stalled["reason"] == "no_progress"
    assert stalled["node_id"] == "orphan"
    assert stalled["attempts"] == 2
    assert stalled["last_failure"]["phase"] == "connectivity"
    assert stalled["last_failure"]["graph_fingerprint"]
    assert stalled["last_failure"]["candidate_fingerprint"]
    assert stalled["last_failure"]["proposal_fingerprint"]
    assert [node["id"] for node in stalled["workflow"]["nodes"]] == ["done"]
    assert store.etag() == original_etag
    assert store.load().workflows[0].nodes[0].data["template"] == "旧结果"


def test_generator_ignores_position_and_summary_only_repairs(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "语义指纹", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "done", "type": "output", "data": {"template": "{{latest}}"}},
        ]}],
    })
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="cosmetic-loop", workflow_id="flow", base_etag=store.etag(),
        draft=project.workflows[0].model_dump(mode="json"), prompt="调整输出", model="provider/model",
    )
    attempts = 0

    def fake_invoke(*_args):
        nonlocal attempts
        attempts += 1
        step = {
            "action": "update_node",
            "node": {
                "id": "done", "type": "output", "data": {"template": "{{latest}}"},
                "position": {"x": attempts * 20, "y": attempts * 30},
            },
            "summary": f"仅修改展示措辞 {attempts}",
        }
        return f"<result>{json.dumps(step, ensure_ascii=False)}</result>"

    manager._invoke = fake_invoke
    manager._run(generation, project)

    assert attempts == 2
    assert generation.events[-1]["event"] == "generation.stalled"
    assert generation.events[-1]["data"]["last_failure"]["phase"] == "progress_unchanged"
    assert not any(item["event"] == "generation.layer_completed" for item in generation.events)


def test_generator_stalls_immediately_when_delete_returns_to_accepted_graph(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "回环保护", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [{"source": "start", "target": "done"}]}],
    })
    store.save(project)
    original_etag = store.etag()
    manager = GeneratorManager(store)
    generation = Generation(
        id="graph-cycle", workflow_id="flow", base_etag=original_etag,
        draft=project.workflows[0].model_dump(mode="json"),
        prompt="先增加临时节点，再删除临时节点", model="provider/model",
    )
    steps = iter([
        {"action": "add_node", "node": {"id": "temp", "type": "prompt", "data": {"template": "临时"}}, "edges": [
            {"source": "start", "target": "temp"}, {"source": "temp", "target": "done"},
        ]},
        {"action": "delete_node", "node_ids": ["temp"], "summary": "删除临时节点"},
    ])
    calls = 0

    def fake_invoke(*_args):
        nonlocal calls
        calls += 1
        return f"<result>{json.dumps(next(steps), ensure_ascii=False)}</result>"

    manager._invoke = fake_invoke
    manager._probe_incremental_workflow = lambda *_args: (_ for _ in ()).throw(
        AssertionError("历史图回环应在 Harness 探测前暂停")
    )
    manager._run(generation, project)

    assert calls == 2
    assert generation.events[-1]["event"] == "generation.stalled"
    assert generation.events[-1]["data"]["last_failure"]["phase"] == "progress_cycle"
    assert [node["id"] for node in generation.events[-1]["data"]["workflow"]["nodes"]] == ["start", "done", "temp"]
    assert store.etag() == original_etag


def test_condition_semantics_and_node_type_aliases_are_strict():
    missing_expression = ProjectSpec.model_validate({
        "version": "1", "name": "条件", "workflows": [{"id": "flow", "name": "条件", "nodes": [
            {"id": "route", "type": "condition", "data": {}},
            {"id": "yes", "type": "output", "data": {}},
            {"id": "no", "type": "output", "data": {}},
        ], "edges": [
            {"source": "route", "target": "yes", "condition": "true"},
            {"source": "route", "target": "no", "condition": "false"},
        ]}],
    })
    assert "缺少 expression" in "；".join(validate_executable_workflow(
        missing_expression, missing_expression.workflows[0], runtime=True,
    ))

    chinese_branch = missing_expression.model_copy(deep=True)
    chinese_branch.workflows[0].nodes[0].data["expression"] = "input == true"
    chinese_branch.workflows[0].edges[0].condition = "通过"
    errors = "；".join(validate_executable_workflow(
        chinese_branch, chinese_branch.workflows[0], runtime=True,
    ))
    assert "必须使用 true 或 false" in errors
    assert "通过" in errors

    normalized = _normalize_workflow_result({
        "id": "flow", "name": "别名", "nodes": [
            {"id": "gate", "type": "human_approval", "data": {}},
        ], "edges": [],
    }, set())
    assert normalized["nodes"][0]["type"] == "approval"

    with pytest.raises(RuntimeError, match="合法类型") as exc_info:
        _normalize_workflow_result({
            "id": "flow", "name": "非法类型", "nodes": [
                {"id": "bad", "type": "command", "data": {}},
            ], "edges": [],
        }, set())
    assert "approval" in str(exc_info.value)
    assert "output" in str(exc_info.value)


def test_runtime_probe_tiering_covers_risky_nodes_and_condition_edges():
    previous = WorkflowSpec.model_validate({
        "id": "flow", "name": "分级", "nodes": [
            {"id": "done", "type": "output", "data": {"template": "旧"}},
        ], "edges": [],
    })
    static_candidate = WorkflowSpec.model_validate({
        "id": "flow", "name": "分级", "nodes": [
            {"id": "done", "type": "output", "data": {"template": "新"}},
        ], "edges": [],
    })
    assert _step_requires_runtime_probe(previous, static_candidate, "update_node", "done") is False

    risky_candidate = WorkflowSpec.model_validate({
        "id": "flow", "name": "分级", "nodes": [
            {"id": "gate", "type": "approval", "data": {}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [{"source": "gate", "target": "done"}],
    })
    assert _step_requires_runtime_probe(previous, risky_candidate, "add_node", "gate") is True

    condition_before = WorkflowSpec.model_validate({
        "id": "flow", "name": "条件边", "nodes": [
            {"id": "route", "type": "condition", "data": {"expression": "input == true"}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [{"source": "route", "target": "done", "condition": "true"}],
    })
    condition_after = condition_before.model_copy(deep=True)
    condition_after.edges[0].condition = "false"
    assert _step_requires_runtime_probe(condition_before, condition_after, "update_node", "done") is True


def test_failed_cases_are_retried_before_single_full_regression():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "验收", "nodes": [{"id": "done", "type": "output", "data": {}}],
    })
    project = ProjectSpec.model_validate({"version": "1", "name": "验收", "workflows": [workflow.model_dump(mode="json")]})
    metrics = (0, 0, 0, 0, 0, 0.0, 0)
    calls = []

    class FocusedEvaluator:
        def evaluate(self, _project, _workflow, index, *, case_ids=None):
            calls.append(None if case_ids is None else set(case_ids))
            cases = [CaseResult("failed-case", True)] if case_ids is not None else []
            return CandidateResult(index, workflow, True, cases, metrics)

    generation = Generation(
        id="focused", workflow_id="flow", base_etag="etag",
        draft=workflow.model_dump(mode="json"), prompt="修复", model="provider/model",
    )
    result, failed_ids = GeneratorManager._evaluate_failed_cases_first(
        generation, FocusedEvaluator(), project, workflow, 2, {"failed-case"},
    )
    assert result.passed is True
    assert failed_ids == set()
    assert calls == [{"failed-case"}, None]
    stages = [item["data"]["stage"] for item in generation.events if item["event"] == "generation.stage"]
    assert stages == ["retrying_failed_cases", "final_regression"]


def test_resume_uses_stalled_draft_and_saves_once(monkeypatch, tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "续跑", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": []}],
    })
    store.save(project)
    base_etag = store.etag()
    stalled_draft = WorkflowSpec.model_validate({
        "id": "flow", "name": "流程", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
        ], "edges": [],
    })
    previous = Generation(
        id="stalled", workflow_id="flow", base_etag=base_etag,
        draft=stalled_draft.model_dump(mode="json"), prompt="创建输入输出流程",
        model="provider/model", completed=True, stalled=True,
        last_failure={"phase": "full_static_graph", "action": "complete", "attempts": 2},
    )
    manager = GeneratorManager(store)
    manager.generations[previous.id] = previous
    manager.active["flow"] = previous.id
    steps = iter([
        {"action": "add_node", "node": {"id": "done", "type": "output", "data": {"template": "{{latest}}"}}, "edges": [{"source": "start", "target": "done"}]},
        {"action": "complete"},
    ])

    def fake_invoke(_generation, _spec, _command, _workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{
                "id": f"case-{index}", "name": "用例", "input": index,
                "assertions": [{"path": "output", "operator": "exists"}],
                "semantic_criteria": ["有效"],
            } for index in range(3)]
            return f"<result>{json.dumps({'cases': cases}, ensure_ascii=False)}</result>"
        if "增量工作流构建器" in prompt:
            return f"<result>{json.dumps(next(steps), ensure_ascii=False)}</result>"
        return '<result>{"passed":true,"score":90,"issues":[]}</result>'

    manager._invoke = fake_invoke
    manager.ensure_ready = lambda _spec=None: {"model": "provider/model"}
    monkeypatch.setattr("openagent_studio.generator.resolve_executable", lambda *_args: "opencode")
    manager._launch = lambda generation, spec: manager._run(generation, spec)
    save_calls = []
    real_save = store.save

    def counted_save(spec, expected=None):
        save_calls.append(expected)
        return real_save(spec, expected)

    monkeypatch.setattr(store, "save", counted_save)
    resumed = manager.resume(previous.id, "继续并补齐输出节点")

    assert resumed.events[-1]["event"] == "generation.completed", resumed.events[-5:]
    assert resumed.initial_failures == [previous.last_failure]
    assert "继续并补齐输出节点" in resumed.prompt
    assert save_calls == [base_etag]
    assert [node.id for node in store.load().workflows[0].nodes] == ["start", "done"]
    with pytest.raises(RuntimeError, match="暂停期间已被修改"):
        manager.resume(previous.id, "再次继续")


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
    client = TestClient(create_app(tmp_path / "project.yaml"))
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
    client = TestClient(create_app(tmp_path / "project.yaml"))
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
    client = TestClient(create_app(tmp_path / "project.yaml"))
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


# ---- 创建 / 修改 / 删除操作边界回归（意图路由 + 专用执行器）----


def test_route_isolation_create_entry_and_existing_message_route(tmp_path: Path):
    app = create_app(tmp_path / "project.yaml")
    client = TestClient(app)
    # FastAPI 0.141+ 的 include_router 会在 app.routes 里放入 _IncludedRouter 包装对象，
    # 它没有 .path 属性；只收集实际注册的路由。
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/generator/workflows" in paths
    assert "/api/generator/workflows/{workflow_id}/messages" in paths
    assert "/api/generator/generations/{generation_id}/resume" in paths
    # 创建入口：非法编号与空需求在调用模型前被拒绝（确定性 422）
    invalid = client.post("/api/generator/workflows", json={"message": "创建入口流程", "workflow_id": "Bad_Id"})
    assert invalid.status_code == 422
    empty = client.post("/api/generator/workflows", json={"message": "", "workflow_id": "entry"})
    assert empty.status_code == 422
    # 已有 workflow 的消息入口仍然注册：空消息在 start() 中被拒绝为 422
    store = SpecStore(tmp_path / "project.yaml")
    store.save(ProjectSpec.model_validate({
        "version": "1", "name": "路由", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [{"id": "done", "type": "output", "data": {}}]}],
    }))
    existing = client.post("/api/generator/workflows/flow/messages", json={"message": ""})
    assert existing.status_code == 422
    # 同名工作流已存在时创建入口拒绝 422，而不是进入修改路径
    duplicate = client.post("/api/generator/workflows", json={"message": "重复创建", "workflow_id": "flow"})
    assert duplicate.status_code == 422
    assert "已存在" in duplicate.json()["detail"]


def test_resume_endpoint_rejects_empty_message_and_etag_conflict(monkeypatch, tmp_path: Path):
    app = create_app(tmp_path / "project.yaml")
    store = app.state.store
    project = ProjectSpec.model_validate({
        "version": "1", "name": "恢复", "workflows": [{"id": "flow", "name": "流程", "nodes": []}],
    })
    store.save(project)
    manager = app.state.generator_manager
    stalled = Generation(
        id="paused-api", workflow_id="flow", base_etag=store.etag(),
        draft=project.workflows[0].model_dump(mode="json"), prompt="修复流程", model="provider/model",
        completed=True, stalled=True, last_failure={"phase": "runtime_probe", "node_id": "gate"},
    )
    manager.generations[stalled.id] = stalled
    manager.active["flow"] = stalled.id
    manager.ensure_ready = lambda _spec=None: {"model": "provider/model"}
    manager._launch = lambda *_args: None
    client = TestClient(app)

    assert client.post(f"/api/generator/generations/{stalled.id}/resume", json={"message": ""}).status_code == 422
    resumed = client.post(f"/api/generator/generations/{stalled.id}/resume", json={"message": "补充修复要求"})
    assert resumed.status_code == 202
    assert resumed.json()["workflow_id"] == "flow"

    changed = store.load().model_copy(deep=True)
    changed.name = "外部修改"
    store.save(changed, store.etag())
    conflict = client.post(f"/api/generator/generations/{stalled.id}/resume", json={"message": "再次继续"})
    assert conflict.status_code == 409
    assert "暂停期间已被修改" in conflict.json()["detail"]


def test_generator_creation_uses_tiered_layer_validation_and_appends_once(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({"version": "1", "name": "创建", "project_dir": str(tmp_path)})
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="create-flow", workflow_id="new-flow", base_etag=store.etag(),
        draft=WorkflowSpec(id="new-flow", name="new-flow").model_dump(mode="json"),
        prompt="创建一个先接收输入再输出结果的流程", model="provider/model", mode="create",
    )
    steps = iter([
        {"action": "add_node", "node": {"id": "start", "type": "manual_trigger", "data": {"description": "接收输入"}}, "add_edges": [], "summary": "创建入口"},
        {"action": "add_node", "node": {"id": "gate", "type": "approval", "data": {"instructions": "确认继续"}}, "add_edges": [{"source": "start", "target": "gate"}], "summary": "创建审批"},
        {"action": "add_node", "node": {"id": "done", "type": "output", "data": {"description": "输出结果"}}, "add_edges": [{"source": "gate", "target": "done"}], "summary": "创建出口"},
        {"action": "finish_creation", "summary": "入口与出口已连通"},
    ])
    planning_prompts = []

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{"id": f"case-{i}", "name": "用例", "input": i, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["有效"], "approvals": {"gate": True}} for i in range(3)]
            return f"<result>{json.dumps({'cases': cases}, ensure_ascii=False)}</result>"
        if "工作流创建器" in prompt:
            planning_prompts.append(prompt)
            return f"<result>{json.dumps(next(steps), ensure_ascii=False)}</result>"
        return '<result>{"passed":true,"score":90,"issues":[]}</result>'

    manager._invoke = fake_invoke
    probe_calls = []
    manager._probe_incremental_workflow = lambda *args: (probe_calls.append(args) or [])
    manager._run(generation, project)

    assert generation.events[-1]["event"] == "generation.completed", generation.events[-5:]
    assert len(probe_calls) == 1
    assert probe_calls[0][3] == "gate"
    assert len(planning_prompts) == 4
    layers = [item for item in generation.events if item["event"] == "generation.layer_completed"]
    assert len(layers) == 3
    assert [item["data"]["validation_tier"] for item in layers] == ["static_only", "runtime_probe", "static_only"]
    stages = [item["data"]["stage"] for item in generation.events if item["event"] == "generation.stage"]
    assert "validating_complete_graph" in stages
    assert "full_evaluating" in stages
    # 创建结果以 append 方式一次保存，且摘要准确描述结构检查与最终验收
    saved = store.load().workflows
    assert len(saved) == 1
    assert saved[0].id == "new-flow"
    assert [node.id for node in saved[0].nodes] == ["start", "gate", "done"]
    assert [(edge.source, edge.target) for edge in saved[0].edges] == [("start", "gate"), ("gate", "done")]
    assert len(saved[0].evaluation.cases) == 3
    completed = generation.events[-1]["data"]
    assert "逐节点创建" in completed["assistant_message"]


def test_generator_creation_static_failure_never_saves_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 完整静态校验持续失败时循环必须有界，否则会被误认为“无法修复”而无限重试
    monkeypatch.setenv("OPENAGENT_INCREMENTAL_MAX_ITERATIONS", "5")
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({"version": "1", "name": "创建", "project_dir": str(tmp_path)})
    store.save(project)
    manager = GeneratorManager(store)
    generation = Generation(
        id="create-fail", workflow_id="broken-flow", base_etag=store.etag(),
        draft=WorkflowSpec(id="broken-flow", name="broken-flow").model_dump(mode="json"),
        prompt="创建一个无法闭合的流程", model="provider/model", mode="create",
    )
    creation_calls = []

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            raise AssertionError("完整静态校验失败前不应生成验收用例")
        if "工作流创建器" in prompt:
            creation_calls.append(prompt)
            if len(creation_calls) == 1:
                return '<result>{"action":"add_node","node":{"id":"start","type":"manual_trigger","data":{}},"add_edges":[],"summary":"入口"}</result>'
            return '<result>{"action":"finish_creation","summary":"只有入口"}</result>'
        raise AssertionError(prompt)

    manager._invoke = fake_invoke
    manager._run(generation, project)

    assert generation.events[-1]["event"] == "generation.stalled", generation.events[-5:]
    stages = [item["data"]["stage"] for item in generation.events if item["event"] == "generation.stage"]
    assert "validating_complete_graph" in stages
    assert "full_evaluating" not in stages  # 静态失败时绝不进入完整运行验收
    assert not any(item.id == "broken-flow" for item in store.load().workflows)
    assert store.load().workflows == []


def test_incremental_update_preserves_undeclared_edges():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "边保留", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "mid", "type": "prompt", "data": {"template": "旧"}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [
            {"source": "start", "target": "mid"},
            {"source": "mid", "target": "done"},
        ],
    })
    # 未声明 edges 时入射/出射边必须原样保留
    candidate, action, touched, *_ = _apply_incremental_step(
        workflow, {"action": "update_node", "node": {"id": "mid", "type": "prompt", "data": {"template": "新"}}}, set(),
    )
    assert action == "update_node"
    assert touched == "mid"
    assert candidate.nodes[1].data["template"] == "新"
    assert {(edge.source, edge.target) for edge in candidate.edges} == {("start", "mid"), ("mid", "done")}
    # 显式声明 edges 时才按声明替换该节点的 incident 边
    patched, *_ = _apply_incremental_step(
        workflow, {"action": "update_node", "node": {"id": "done", "type": "output", "data": {}}, "edges": [{"source": "start", "target": "done"}]}, set(),
    )
    # done 的入射边 (mid→done) 被声明替换为 (start→done)，与 done 无关的 (start→mid) 原样保留
    assert {(edge.source, edge.target) for edge in patched.edges} == {("start", "mid"), ("start", "done")}


def test_creation_step_revise_edges_mode_and_add_connection_rule():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "创建", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [{"source": "start", "target": "done"}],
    })
    # revise_node 默认 preserve：边原样保留，只替换节点
    revised, action, touched, _ = _apply_creation_step(
        workflow, {"action": "revise_node", "node": {"id": "done", "type": "output", "data": {"description": "更新"}}, "summary": "补说明"}, set(),
    )
    assert action == "revise_node" and touched == "done"
    assert revised.nodes[1].data["description"] == "更新"
    assert [(edge.source, edge.target) for edge in revised.edges] == [("start", "done")]
    # preserve 时提交边修改被拒绝
    with pytest.raises(RuntimeError):
        _apply_creation_step(workflow, {"action": "revise_node", "node": {"id": "done", "type": "output", "data": {}}, "add_edges": [{"source": "start", "target": "done"}]}, set())
    # patch 只应用显式声明的 remove/add 边
    patched, *_ = _apply_creation_step(
        workflow, {"action": "revise_node", "node": {"id": "done", "type": "output", "data": {}},
        "edges_mode": "patch", "remove_edges": [{"source": "start", "target": "done"}], "add_edges": [], "summary": "断开"}, set(),
    )
    assert patched.edges == []
    # 删除不存在的边被拒绝
    with pytest.raises(RuntimeError):
        _apply_creation_step(workflow, {"action": "revise_node", "node": {"id": "done", "type": "output", "data": {}},
            "edges_mode": "patch", "remove_edges": [{"source": "start", "target": "missing"}]}, set())
    # 第一个节点可以独立存在，不要求接入已有图
    solo, action, touched, _ = _apply_creation_step(
        WorkflowSpec(id="flow", name="创建"),
        {"action": "add_node", "node": {"id": "start", "type": "manual_trigger", "data": {}}, "add_edges": [], "summary": "入口"}, set(),
    )
    assert [node.id for node in solo.nodes] == ["start"]
    assert _creation_step_errors(WorkflowSpec(id="flow", name="创建"), solo, action, touched) == []
    # 已有节点时新增节点必须接入草稿
    orphan, *_ = _apply_creation_step(
        solo, {"action": "add_node", "node": {"id": "done", "type": "output", "data": {}}, "add_edges": [], "summary": "未接入"}, set(),
    )
    errors = _creation_step_errors(solo, orphan, "add_node", "done")
    assert any("没有接入当前草稿" in error for error in errors)


def test_delete_authorization_negation_and_scope():
    assert _explicit_delete_request("删除节点 mid")
    assert _explicit_delete_request("请删除 workflow report-flow")
    assert _explicit_delete_request("把节点 old-step 移除掉")
    assert not _explicit_delete_request("不要删除节点 mid")
    assert not _explicit_delete_request("别删除工作流 report-flow")
    assert not _explicit_delete_request("不能删除任何节点")
    assert not _explicit_delete_request("请优化流程，不要动节点")


def test_delete_workflow_route_lifecycle(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "生命周期", "project_dir": str(tmp_path),
        "workflows": [
            {"id": "flow", "name": "流程", "nodes": [{"id": "done", "type": "output", "data": {}}]},
            {"id": "keep", "name": "保留", "nodes": [{"id": "done", "type": "output", "data": {}}]},
        ],
    })
    store.save(project)
    client = TestClient(create_app(tmp_path / "project.yaml"))
    response = client.delete("/api/workflows/flow", headers={"If-Match": store.etag()})
    assert response.status_code == 200
    assert response.json()["workflow_id"] == "flow"
    assert [item.id for item in store.load().workflows] == ["keep"]


def test_delete_workflow_route_404_and_stale_etag(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "生命周期", "project_dir": str(tmp_path),
        "workflows": [{"id": "flow", "name": "流程", "nodes": [{"id": "done", "type": "output", "data": {}}]}],
    })
    store.save(project)
    client = TestClient(create_app(tmp_path / "project.yaml"))
    missing = client.delete("/api/workflows/nope")
    assert missing.status_code == 404
    stale = client.delete("/api/workflows/flow", headers={"If-Match": "stale-etag"})
    assert stale.status_code == 409
    assert [item.id for item in store.load().workflows] == ["flow"]  # 并发冲突不覆盖、不删除
    assert [item["id"] for item in client.get("/api/workflows").json()] == ["flow"]


def test_delete_workflow_route_blockers(tmp_path: Path):
    env_file = tmp_path / "platform.env"
    env_file.write_text("FEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\nFEISHU_VERIFICATION_TOKEN=verify\n", encoding="utf-8")
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "生命周期", "project_dir": str(tmp_path),
        "workflows": [
            {"id": "bot-flow", "name": "机器人", "nodes": [{"id": "done", "type": "output", "data": {}}]},
            {"id": "referenced", "name": "被引用", "nodes": [{"id": "done", "type": "output", "data": {}}]},
            {"id": "parent", "name": "父流程", "nodes": [
                {"id": "sub", "type": "subworkflow", "data": {"workflow_id": "referenced"}},
                {"id": "done", "type": "output", "data": {}},
            ], "edges": [{"source": "sub", "target": "done"}]},
        ],
        "integrations": {"feishu": [{"id": "main", "workflow_id": "bot-flow", "env_file": str(env_file), "auto_reply": False}]},
    })
    store.save(project)
    client = TestClient(create_app(tmp_path / "project.yaml"))
    etag = store.etag()
    blocked = client.delete("/api/workflows/bot-flow", headers={"If-Match": etag})
    assert blocked.status_code == 409
    assert "飞书集成" in blocked.json()["detail"]
    blocked_sub = client.delete("/api/workflows/referenced", headers={"If-Match": etag})
    assert blocked_sub.status_code == 409
    assert "子工作流" in blocked_sub.json()["detail"]
    # blocker 失败后磁盘状态完全不变
    assert [item.id for item in store.load().workflows] == ["bot-flow", "referenced", "parent"]
