"""CreatorHarness 集成测试 — 双 Harness 架构核心"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import time
import threading

import pytest

from openagent_studio.creator import CreatorHarness, AgentCapability, IntentType, NodeTypeInfo
from openagent_studio.creator.generator import WorkflowGenerator


@dataclasses.dataclass
class FakeGeneration:
    """模拟 GeneratorManager 返回的 generation 对象，支持 emit。"""
    id: str
    workflow_id: str
    events: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    next_event_sequence: int = 0
    event_signal: threading.Condition = dataclasses.field(default_factory=threading.Condition)
    completed: bool = False
    cancelled: bool = False
    stalled: bool = False
    process: Any = None
    mode: str = "modify"
    messages: list[dict[str, str]] = dataclasses.field(default_factory=list)
    last_failure: dict[str, Any] = None

    def emit(self, event: str, data: dict[str, Any]) -> None:
        with self.event_signal:
            item = {"event": event, "data": data, "sequence": self.next_event_sequence, "timestamp": time.time()}
            self.next_event_sequence += 1
            self.events.append(item)
            self.event_signal.notify_all()


class FakeStore:
    """模拟 SpecStore — 提供 raw() 用于 registry 加载。"""

    def __init__(self, harness_agents: list[dict] | None = None):
        self._harness_agents = harness_agents or [
            {
                "id": "coding",
                "name": "Coding Agent",
                "description": "代码开发智能体",
                "backend_id": "default",
                "labels": {"agent-harness/capability": "text-generation", "agent-harness/sandbox": "read-write"},
            },
            {
                "id": "knowledge",
                "name": "Knowledge Agent",
                "description": "知识检索智能体",
                "backend_id": "default",
                "labels": {"agent-harness/capability": "repository-analysis", "agent-harness/sandbox": "read-only"},
            },
        ]

    def raw(self) -> dict[str, Any]:
        return {"version": "1", "name": "Test", "harness": self._harness_agents}

    def load(self):
        return SimpleNamespace(
            workflows=[
                SimpleNamespace(id="flow-1", name="测试流程", nodes=[], _data={}),
            ]
        )


class FakeGeneratorManager:
    """模拟 GeneratorManager。"""

    def __init__(self):
        self.history: dict[str, list] = {}

    def create(self, message, workflow_id="", name=None):
        return FakeGeneration(id="gen-1", workflow_id="flow-new")

    def create_direct(self, message, workflow_id="", name=None):
        return FakeGeneration(id="gen-1", workflow_id="flow-new")

    def start(self, workflow_id, message):
        return FakeGeneration(id="gen-2", workflow_id=workflow_id)

    def optimize(self, workflow_id):
        return FakeGeneration(id="gen-3", workflow_id=workflow_id)

    def resume(self, generation_id, message):
        return FakeGeneration(id=generation_id, workflow_id="flow-1")

    def require(self, generation_id):
        raise KeyError(generation_id)

    def cancel(self, generation_id):
        raise KeyError(generation_id)


def make_harness(auto_sync: bool = True) -> CreatorHarness:
    store = FakeStore()
    gm = FakeGeneratorManager()
    return CreatorHarness(
        store=store,
        general_harness_url="http://127.0.0.1:1",  # 不可达，降级本地
        auto_sync=auto_sync,
        generator=gm,
    )


def test_harness_status():
    harness = make_harness()
    status = harness.status()
    assert status["ready"] is True
    assert status["agents_count"] == 2
    assert status["node_types_count"] >= 20
    assert status["workflow_generator_ready"] is True


def test_harness_node_types_match_frontend():
    """节点类型目录必须与前端预期的 23 个类型一致。"""
    harness = make_harness()
    node_types = harness.get_node_types()
    types = {nt.type for nt in node_types}
    expected = {
        "manual_trigger", "webhook", "schedule", "llm", "agent",
        "knowledge_retrieval", "tool", "code", "prompt", "variable_set",
        "transform", "merge", "http_request", "condition", "switch",
        "parallel", "iteration", "loop", "delay", "approval",
        "validator", "subworkflow", "output",
    }
    assert types == expected, f"节点类型不匹配: {expected - types} / {types - expected}"


def test_harness_agent_requiring_node_types():
    """6 种节点类型需要 agent_id。"""
    harness = make_harness()
    node_types = harness.get_node_types()
    agent_types = {nt.type for nt in node_types if nt.requires_agent}
    assert agent_types == {"llm", "agent", "knowledge_retrieval", "tool", "code", "validator"}


def test_harness_node_type_defaults():
    harness = make_harness()
    node_types = harness.get_node_types()
    for nt in node_types:
        if nt.type == "webhook":
            assert "path" in nt.default_data
        if nt.type == "llm":
            assert "prompt" in nt.default_data


def test_harness_get_agents():
    harness = make_harness()
    agents = harness.get_agents()
    assert len(agents) == 2
    ids = {a.agent_id for a in agents}
    assert ids == {"coding", "knowledge"}


def test_harness_get_agent():
    harness = make_harness()
    agent = harness.get_agent("coding")
    assert agent is not None
    assert agent.backend_id == "default"
    assert harness.get_agent("nonexistent") is None


def test_harness_get_agents_for_node_type():
    harness = make_harness()
    llm_agents = harness.get_agents_for_node_type("llm")
    assert len(llm_agents) == 1
    assert llm_agents[0].agent_id == "coding"
    # repository-analysis 能力 → knowledge_retrieval
    kr_agents = harness.get_agents_for_node_type("knowledge_retrieval")
    assert len(kr_agents) == 1
    assert kr_agents[0].agent_id == "knowledge"


def test_harness_parse_intent_create():
    harness = make_harness()
    result = harness.parse_intent("帮我创建一个代码审查流程")
    assert result["intent"] == "create_workflow"
    assert result["confidence"] >= 0.9


def test_harness_parse_intent_chat():
    harness = make_harness()
    result = harness.parse_intent("你好")
    assert result["intent"] == "chat_reply"


def test_harness_decide_create_routes_to_generator():
    """决策引擎将创建意图路由到 WorkflowGenerator。"""
    harness = make_harness()
    result = harness.decide("帮我创建一个代码审查流程")
    assert "generation_id" in result
    assert result.get("workflow_id") == "flow-new"


def test_harness_decide_modify_routes_to_generator():
    harness = make_harness()
    result = harness.decide("添加一个审批节点", workflow_id="flow-1")
    assert "generation_id" in result
    assert result.get("workflow_id") == "flow-1"


def test_harness_decide_optimize_routes_to_generator():
    harness = make_harness()
    result = harness.decide("优化工作流性能", workflow_id="flow-1")
    assert "generation_id" in result
    assert result["workflow_id"] == "flow-1"


def test_harness_decide_chat_reply():
    harness = make_harness()
    result = harness.decide("你好")
    assert result["action"] == "chat_reply"
    assert "reply" in result


def test_harness_decide_unknown():
    harness = make_harness()
    result = harness.decide("asdfghjklqwertyuiop")
    assert result["action"] == "clarify"


def test_harness_workflow_generator_not_none():
    harness = make_harness()
    assert harness.workflow_generator is not None
    assert isinstance(harness.workflow_generator, WorkflowGenerator)


def test_harness_reload():
    harness = make_harness()
    # reload 不应报错（即使通用 Harness 不可达）
    harness.reload()
    assert harness.status()["ready"] is True


def test_harness_begin_generation():
    harness = make_harness()
    state = harness.begin_generation("flow-1", "gen-xyz")
    assert state.generation_id == "gen-xyz"
    assert state.workflow_id == "flow-1"
    assert state.status == "idle"


def test_get_default_node_types_and_capabilities():
    harness = make_harness()
    node_dicts = harness.get_default_node_types()
    assert isinstance(node_dicts, list)
    assert len(node_dicts) >= 20
    caps = harness.get_agent_capabilities()
    assert isinstance(caps, list)
    assert len(caps) == 2


def test_harness_without_generator():
    """无 generator 时，生成类意图回退到 DecisionEngine 的容错路径。"""
    store = FakeStore()
    harness = CreatorHarness(
        store=store,
        general_harness_url="http://127.0.0.1:1",
        auto_sync=False,
        generator=None,
    )
    # 无 generator 时 workflow_generator 为 None
    assert harness.workflow_generator is None
    # 无 generator 的生成意图会抛 DecisionError（由 decision_engine 抛出）
    from openagent_studio.creator.errors import DecisionError
    with pytest.raises(DecisionError):
        harness.decide("帮我创建一个流程")