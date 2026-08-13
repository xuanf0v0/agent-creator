from __future__ import annotations

import logging
from typing import Any

from .errors import DecisionError
from .intent import IntentParser, create_intent_parser
from .models import CreatorDecision, CreatorState, IntentResult, IntentType, slugify_workflow_id
from .registry import AgentCapabilityRegistry

logger = logging.getLogger(__name__)

# 决策类型到动作的映射
_ACTION_MAPPING: dict[IntentType, str] = {
    IntentType.CREATE_WORKFLOW: "create_workflow",
    IntentType.MODIFY_WORKFLOW: "modify_workflow",
    IntentType.REPAIR_WORKFLOW: "repair_workflow",
    IntentType.OPTIMIZE_WORKFLOW: "optimize_workflow",
    IntentType.CHAT_REPLY: "chat_reply",
    IntentType.UNKNOWN: "reply_clarify",
}


class DecisionEngine:
    """工作流创作决策引擎。

    职责：
    - 接收 IntentParser 的解析结果
    - 根据当前工作流状态产生结构化决策
    - 编排 GeneratorManager 完成实际执行
    - 管理多轮对话的决策状态
    """

    def __init__(
        self,
        store: Any | None = None,
        registry: AgentCapabilityRegistry | None = None,
        generator: Any | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._generator = generator
        self._intent_parser = create_intent_parser(store)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide(
        self,
        message: str,
        workflow_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> IntentResult:
        """解析用户意图并返回决策结果。

        这是决策引擎的主要入口点：
        1. 调用 IntentParser 解析用户意图
        2. 根据意图和当前状态决定下一步行动
        3. 返回 IntentResult（包含决策类型）
        """
        # 步骤 1: 解析用户意图
        intent_result = self._intent_parser.parse(message, workflow_id, history)

        # 步骤 2: 验证和调整决策
        intent_result = self._validate_intent(intent_result, workflow_id)

        logger.info(
            "意图解析结果: intent=%s confidence=%.2f workflow_id=%s",
            intent_result.intent.value,
            intent_result.confidence,
            intent_result.workflow_id,
        )
        return intent_result

    def create_workflow(
        self,
        message: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """开始创建新工作流。

        委托给 GeneratorManager 执行。
        """
        if not self._generator:
            raise DecisionError("Generator 不可用，无法创建工作流")

        try:
            generation = self._generator.create(
                workflow_id=slugify_workflow_id(name or message),  # 新工作流，ID 由名称/消息生成
                message=message,
                name=name,
            )
            return {
                "action": "create_workflow",
                "generation_id": generation.id,
                "workflow_id": generation.workflow_id,
                "status": "started",
            }
        except Exception as exc:
            raise DecisionError(f"创建工作流失败: {exc}") from exc

    def modify_workflow(
        self,
        workflow_id: str,
        message: str,
    ) -> dict[str, Any]:
        """修改现有工作流。

        委托给 GeneratorManager 执行。
        """
        if not self._generator:
            raise DecisionError("Generator 不可用，无法修改工作流")

        try:
            generation = self._generator.start(workflow_id, message)
            return {
                "action": "modify_workflow",
                "generation_id": generation.id,
                "workflow_id": workflow_id,
                "status": "started",
            }
        except Exception as exc:
            raise DecisionError(f"修改工作流失败: {exc}") from exc

    def repair_workflow(
        self,
        workflow_id: str,
        failure_context: str | None = None,
    ) -> dict[str, Any]:
        """修复工作流故障。

        委托给 GeneratorManager 执行。
        """
        if not self._generator:
            raise DecisionError("Generator 不可用，无法修复工作流")

        # 修复场景：复用 modify_workflow，传入失败信息
        message = failure_context or "修复工作流故障"
        try:
            generation = self._generator.start(workflow_id, message)
            return {
                "action": "repair_workflow",
                "generation_id": generation.id,
                "workflow_id": workflow_id,
                "status": "started",
            }
        except Exception as exc:
            raise DecisionError(f"修复工作流失败: {exc}") from exc

    def optimize_workflow(
        self,
        workflow_id: str,
    ) -> dict[str, Any]:
        """优化工作流。

        委托给 GeneratorManager 执行。
        """
        if not self._generator:
            raise DecisionError("Generator 不可用，无法优化工作流")

        try:
            generation = self._generator.optimize(workflow_id)
            return {
                "action": "optimize_workflow",
                "generation_id": generation.id,
                "workflow_id": workflow_id,
                "status": "started",
            }
        except Exception as exc:
            raise DecisionError(f"优化工作流失败: {exc}") from exc

    def chat_reply(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """处理闲聊回复。

        直接回复用户，不涉及工作流修改。
        """
        # 简单的闲聊回复，可以后续接 LLM 生成更丰富的回复
        reply_text = self._generate_reply(message, history)
        return {
            "action": "chat_reply",
            "reply": reply_text,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _validate_intent(
        self,
        intent: IntentResult,
        workflow_id: str | None,
    ) -> IntentResult:
        """验证和调整意图结果。

        规则：
        - 如果没有现有工作流，用户意图不可能是 modify/repair/optimize
        - 这种情况降级为 create_workflow 或 chat_reply
        """
        # 如果没有现有工作流但用户要求修改/修复/优化
        if not workflow_id:
            if intent.intent in (
                IntentType.MODIFY_WORKFLOW,
                IntentType.REPAIR_WORKFLOW,
                IntentType.OPTIMIZE_WORKFLOW,
            ):
                # 降级为创建或闲聊
                if intent.confidence < 0.7:
                    # 低置信度 → 闲聊
                    return IntentResult(
                        intent=IntentType.CHAT_REPLY,
                        confidence=0.5,
                        workflow_id=None,
                        reasoning="没有现有工作流，无法修改/修复/优化，将转为创建新工作流",
                        raw_message=intent.raw_message,
                    )
                # 高置信度 → 创建
                intent.intent = IntentType.CREATE_WORKFLOW
                intent.reasoning += "（无现有工作流，自动转为创建新工作流）"
        return intent

    def _generate_reply(
        self,
        message: str,
        history: list[dict[str, str]] | None,
    ) -> str:
        """生成闲聊回复。

        目前是简单的占位符，后续可以接入 LLM 生成更丰富的回复。
        """
        msg_lower = message.lower().strip()
        if msg_lower in ("你好", "嗨", "hi", "hello", "你好啊"):
            return "你好！我是 OpenAgent Studio 的工作流创作助手。我可以帮助你创建、修改、优化和修复智能体工作流。请告诉我你需要什么帮助？"
        if "你是谁" in msg_lower or "你能做什么" in msg_lower:
            return "我是 OpenAgent Studio 的 AI 工作流助手。我可以帮你：\n1. 从零创建工作流\n2. 修改现有工作流（添加/删除/调整节点）\n3. 修复运行失败的工作流\n4. 优化工作流性能和结构\n\n告诉我你想做什么？"
        return "感谢你的消息！如果你有任何关于工作流创作的问题，请随时告诉我。"


# 后端快捷函数：创建默认决策引擎
def create_decision_engine(
    store: Any | None = None,
    registry: AgentCapabilityRegistry | None = None,
    generator: Any | None = None,
) -> DecisionEngine:
    return DecisionEngine(store=store, registry=registry, generator=generator)