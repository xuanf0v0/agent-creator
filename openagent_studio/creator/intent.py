from __future__ import annotations

import json
import logging
import re
from typing import Any

from .errors import IntentError
from .models import IntentResult, IntentType

logger = logging.getLogger(__name__)

# 规则匹配的关键词 — 快速分类路径
_INTENT_RULES: list[tuple[re.Pattern, IntentType, float]] = [
    # CREATE_WORKFLOW
    (re.compile(r"^(创建|新建|做个|设计|生成|构建|搭建|编写|开发|造个).*?(工作流|流程|智能体|机器人|助手)"), IntentType.CREATE_WORKFLOW, 0.95),
    (re.compile(r"^(我要|我想|帮我|请|给我)(创建|新建|设计|生成|构建|做个)"), IntentType.CREATE_WORKFLOW, 0.90),
    (re.compile(r"^从零开始|从头开始|空白工作流"), IntentType.CREATE_WORKFLOW, 0.95),
    # MODIFY_WORKFLOW
    (re.compile(r"^(添加|增加|加入|新增|加个|加一).*(节点|步骤|模块|功能)"), IntentType.MODIFY_WORKFLOW, 0.90),
    (re.compile(r"^(修改|更改|更新|编辑|改一下|调整|换).*"), IntentType.MODIFY_WORKFLOW, 0.85),
    (re.compile(r"^(删除|移除|去掉|删掉|清除).*"), IntentType.MODIFY_WORKFLOW, 0.90),
    (re.compile(r"^(连接|连线|把.*连|连接.*到)"), IntentType.MODIFY_WORKFLOW, 0.80),
    # REPAIR_WORKFLOW
    (re.compile(r"^(修复|修理|修好|修一下|救).*"), IntentType.REPAIR_WORKFLOW, 0.90),
    (re.compile(r"(坏了|错误|失败|出问题|报错|崩溃|不工作|无效|挂了)"), IntentType.REPAIR_WORKFLOW, 0.80),
    # OPTIMIZE_WORKFLOW
    (re.compile(r"^(优化|改进|改善|提升|重构|重写|精简)"), IntentType.OPTIMIZE_WORKFLOW, 0.85),
    (re.compile(r"(优化|改进|改善|提升|重构|精简|加速|更.*好)"), IntentType.OPTIMIZE_WORKFLOW, 0.70),
    # CHAT_REPLY — 问候
    (re.compile(r"^(你好|嗨|hi|hello|你好啊|早上好|下午好|晚上好)$"), IntentType.CHAT_REPLY, 0.95),
    (re.compile(r"^(你是谁|你能做什么|你是什么)"), IntentType.CHAT_REPLY, 0.95),
]

# 当规则匹配不足时，使用 LLM 分类的 prompt
INTENT_CLASSIFICATION_PROMPT = """你是 OpenAgent Studio 的意图分析器。分析用户消息并输出 JSON。

只输出 <result>{JSON}</result>，JSON 格式：
{
  "intent": "create_workflow | modify_workflow | repair_workflow | optimize_workflow | chat_reply",
  "confidence": 0.0~1.0,
  "reasoning": "简短的中文判断理由"
}

意图定义：
- create_workflow: 用户要求从零创建新工作流，或表达需要自动化流程但尚未有工作流
- modify_workflow: 用户要求修改现有工作流（增/删/改节点或连线）
- repair_workflow: 用户报告工作流运行出错、失败或异常，需要修复
- optimize_workflow: 用户要求优化、改进、重构现有工作流
- chat_reply: 用户只是打招呼、问概念、问建议、问问题，不是要改工作流

判断规则：
1. 如果没有现有工作流，用户说"帮我做X"通常是 create_workflow
2. 如果已有工作流，用户说"添加X"是 modify_workflow
3. 用户说"运行失败了"/"报错了"是 repair_workflow
4. 用户问"怎么优化"/"能不能改进"是 optimize_workflow
5. 纯打招呼、问概念、问建议、问用法是 chat_reply

当前工作流：{workflow_context}
用户消息：{message}
"""


