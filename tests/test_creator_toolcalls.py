"""工具调用模式（_build_toolcalls）测试 — Creator Harness Phase 2"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from openagent_studio.generator import (
    Generation,
    GeneratorManager,
    _coerce_action_list,
    _modify_build_mode,
)
from openagent_studio.models import ProjectSpec, WorkflowEvaluation, WorkflowSpec
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


def test_modify_build_mode_defaults_to_toolcalls(monkeypatch):
    monkeypatch.delenv("OPENAGENT_GENERATOR_MODE", raising=False)
    assert _modify_build_mode() == "toolcalls"
    monkeypatch.setenv("OPENAGENT_GENERATOR_MODE", "incremental")
    assert _modify_build_mode() == "incremental"
    monkeypatch.setenv("OPENAGENT_GENERATOR_MODE", "chain")
    assert _modify_build_mode() == "chain"
    monkeypatch.setenv("OPENAGENT_GENERATOR_MODE", "bogus")
    assert _modify_build_mode() == "toolcalls"


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
