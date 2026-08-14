"""LLM Agent Loop/DAG 构建测试；保留旧 _build_toolcalls 兼容入口。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openagent_studio.generator import (
    Generation,
    GeneratorManager,
    _GenerationAwaitingInput,
    _coerce_action_list,
    _create_build_mode,
    _dag_errors,
    _modify_build_mode,
)
from openagent_studio.models import ProjectSpec, WorkflowEdge, WorkflowEvaluation, WorkflowSpec
from openagent_studio.store import SpecStore


def _original() -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "flow", "name": "f", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
        ], "edges": [],
    })


def _make_generation(store: SpecStore, build_mode: str = "toolcalls") -> Generation:
    return Generation(
        id="g1", workflow_id="flow", base_etag=store.etag(),
        draft=_original().model_dump(mode="json"), prompt="创建一个流程",
        harness_agent_ids=set(), model="test-model", mode="modify",
        build_mode=build_mode,
    )


def _make_spec() -> ProjectSpec:
    return ProjectSpec.model_validate({"version": "1", "name": "t", "workflows": [], "harness": []})


def _fixture(tmp_path: Path, build_mode: str = "toolcalls"):
    store = SpecStore(tmp_path / "project.yaml")
    manager = GeneratorManager(store)
    gen = _make_generation(store, build_mode)
    spec = _make_spec()
    original = _original()
    evaluator = MagicMock()
    return manager, gen, spec, original, evaluator


def test_coerce_action_list_shapes():
    single = {"action": "complete"}
    assert _coerce_action_list(single) == [single]
    lst = [{"action": "add_node"}, {"action": "complete"}]
    assert _coerce_action_list(lst) == lst
    wrapped = {"operations": lst}
    assert _coerce_action_list(wrapped) == lst
    import pytest
    with pytest.raises(RuntimeError):
        _coerce_action_list("not a dict")


def test_build_mode_defaults_to_agent_loop(monkeypatch):
    monkeypatch.delenv("OPENAGENT_GENERATOR_MODE", raising=False)
    assert _modify_build_mode() == "agent_loop"
    assert _create_build_mode() == "agent_loop"
    monkeypatch.setenv("OPENAGENT_GENERATOR_MODE", "incremental")
    assert _modify_build_mode() == "incremental"
    monkeypatch.setenv("OPENAGENT_GENERATOR_MODE", "chain")
    assert _modify_build_mode() == "chain"
    monkeypatch.setenv("OPENAGENT_GENERATOR_MODE", "bogus")
    assert _modify_build_mode() == "agent_loop"


def test_dag_rejects_back_edges_but_accepts_forward_edges():
    workflow = WorkflowSpec.model_validate({
        "id": "flow", "name": "f",
        "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "work", "type": "prompt", "data": {"template": "{{latest}}"}},
            {"id": "done", "type": "output", "data": {"template": "{{latest}}"}},
        ],
        "edges": [
            {"source": "start", "target": "work"},
            {"source": "work", "target": "done"},
        ],
    })
    assert _dag_errors(workflow) == []
    workflow.edges.append(WorkflowEdge(source="done", target="work"))
    assert any("图循环" in error for error in _dag_errors(workflow))


def test_agent_loop_never_accepts_cycle_after_delete(monkeypatch, tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    manager = GeneratorManager(store)
    original = WorkflowSpec.model_validate({
        "id": "flow", "name": "坏图",
        "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "a", "type": "prompt", "data": {"template": "{{latest}}"}},
            {"id": "b", "type": "prompt", "data": {"template": "{{latest}}"}},
            {"id": "c", "type": "output", "data": {"template": "{{latest}}"}},
        ],
        "edges": [
            {"source": "start", "target": "a"},
            {"source": "a", "target": "b"},
            {"source": "b", "target": "a"},
            {"source": "b", "target": "c"},
        ],
    })
    generation = Generation(
        id="cycle-delete", workflow_id="flow", base_etag=store.etag(),
        draft=original.model_dump(mode="json"), prompt="删除节点 c",
        harness_agent_ids=set(), model="test-model", build_mode="agent_loop",
    )
    spec = _make_spec()
    evaluator = MagicMock()
    manager._invoke_result = MagicMock(return_value=[{
        "action": "delete_node", "node_ids": ["c"], "summary": "删除出口",
    }])
    manager._probe_incremental_workflow = MagicMock(return_value=[])
    monkeypatch.setenv("OPENAGENT_AGENT_LOOP_MAX_ITERATIONS", "1")

    with pytest.raises(RuntimeError, match="最大迭代次数"):
        manager._build_agent_loop(generation, spec, ["opencode"], tmp_path, original, evaluator, "[]")

    failures = [event for event in generation.events if event["event"] == "generation.layer_failed"]
    assert failures and failures[0]["data"]["phase"] == "dag"
    assert any("图循环" in error for error in failures[0]["data"]["errors"])
    assert not any(event["event"] == "generation.layer_completed" for event in generation.events)


def test_build_toolcalls_applies_multiple_actions_then_completes(tmp_path: Path):
    manager, gen, spec, original, evaluator = _fixture(tmp_path)
    manager._invoke_result = MagicMock(return_value=[
        {"action": "add_node", "node": {"id": "done", "type": "output", "data": {"template": "{{latest}}"}},
         "edges": [{"source": "start", "target": "done"}], "summary": "建出口"},
        {"action": "complete", "summary": "入口到输出已连通"},
    ])
    manager._generate_evaluation = MagicMock(return_value=WorkflowEvaluation(cases=[]))
    manager._evaluate_failed_cases_first = MagicMock(return_value=(SimpleNamespace(passed=True), set()))

    result = manager._build_toolcalls(gen, spec, ["opencode"], tmp_path, original, evaluator, "[]")

    assert [n.id for n in result.nodes] == ["start", "done"]
    assert [(e.source, e.target) for e in result.edges] == [("start", "done")]
    # 一次响应完成，只调用了一次 _invoke_result
    assert manager._invoke_result.call_count == 1


def test_agent_loop_requires_evaluate_before_finalize(tmp_path: Path):
    manager, gen, spec, original, evaluator = _fixture(tmp_path, build_mode="agent_loop")
    manager._invoke_result = MagicMock(side_effect=[
        [{"action": "finalize", "summary": "提前结束"}],
        [{"action": "add_node", "node": {"id": "done", "type": "output", "data": {"template": "{{latest}}"}},
          "edges": [{"source": "start", "target": "done"}], "summary": "补出口"},
         {"action": "evaluate", "summary": "请求真实验收"},
         {"action": "finalize", "summary": "验收通过后结束"}],
    ])
    manager._generate_evaluation = MagicMock(return_value=WorkflowEvaluation(cases=[]))
    manager._evaluate_failed_cases_first = MagicMock(return_value=(SimpleNamespace(passed=True), set()))

    result = manager._build_toolcalls(gen, spec, ["opencode"], tmp_path, original, evaluator, "[]")

    assert [node.id for node in result.nodes] == ["start", "done"]
    assert manager._invoke_result.call_count == 2
    assert any(item["event"] == "generation.agent_tool" and item["data"]["action"] == "finalize"
               for item in gen.events)


def test_agent_loop_can_ask_user_without_timeout_stall(tmp_path: Path):
    import pytest

    manager, gen, spec, original, evaluator = _fixture(tmp_path, build_mode="agent_loop")
    manager._invoke_result = MagicMock(return_value=[{
        "action": "ask_user",
        "question": "审批拒绝时是否仍然输出评审上下文？",
        "options": ["是", "否"],
    }])

    with pytest.raises(_GenerationAwaitingInput):
        manager._build_toolcalls(gen, spec, ["opencode"], tmp_path, original, evaluator, "[]")

    assert gen.completed is True
    assert gen.awaiting_input is True
    assert gen.stalled is False
    assert gen.events[-1]["event"] == "generation.question"


def test_resume_accepts_agent_question_and_keeps_draft(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    project = ProjectSpec.model_validate({
        "version": "1", "name": "t",
        "workflows": [{"id": "flow", "name": "f", "nodes": [], "edges": []}],
        "harness": [],
    })
    store.save(project)
    manager = GeneratorManager(store)
    previous = Generation(
        id="question", workflow_id="flow", base_etag=store.etag(),
        draft={"id": "flow", "name": "f", "nodes": [], "edges": []},
        prompt="创建审核流程", model="test-model", completed=True, build_mode="agent_loop",
        awaiting_input=True,
        last_failure={"phase": "awaiting_input", "question": "是否需要审批？"},
    )
    manager.generations[previous.id] = previous
    manager.active[previous.workflow_id] = previous.id
    manager.ensure_ready = lambda _spec=None: {"model": "test-model"}
    manager._launch = lambda *_args: None

    resumed = manager.resume(previous.id, "需要审批")

    assert resumed.awaiting_input is False
    assert resumed.build_mode == "agent_loop"
    assert "用户回答 Agent 问题：需要审批" in resumed.prompt


def test_build_toolcalls_recovers_from_partial_failure(tmp_path: Path):
    manager, gen, spec, original, evaluator = _fixture(tmp_path)
    # 第一轮：第一个操作合法，第二个操作 action 非法 → 部分失败，回填重试
    # 第二轮：全部合法 + complete
    manager._invoke_result = MagicMock(side_effect=[
        [
            {"action": "add_node", "node": {"id": "a", "type": "prompt", "data": {"template": "a"}},
             "edges": [{"source": "start", "target": "a"}], "summary": "加a"},
            {"action": "bogus_action", "summary": "非法"},
        ],
        [
            {"action": "add_node", "node": {"id": "done", "type": "output", "data": {"template": "{{latest}}"}},
             "edges": [{"source": "a", "target": "done"}], "summary": "加出口"},
            {"action": "complete", "summary": "连通"},
        ],
    ])
    manager._generate_evaluation = MagicMock(return_value=WorkflowEvaluation(cases=[]))
    manager._evaluate_failed_cases_first = MagicMock(return_value=(SimpleNamespace(passed=True), set()))

    result = manager._build_toolcalls(gen, spec, ["opencode"], tmp_path, original, evaluator, "[]")

    # 第一轮 a 已接受，bogus 失败；第二轮补 done + complete
    assert [n.id for n in result.nodes] == ["start", "a", "done"]
    assert manager._invoke_result.call_count == 2
    # 记录了部分失败
    assert any(e["event"] == "generation.layer_failed" for e in gen.events)