class IntentParser:
    """用户意图解析器。

    职责：
    - 将用户自然语言消息分类为结构化 IntentType
    - 提供置信度评分
    - 规则匹配优先，低置信度时回退到 LLM 分类
    """

    def __init__(
        self,
        store: Any | None = None,
        llm_classify: bool = True,
        confidence_threshold: float = 0.6,
    ) -> None:
        self._store = store
        self._llm_classify = llm_classify
        self._confidence_threshold = confidence_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(
        self,
        message: str,
        workflow_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> IntentResult:
        """解析用户消息，返回意图结果。

        Args:
            message: 用户消息文本。
            workflow_id: 当前工作流 ID（如果有）。
            history: 最近对话历史。

        Returns:
            IntentResult 结构。
        """
        if not message or not message.strip():
            raise IntentError("消息不能为空")

        message = message.strip()

        # 1. 规则匹配快速路径
        result = self._rule_match(message)
        if result and result.confidence >= self._confidence_threshold:
            logger.debug(
                "规则匹配意图: %s (confidence=%.2f, reason=%s)",
                result.intent, result.confidence, result.reasoning,
            )
            result.workflow_id = workflow_id
            return result

        # 2. 低置信度时回退到 LLM 分类
        if self._llm_classify:
            try:
                result = self._llm_classify_intent(message, workflow_id, history)
                if result and result.confidence >= self._confidence_threshold:
                    logger.debug(
                        "LLM 分类意图: %s (confidence=%.2f, reason=%s)",
                        result.intent, result.confidence, result.reasoning,
                    )
                    return result
            except Exception as exc:
                logger.warning("LLM 意图分类失败，回退规则结果: %s", exc)

        # 3. 默认：使用规则结果（即使置信度低）或 UNKNOWN
        if result:
            return result
        return IntentResult(
            intent=IntentType.UNKNOWN,
            confidence=0.0,
            workflow_id=workflow_id,
            reasoning="无法确定用户意图",
            raw_message=message,
        )

    # ------------------------------------------------------------------
    # 规则匹配
    # ------------------------------------------------------------------

    def _rule_match(self, message: str) -> IntentResult | None:
        """尝试用关键词规则匹配意图。"""
        for pattern, intent, confidence in _INTENT_RULES:
            match = pattern.search(message)
            if match:
                return IntentResult(
                    intent=intent,
                    confidence=confidence,
                    reasoning=f"关键词匹配: {match.group(0)}",
                    raw_message=message,
                )
        return None

    # ------------------------------------------------------------------
    # LLM 分类
    # ------------------------------------------------------------------

    def _llm_classify_intent(
        self,
        message: str,
        workflow_id: str | None,
        history: list[dict[str, str]] | None,
    ) -> IntentResult | None:
        """使用 LLM 对消息进行意图分类。

        需要 generator 或外部 LLM 调用来执行。
        """
        # 构建工作流上下文描述
        workflow_context = "无（新建工作流）"
        if workflow_id and self._store:
            try:
                spec = self._store.load()
                workflow = next(
                    (w for w in spec.workflows if w.id == workflow_id),
                    None,
                )
                if workflow:
                    nodes = workflow.nodes
                    workflow_context = (
                        f"工作流 {workflow.name} ({workflow.id})，"
                        f"包含 {len(nodes)} 个节点"
                    )
            except Exception:
                pass

        prompt = INTENT_CLASSIFICATION_PROMPT.format(
            workflow_context=workflow_context,
            message=message,
        )
        # LLM 调用由外部注入，这里返回 None 表示需要外部回退
        logger.debug("LLM 分类 prompt: %s", prompt[:200])
        return None


# 后端快捷函数：创建默认解析器
def create_intent_parser(store: Any | None = None) -> IntentParser:
    return IntentParser(store=store, llm_classify=True)