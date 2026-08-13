from __future__ import annotations

import dataclasses
import logging
from typing import Any

from .errors import RegistryError
from .intent import IntentParser, create_intent_parser
from .decision import DecisionEngine, create_decision_engine
from .generator import WorkflowGenerator, create_workflow_generator
from .models import AgentCapability, CreatorState, IntentType, NodeTypeInfo
from .registry import AgentCapabilityRegistry

logger = logging.getLogger(__name__)

# 节点类型目录 — 与前端 nodeCatalog 保持一致，由后端统一下发
_NODE_TYPE_CATALOG: list[NodeTypeInfo] = [
    # 触发器
    NodeTypeInfo(type="manual_trigger", label="手动触发", category="触发器", icon="▶", description="从表单或 API 输入启动流程"),
    NodeTypeInfo(type="webhook", label="Webhook", category="触发器", icon="⚡", description="通过 HTTP Webhook 触发", default_data={"path": "/hooks/workflow", "method": "POST"}),
    NodeTypeInfo(type="schedule", label="定时触发", category="触发器", icon="◷", description="使用 Cron 计划触发", default_data={"cron": "0 9 * * *", "timezone": "Asia/Shanghai"}),
    # AI
    NodeTypeInfo(type="llm", label="LLM", category="AI", icon="◆", description="调用模型完成单轮生成", requires_agent=True, default_data={"prompt": "请基于以下输入完成任务：\n{{latest}}"}),
    NodeTypeInfo(type="agent", label="智能体", category="AI", icon="✦", description="调用 Harness 智能体完成任务", requires_agent=True, default_data={"prompt": "请完成以下任务：\n{{latest}}", "relative_path": "."}),
    NodeTypeInfo(type="knowledge_retrieval", label="知识检索", category="AI", icon="⌕", description="从知识文档召回相关内容", requires_agent=True, default_data={"query": "{{latest}}", "top_k": 3, "documents": []}),
    NodeTypeInfo(type="tool", label="工具调用", category="AI", icon="⌘", description="通过 Harness 执行受治理工具", requires_agent=True, default_data={"prompt": "请调用合适的工具处理：\n{{latest}}"}),
    NodeTypeInfo(type="code", label="代码任务", category="AI", icon="</>", description="通过 Harness Agent 执行代码任务", requires_agent=True, default_data={"prompt": "请完成代码任务并运行验证：\n{{latest}}", "relative_path": "."}),
    # 数据处理
    NodeTypeInfo(type="prompt", label="模板", category="数据处理", icon="T", description="组织提示词或文本模板", default_data={"template": "{{latest}}"}),
    NodeTypeInfo(type="variable_set", label="变量赋值", category="数据处理", icon="x=", description="创建工作流变量对象", default_data={"variables": {}}),
    NodeTypeInfo(type="transform", label="数据转换", category="数据处理", icon="⇄", description="解析、提取、筛选或扁平化数据"),
    NodeTypeInfo(type="merge", label="合并", category="数据处理", icon="⋈", description="合并多个上游分支结果"),
    # 集成
    NodeTypeInfo(type="http_request", label="HTTP 请求", category="集成", icon="◎", description="调用 HTTPS API", default_data={"url": "", "method": "GET", "headers": {}, "body": {}, "timeout_seconds": 60, "allow_private": False, "fail_on_error": True}),
    # 流程控制
    NodeTypeInfo(type="condition", label="IF/ELSE", category="流程控制", icon="◇", description="根据布尔结果选择分支", default_data={"condition": "{{latest}}"}),
    NodeTypeInfo(type="switch", label="多路分支", category="流程控制", icon="⑂", description="按多个条件路由到不同分支", default_data={"cases": []}),
    NodeTypeInfo(type="parallel", label="并行", category="流程控制", icon="⋮", description="并行调度互不依赖的分支"),
    NodeTypeInfo(type="iteration", label="迭代", category="流程控制", icon="↻", description="遍历或按次数生成迭代项"),
    NodeTypeInfo(type="loop", label="循环", category="流程控制", icon="⟳", description="执行有限次数循环", default_data={"count": 3}),
    NodeTypeInfo(type="delay", label="等待", category="流程控制", icon="◴", description="延迟后继续执行", default_data={"seconds": 5}),
    # 人工与质量
    NodeTypeInfo(type="approval", label="人工审批", category="人工与质量", icon="✓", description="暂停并等待人工决定"),
    NodeTypeInfo(type="validator", label="验证器", category="人工与质量", icon="⌁", description="验证任务终态或业务规则", requires_agent=True, default_data={}),
    # 编排
    NodeTypeInfo(type="subworkflow", label="子工作流", category="编排", icon="▣", description="调用另一个工作流", default_data={"workflow_id": ""}),
    # 输出
    NodeTypeInfo(type="output", label="结束/输出", category="输出", icon="→", description="定义工作流最终输出"),
]

# 6 种节点类型需要 agent_id — 创建时由 Creator Harness 从注册表自动填充
AGENT_REQUIRING_NODE_TYPES = {"llm", "agent", "knowledge_retrieval", "tool", "code", "validator"}


