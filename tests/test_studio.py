from pathlib import Path
from fastapi.testclient import TestClient
from openagent_studio.app import create_app
from openagent_studio.generator import Generation, GeneratorManager, SYSTEM_PROMPT, _CompactionTimeoutError, _EmptyCompactionError, _command_exceeds_limit, _compact_prompt_length, _compaction_timeout_seconds, _invoke_timeout_seconds, _is_command_line_too_long, _normalize_evaluation_result, _normalize_workflow_result, _parse_result, _with_file_prompt
from openagent_studio.store import SpecStore
from openagent_studio.models import EvaluationAssertion, ProjectSpec, QQIntegrationSpec, WorkflowEvaluation, WorkflowSpec
from openagent_studio.workflow_runner import EvaluationPolicy, WorkflowManager
from openagent_studio.evaluation import CandidateResult, HarnessInfrastructureError, SemanticVerdict, WorkflowEvaluator, check_assertion, complexity_metrics
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
        return '<result>{"action":"reply","answer":"输出节点负责定义工作流最终返回值。"}</result>'

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
    assert run.outputs["output"]["text"] == "done"


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
    assert run.outputs["result"]["text"] == "real harness result"
    assert run.id not in manager.runs


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
    assert all("一次只处理一个节点" in prompt for prompt in planning_prompts)
    assert len([item for item in generation.events if item["event"] == "generation.layer_completed"]) == 2
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
        {"action": "add_node", "node": {"id": "done", "type": "output", "data": {}}, "edges": []},
        {"action": "add_node", "node": {"id": "done", "type": "output", "data": {"template": "{{latest}}"}}, "edges": []},
        {"action": "complete"},
    ])

    def fake_invoke(generation, spec, command, workdir, prompt):
        if "验收设计师" in prompt:
            cases = [{"id": f"case-{i}", "name": "用例", "input": i, "assertions": [{"path": "output", "operator": "exists"}], "semantic_criteria": ["有效"]} for i in range(3)]
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
    assert len([item for item in generation.events if item["event"] == "generation.layer_completed"]) == 1


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
