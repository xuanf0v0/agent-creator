"""Intent Parser 单元测试 — Creator Harness Layer 1"""

from __future__ import annotations

import pytest
from openagent_studio.creator import IntentParser, IntentResult, IntentType, IntentError


def test_parse_create_workflow_intent():
    parser = IntentParser()
    result = parser.parse("帮我创建一个代码审查流程")
    assert result.intent == IntentType.CREATE_WORKFLOW
    assert result.confidence >= 0.9


def test_parse_create_workflow_from_scratch():
    parser = IntentParser()
    result = parser.parse("从零开始设计一个自动化部署流程")
    assert result.intent == IntentType.CREATE_WORKFLOW
    assert result.confidence >= 0.9


def test_parse_modify_workflow_intent():
    parser = IntentParser()
    result = parser.parse("添加一个审批节点到当前流程")
    assert result.intent == IntentType.MODIFY_WORKFLOW
    assert result.confidence >= 0.85


def test_parse_modify_delete_intent():
    parser = IntentParser()
    result = parser.parse("删除最后一个节点")
    assert result.intent == IntentType.MODIFY_WORKFLOW


def test_parse_repair_workflow_intent():
    parser = IntentParser()
    result = parser.parse("工作流运行报错了，帮我修复一下")
    assert result.intent == IntentType.REPAIR_WORKFLOW


def test_parse_optimize_workflow_intent():
    parser = IntentParser()
    result = parser.parse("优化一下这个工作流的性能")
    assert result.intent == IntentType.OPTIMIZE_WORKFLOW
    assert result.confidence >= 0.85


def test_parse_chat_greeting():
    parser = IntentParser()
    result = parser.parse("你好")
    assert result.intent == IntentType.CHAT_REPLY
    assert result.confidence >= 0.9


def test_parse_identity_question():
    parser = IntentParser()
    result = parser.parse("你是谁")
    assert result.intent == IntentType.CHAT_REPLY
    assert result.confidence >= 0.9


def test_parse_unknown_intent():
    parser = IntentParser(llm_classify=False)
    result = parser.parse("今天的天气真不错")
    assert result.intent == IntentType.UNKNOWN
    assert result.confidence == 0.0


def test_parse_empty_message_raises():
    parser = IntentParser()
    with pytest.raises(IntentError):
        parser.parse("")
    with pytest.raises(IntentError):
        parser.parse("   ")


def test_parse_with_workflow_id():
    parser = IntentParser()
    result = parser.parse("添加一个节点", workflow_id="flow-123")
    assert result.intent == IntentType.MODIFY_WORKFLOW
    assert result.workflow_id == "flow-123"


def test_parse_confidence_threshold():
    """低置信度时，LLM 分类关闭则返回规则结果（即使低）。"""
    parser = IntentParser(llm_classify=False, confidence_threshold=0.99)
    result = parser.parse("创建一个工作流")
    # 规则匹配给 0.95，低于 0.99 阈值但没有 LLM 回退，返回规则结果
    assert result is not None
    assert result.intent == IntentType.CREATE_WORKFLOW


def test_rule_match_optimize_refactor():
    parser = IntentParser()
    result = parser.parse("重构这个工作流")
    assert result.intent == IntentType.OPTIMIZE_WORKFLOW


def test_rule_match_modify_connect():
    parser = IntentParser()
    result = parser.parse("把审批节点连接到输出节点")
    assert result.intent == IntentType.MODIFY_WORKFLOW


def test_parse_create_workflow_variants():
    parser = IntentParser()
    variants = [
        "做个客服机器人工作流",
        "设计一个数据处理流程",
        "帮我创建一个审批流程",
        "给我生成一个工作流",
    ]
    for msg in variants:
        result = parser.parse(msg)
        assert result.intent == IntentType.CREATE_WORKFLOW, f"应为 CREATE_WORKFLOW: {msg}"


def test_parse_modify_update_variants():
    parser = IntentParser()
    variants = [
        "修改一下这个流程",
        "更新 LLM 节点的提示词",
        "改一下节点配置",
        "调整一下顺序",
    ]
    for msg in variants:
        result = parser.parse(msg)
        assert result.intent == IntentType.MODIFY_WORKFLOW, f"应为 MODIFY_WORKFLOW: {msg}"


def test_parse_repair_variants():
    parser = IntentParser()
    variants = [
        "修复工作流运行错误",
        "工作流出问题了",
        "智能体挂了",
    ]
    for msg in variants:
        result = parser.parse(msg)
        assert result.intent == IntentType.REPAIR_WORKFLOW, f"应为 REPAIR_WORKFLOW: {msg}"


def test_intent_result_dataclass():
    result = IntentResult(
        intent=IntentType.CREATE_WORKFLOW,
        confidence=0.95,
        workflow_id="flow-1",
        reasoning="关键词匹配",
        raw_message="创建流程",
    )
    assert result.intent == IntentType.CREATE_WORKFLOW
    assert result.confidence == 0.95
    assert result.workflow_id == "flow-1"
    assert result.raw_message == "创建流程"


def test_create_intent_parser():
    from openagent_studio.creator import create_intent_parser
    parser = create_intent_parser()
    assert parser is not None
    assert parser._llm_classify is True