class CreatorHarness:
    """Creator Harness — 针对工作流创作任务的专用 Harness。

    与通用 Harness（agent-harness）的关系：
    - 通用 Harness 负责：任务执行、Agent 治理、可靠性管理（不变）
    - 本 Creator Harness 负责：工作流创作意图、节点编排决策、能力注册

    双 Harness 共同驱动：创作阶段由 Creator Harness 主导，
    运行/验证阶段通过 Agent Capability Registry 复用通用 Harness 的 Agent。
    """

    def __init__(
        self,
        store: Any,
        general_harness_url: str = "http://127.0.0.1:8765",
        auto_sync: bool = True,
        generator: Any = None,
    ) -> None:
        self._store = store
        self._general_harness_url = general_harness_url
        self._registry = AgentCapabilityRegistry(store)
        self._node_types = _NODE_TYPE_CATALOG
        self._state: CreatorState | None = None
        self._generator = generator
        self._intent_parser = create_intent_parser(store)
        self._decision_engine = create_decision_engine(store, self._registry, generator)
        self._workflow_generator = create_workflow_generator(store, generator, self._registry) if generator else None
        if auto_sync:
            self._reload()

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        """从 store 重新加载 spec 并同步能力注册表。"""
        try:
            spec = self._store.raw()
        except Exception as exc:  # noqa: BLE001 - store 异常统一转为 RegistryError
            raise RegistryError(f"无法读取项目配置：{exc}") from exc
        self._registry.load_from_spec(spec)
        # 同步通用 Harness（失败不阻塞，降级为本地数据）
        self._registry.sync_with_general_harness(self._general_harness_url)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "ready": self._registry.is_ready(),
            "agents_count": len(self._registry.get_agents()),
            "general_harness_connected": self._registry.is_harness_connected(),
            "node_types_count": len(self._node_types),
            "state": dataclasses.asdict(self._state) if self._state else None,
            "intent_parser_ready": self._intent_parser is not None,
            "decision_engine_ready": self._decision_engine is not None,
            "workflow_generator_ready": self._workflow_generator is not None,
        }

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    @property
    def intent_parser(self) -> IntentParser:
        return self._intent_parser

    @property
    def decision_engine(self) -> DecisionEngine:
        return self._decision_engine

    @property
    def workflow_generator(self) -> WorkflowGenerator | None:
        return self._workflow_generator

    # ------------------------------------------------------------------
    # 决策入口（Layer 2 核心）
    # ------------------------------------------------------------------

    def decide(
        self,
        message: str,
        workflow_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """解析用户意图并执行相应的创作操作。

        这是 Creator Harness 的主要入口点：
        1. IntentParser 解析用户意图
        2. DecisionEngine 编排执行
        3. 返回结构化结果供前端使用

        Layer 3 起：生成类意图（create/modify/repair/optimize）
        统一经由 WorkflowGenerator 执行，以获得结构化事件流与进度追踪。
        """
        if not self._decision_engine:
            return {"error": "Decision Engine 不可用"}

        # 步骤 1: 解析意图
        intent_result = self._decision_engine.decide(message, workflow_id, history)

        # 步骤 2: 根据意图执行操作
        intent = intent_result.intent
        if intent == IntentType.CREATE_WORKFLOW:
            if self._workflow_generator:
                return self._workflow_generator.generate(message=message, workflow_id=None)
            return self._decision_engine.create_workflow(message)
        elif intent == IntentType.MODIFY_WORKFLOW:
            if self._workflow_generator:
                return self._workflow_generator.generate(message=message, workflow_id=workflow_id or "")
            return self._decision_engine.modify_workflow(workflow_id or "", message)
        elif intent == IntentType.REPAIR_WORKFLOW:
            if self._workflow_generator:
                return self._workflow_generator.generate(message=message, workflow_id=workflow_id or "")
            return self._decision_engine.repair_workflow(workflow_id or "")
        elif intent == IntentType.OPTIMIZE_WORKFLOW:
            if self._workflow_generator:
                return self._workflow_generator.optimize(workflow_id or "")
            return self._decision_engine.optimize_workflow(workflow_id or "")
        elif intent == IntentType.CHAT_REPLY:
            return self._decision_engine.chat_reply(message, history)
        else:
            return {
                "action": "clarify",
                "message": intent_result.reasoning or "我无法理解你的请求，请换一个方式表达？",
                "intent": intent.value,
            }

    def parse_intent(
        self,
        message: str,
        workflow_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """仅解析用户意图，不执行操作。"""
        if not self._intent_parser:
            return {"error": "Intent Parser 不可用"}
        result = self._intent_parser.parse(message, workflow_id, history)
        return dataclasses.asdict(result)

    def get_node_types(self) -> list[NodeTypeInfo]:
        return self._node_types

    def get_agents(self) -> list[AgentCapability]:
        return self._registry.get_agents()

    def get_agent(self, agent_id: str) -> AgentCapability | None:
        return self._registry.get_agent(agent_id)

    def get_agents_for_node_type(self, node_type: str) -> list[AgentCapability]:
        """返回能驱动指定节点类型的候选 agents。"""
        return self._registry.get_agents_for_node_type(node_type)

    def get_default_node_types(self) -> list[dict[str, Any]]:
        return [dataclasses.asdict(item) for item in self._node_types]

    def get_agent_capabilities(self) -> list[dict[str, Any]]:
        return [dataclasses.asdict(item) for item in self._registry.get_agents()]

    def reload(self) -> None:
        """外部配置变更后调用，刷新能力注册表。"""
        self._reload()

    # ------------------------------------------------------------------
    # 状态管理（Layer 2/3 将使用）
    # ------------------------------------------------------------------

    def begin_generation(self, workflow_id: str, generation_id: str) -> CreatorState:
        self._state = CreatorState(generation_id=generation_id, workflow_id=workflow_id)
        return self._state