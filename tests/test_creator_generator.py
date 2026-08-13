"""Workflow Generator 单元测试 — Creator Harness Layer 3"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from openagent_studio.creator import WorkflowGenerator, create_workflow_generator, GenerationError


@dataclass
class FakeGeneration:
    """模拟 GeneratorManager 的 Generation 对象。"""
    id: str = "gen-1"
    workflow_id: str = "flow-1"
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_sequence: int = 0
    event_signal: threading.Condition = field(default_factory=threading.Condition)
    completed: bool = False
    cancelled: bool = False
    stalled: bool = False
    process: Any = None
    mode: str = "modify"
    messages: list[dict[str, str]] = field(default_factory=list)
    last_failure: dict[str, Any] = None

    def emit(self, event: str, data: dict[str, Any]) -> None:
        with self.event_signal:
            item = {"event": event, "data": data, "sequence": self.next_event_sequence, "timestamp": time.time()}
            self.next_event_sequence += 1
            self.events.append(item)
            self.event_signal.notify_all()


class FakeGeneratorManager:
    """模拟 GeneratorManager 用于测试 WorkflowGenerator。"""

    def __init__(self):
        self.generations: dict[str, FakeGeneration] = {}
        self.history: dict[str, list] = {}

    def create(self, message, workflow_id="", name=None):
        gen = FakeGeneration(id="gen-new", workflow_id="flow-new", mode="create")
        self.generations[gen.id] = gen
        return gen

    def create_direct(self, message, workflow_id="", name=None):
        return self.create(message, workflow_id=workflow_id, name=name)

    def start(self, workflow_id, message):
        gen = FakeGeneration(id="gen-start", workflow_id=workflow_id, mode="modify")
        self.generations[gen.id] = gen
        return gen

    def resume(self, generation_id, message):
        gen = self.generations.get(generation_id)
        if gen is None:
            gen = FakeGeneration(id=generation_id, workflow_id="flow-1", mode="modify")
            self.generations[gen.id] = gen
        return gen

    def optimize(self, workflow_id):
        gen = FakeGeneration(id="gen-opt", workflow_id=workflow_id, mode="optimize")
        self.generations[gen.id] = gen
        return gen

    def cancel(self, generation_id):
        gen = self.require(generation_id)
        gen.cancelled = True
        return gen

    def require(self, generation_id):
        if generation_id not in self.generations:
            raise KeyError(generation_id)
        gen = self.generations[generation_id]
        # 模拟完成状态
        gen.completed = True
        gen.next_event_sequence = len(gen.events)
        return gen


class FakeStore:
    """模拟 SpecStore 用于测试。"""

    def __init__(self):
        self._spec = {"version": "1", "name": "Test", "workflows": []}

    def load(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            workflows=[SimpleNamespace(id="flow-1", name="Test Flow", nodes=[], _data={})]
        )


def test_generate_create_workflow():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    result = wg.generate(message="创建一个代码审查流程")
    assert result["generation_id"] == "gen-new"
    assert result["workflow_id"] == "flow-new"
    assert result["status"] == "running"


def test_generate_modify_workflow():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    result = wg.generate(message="添加一个审批节点", workflow_id="flow-1")
    assert result["generation_id"] == "gen-start"
    assert result["workflow_id"] == "flow-1"
    assert result["status"] == "running"


def test_generate_with_name():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    result = wg.generate(message="创建一个新流程", name="我的新流程")
    assert result["status"] == "running"


def test_optimize_workflow():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    result = wg.optimize(workflow_id="flow-1")
    assert result["generation_id"] == "gen-opt"
    assert result["status"] == "running"


def test_cancel_generation():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    # 先创建一个 generation
    wg.generate(message="创建一个流程")
    result = wg.cancel("gen-new")
    assert result["cancelled"] is True


def test_cancel_already_cancelled():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="创建一个流程")
    # 第一次取消
    wg.cancel("gen-new")
    # 第二次取消应返回已取消
    result = wg.cancel("gen-new")
    assert result["cancelled"] is True


def test_get_generation():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="创建一个流程")
    gen_info = wg.get_generation("gen-new")
    assert gen_info is not None
    assert gen_info["id"] == "gen-new"
    assert "status" in gen_info
    assert "workflow_id" in gen_info


def test_get_generation_not_found():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    result = wg.get_generation("nonexistent")
    assert result is None


def test_list_generations():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="创建一个流程")
    wg.generate(message="修改现有流程", workflow_id="flow-1")
    all_gens = wg.list_generations()
    assert len(all_gens) == 2


def test_list_generations_filter_by_workflow():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="创建一个流程")
    wg.generate(message="修改现有流程", workflow_id="flow-1")
    flow_gens = wg.list_generations(workflow_id="flow-1")
    assert len(flow_gens) == 1


def test_stream_events():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="创建一个流程")
    gen = gm.generations["gen-new"]
    gen.emit("generation.step", {"step": 1, "message": "正在创建节点"})
    gen.emit("generation.node_created", {"node_id": "node-1", "node_type": "llm"})
    gen.completed = True
    gen.next_event_sequence = len(gen.events)

    events = list(wg.stream_events("gen-new"))
    # 应该有 started + step + node_created 事件
    assert len(events) >= 3
    assert any("generation.step" in e for e in events)
    assert any("generation.node_created" in e for e in events)


def test_stream_events_expired():
    """过期 generation 应返回 failed 事件。"""
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    events = list(wg.stream_events("nonexistent"))
    assert len(events) == 1
    assert "generation.failed" in events[0]


def test_find_running_generation():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="修改流程", workflow_id="flow-1")
    # 第二次调用应找到已有运行中的 generation 并 resume
    result = wg.generate(message="再改一下", workflow_id="flow-1")
    assert result["generation_id"] == "gen-start"  # 同一个 generation


def test_generation_status_running():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="创建一个流程")
    gen = gm.generations["gen-new"]
    status = wg._generation_status(gen)
    assert status == "running"


def test_generation_status_completed():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="创建一个流程")
    gen = gm.generations["gen-new"]
    gen.completed = True
    status = wg._generation_status(gen)
    assert status == "completed"


def test_generation_status_cancelled():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="创建一个流程")
    gen = gm.generations["gen-new"]
    gen.cancelled = True
    status = wg._generation_status(gen)
    assert status == "cancelled"


def test_get_messages():
    store = FakeStore()
    gm = FakeGeneratorManager()
    gm.history["flow-1"] = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
    ]
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    messages = wg.get_messages("flow-1")
    assert len(messages) == 2
    assert messages[0]["role"] == "user"


def test_get_chat_status_no_history():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    status = wg.get_chat_status("flow-1")
    assert status["has_history"] is False
    assert status["active_generation"] is None


def test_get_chat_status_with_history():
    store = FakeStore()
    gm = FakeGeneratorManager()
    gm.history["flow-1"] = [{"role": "user", "content": "你好"}]
    wg = WorkflowGenerator(store=store, generator_manager=gm)
    wg.generate(message="修改流程", workflow_id="flow-1")
    status = wg.get_chat_status("flow-1")
    assert status["has_history"] is True


def test_create_workflow_generator():
    store = FakeStore()
    gm = FakeGeneratorManager()
    wg = create_workflow_generator(store=store, generator_manager=gm)
    assert wg is not None
    assert wg._store is store
    assert wg._generator is gm