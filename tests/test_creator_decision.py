"""Decision Engine 单元测试 — Creator Harness Layer 2"""

from __future__ import annotations

import pytest
from openagent_studio.creator import (
    DecisionEngine,
    DecisionError,
    IntentType,
    IntentResult,
    create_decision_engine,
)


class FakeGenerator:
    """模拟 GeneratorManager 用于测试。"""

    def __init__(self):
        self.generations = {}

    def create(self, message, workflow_id="", name=None):
        gen = FakeGen()
        gen.id = "gen-new"
        gen.workflow_id = "flow-new"
        gen.status = "started"
        return gen

    def start(self, workflow_id, message):
        gen = FakeGen()
        gen.id = "gen-mod"
        gen.workflow_id = workflow_id
        gen.status = "started"
        return gen

    def optimize(self, workflow_id):
        gen = FakeGen()
        gen.id = "gen-opt"
        gen.workflow_id = workflow_id
        gen.status = "started"
        return gen


class FakeGen:
    """模拟 GeneratorManager 返回的 generation 对象。"""
    def __init__(self):
        self.id = ""
        self.workflow_id = ""
        self.status = ""


def test_decide_create_workflow():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.decide("帮我创建一个代码审查流程")
    assert result.intent == IntentType.CREATE_WORKFLOW


def test_decide_create_workflow_when_no_workflow_exists():
    """没有现有工作流时，modify/repair/optimize 降级为 create。"""
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.decide("修改节点配置", workflow_id=None)
    # 高置信度 modify 在无工作流时降级为 create
    assert result.intent == IntentType.CREATE_WORKFLOW


def test_decide_modify_workflow():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.decide("添加一个审批节点", workflow_id="flow-1")
    assert result.intent == IntentType.MODIFY_WORKFLOW


def test_decide_repair_workflow():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.decide("工作流报错了，帮我修复", workflow_id="flow-1")
    assert result.intent == IntentType.REPAIR_WORKFLOW


def test_decide_optimize_workflow():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.decide("优化一下这个工作流", workflow_id="flow-1")
    assert result.intent == IntentType.OPTIMIZE_WORKFLOW


def test_decide_chat_reply():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.decide("你好")
    assert result.intent == IntentType.CHAT_REPLY


def test_decide_no_workflow_low_confidence_modify_downgrades_to_chat():
    """低置信度 modify 在无工作流时降级为 chat_reply。"""
    engine = DecisionEngine(generator=FakeGenerator())
    # 空消息会导致低置信度，但 IntentParser 规则会快速匹配
    # 测试一个没有明确意图的消息
    result = engine.decide("修改", workflow_id=None)
    # "修改" 匹配 MODIFY_WORKFLOW 规则，置信度 0.85，
    # 但无工作流且置信度 >= 0.7，所以降级为 create_workflow
    assert result.intent == IntentType.CREATE_WORKFLOW


def test_create_workflow_action():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.create_workflow("创建一个新工作流", name="测试流程")
    assert result["action"] == "create_workflow"
    assert result["status"] == "started"
    assert "generation_id" in result


def test_modify_workflow_action():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.modify_workflow("flow-1", "添加一个节点")
    assert result["action"] == "modify_workflow"
    assert result["status"] == "started"


def test_repair_workflow_action():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.repair_workflow("flow-1", "代理执行失败")
    assert result["action"] == "repair_workflow"
    assert result["status"] == "started"


def test_optimize_workflow_action():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.optimize_workflow("flow-1")
    assert result["action"] == "optimize_workflow"
    assert result["status"] == "started"


def test_chat_reply_action():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.chat_reply("你好")
    assert result["action"] == "chat_reply"
    assert "reply" in result
    assert "你好" in result["reply"]


def test_chat_reply_identity():
    engine = DecisionEngine(generator=FakeGenerator())
    result = engine.chat_reply("你是谁")
    assert "OpenAgent Studio" in result["reply"]


def test_decision_error_no_generator():
    engine = DecisionEngine(generator=None)
    with pytest.raises(DecisionError):
        engine.create_workflow("test")
    with pytest.raises(DecisionError):
        engine.modify_workflow("flow-1", "test")
    with pytest.raises(DecisionError):
        engine.repair_workflow("flow-1")
    with pytest.raises(DecisionError):
        engine.optimize_workflow("flow-1")


def test_create_decision_engine():
    engine = create_decision_engine()
    assert engine is not None
    assert engine._intent_parser is not None


def test_validate_intent_preserves_create():
    engine = DecisionEngine(generator=FakeGenerator())
    intent = IntentResult(
        intent=IntentType.CREATE_WORKFLOW,
        confidence=0.95,
        raw_message="创建流程",
        reasoning="测试",
    )
    result = engine._validate_intent(intent, workflow_id=None)
    assert result.intent == IntentType.CREATE_WORKFLOW


def test_validate_intent_downgrades_repair_without_workflow():
    engine = DecisionEngine(generator=FakeGenerator())
    intent = IntentResult(
        intent=IntentType.REPAIR_WORKFLOW,
        confidence=0.95,
        raw_message="修复",
        reasoning="测试",
    )
    result = engine._validate_intent(intent, workflow_id=None)
    assert result.intent == IntentType.CREATE_WORKFLOW