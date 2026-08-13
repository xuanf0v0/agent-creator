from __future__ import annotations

import logging
from typing import Any

from .errors import RegistryError
from .models import AgentCapability

logger = logging.getLogger(__name__)

# 能力 → 节点类型映射
# 每个 Harness Agent 的 capability 标签决定了它能驱动哪些节点类型
CAPABILITY_NODE_TYPE_MAP: dict[str, list[str]] = {
    "text-generation": [
        "llm",
        "agent",
        "tool",
        "code",
    ],
    "repository-analysis": [
        "knowledge_retrieval",
        "validator",
    ],
    "test-execution": [
        "code",
        "validator",
    ],
}

# 后备：没有任何能力标签时，所有节点类型都可使用
ALL_NODE_TYPES = [
    "llm",
    "agent",
    "tool",
    "code",
    "http_request",
    "webhook",
    "manual_trigger",
    "cron_trigger",
    "condition",
    "loop",
    "delay",
    "output",
    "input",
    "transformer",
    "knowledge_retrieval",
    "validator",
    "notification",
    "database",
    "file_operation",
    "template",
    "parallel",
    "sub_workflow",
    "aggregator",
    "custom",
]


class AgentCapabilityRegistry:
    """Agent 能力注册表。

    职责：
    - 从 project.yaml 读取 harness 定义，提取能力元数据
    - 将能力标签映射到节点类型
    - 提供查询接口（按能力、按节点类型、按 agent_id）
    - 可选：与通用 Harness 的 Agent Catalog 同步
    """

    def __init__(self, store: Any | None = None) -> None:
        self._store = store
        self._agents: dict[str, AgentCapability] = {}
        self._harness_connected = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_from_spec(self, spec: dict[str, Any]) -> None:
        """从项目 spec 字典加载 harness agents。

        spec 结构对应 project.yaml 中的 ``harness`` 数组。
        """
        raw_agents: list[dict[str, Any]] = spec.get("harness", [])
        if not raw_agents:
            logger.warning("没有找到 harness agents")
            self._agents = {}
            return

        agents: dict[str, AgentCapability] = {}
        for entry in raw_agents:
            agent_id = entry.get("id", "")
            if not agent_id:
                continue

            labels = entry.get("labels", {}) or {}
            capability = ""
            sandbox = "read-only"
            for key, value in labels.items():
                if key.endswith("/capability"):
                    capability = str(value)
                elif key.endswith("/sandbox"):
                    sandbox = str(value)

            supported = CAPABILITY_NODE_TYPE_MAP.get(capability, [])
            agents[agent_id] = AgentCapability(
                agent_id=agent_id,
                name=entry.get("name", agent_id),
                description=entry.get("description", ""),
                capability=capability or "unknown",
                sandbox=sandbox,
                supported_node_types=supported,
                backend_id=entry.get("backend_id", "default"),
                ready=True,
            )

        self._agents = agents
        logger.info(
            "已加载 %d harness agents: %s",
            len(agents),
            ", ".join(a.name for a in agents.values()),
        )

    def get_agent(self, agent_id: str) -> AgentCapability | None:
        return self._agents.get(agent_id)

    def get_agents(self) -> list[AgentCapability]:
        return list(self._agents.values())

    def get_agents_by_capability(self, capability: str) -> list[AgentCapability]:
        return [a for a in self._agents.values() if a.capability == capability]

    def get_agents_for_node_type(self, node_type: str) -> list[AgentCapability]:
        """返回可以驱动指定节点类型的 agents。"""
        return [
            a for a in self._agents.values()
            if node_type in a.supported_node_types
        ]

    def get_supported_node_types(self, agent_id: str) -> list[str]:
        agent = self._agents.get(agent_id)
        if agent is None:
            return []
        return agent.supported_node_types

    def is_ready(self) -> bool:
        return len(self._agents) > 0

    def sync_with_general_harness(self, harness_url: str) -> bool:
        """尝试从通用 Harness 的 task-agent 目录同步能力数据。

        这是可选增强 — 即使通用 Harness 不可达，Creator Harness
        仍可基于 project.yaml 的静态数据正常工作。

        用任务 Token 访问 /api/v1/task-agents（仅需任务读取权限），避免
        /api/v1/agents 需要 agents:manage 权限导致的 403/401 噪音。
        """
        from ..harness_client import create_harness_client

        client = None
        try:
            client = create_harness_client(base_url=harness_url, timeout=5.0)
            records = client.task_agents()
            self._merge_task_agent_data(records)
            self._harness_connected = True
            logger.info("已从通用 Harness 同步 task-agent 数据")
            return True
        except Exception as exc:
            logger.warning("通用 Harness 不可达 (%s)，使用本地数据", exc)
        finally:
            if client is not None:
                client.close()

        self._harness_connected = False
        return False

    def is_harness_connected(self) -> bool:
        return self._harness_connected

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _merge_task_agent_data(self, records: Any) -> None:
        """用通用 Harness 的 task-agent 记录更新本地 agent 的 ready 状态与能力标签。

        records 格式为 ``[ { "id", "labels", "readiness", "accepts_tasks", ... } ]``。
        """
        if not isinstance(records, list):
            return
        for entry in records:
            if not isinstance(entry, dict):
                continue
            agent_id = str(entry.get("id", ""))
            local = self._agents.get(agent_id)
            if local is None:
                continue
            readiness = entry.get("readiness") or {}
            state = str(readiness.get("state", ""))
            local.ready = state == "ready" and bool(entry.get("accepts_tasks"))
            labels = entry.get("labels") or {}
            for key, value in labels.items():
                if key.endswith("/capability"):
                    capability = str(value)
                    mapped = CAPABILITY_NODE_TYPE_MAP.get(capability)
                    if mapped and capability != local.capability:
                        local.capability = capability
                        local.supported_node_types = sorted(set(local.supported_node_types) | set(mapped))