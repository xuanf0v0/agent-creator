from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# 与 models.py 中 WorkflowSpec.id 保持一致的校验模式
WORKFLOW_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify_workflow_id(source: str, fallback: str = "workflow") -> str:
    """将名称或消息转换为合法的 kebab-case 工作流 ID。

    规则（与 ProjectSpec 中 WorkflowSpec.id 一致）：
    - 仅允许小写字母、数字、短横线
    - 不能以短横线开头或结尾，不能连续短横线
    - 中文等其他字符被剥离；若结果为空则使用 fallback
    - 始终追加短唯一后缀，避免碰撞
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (source or "").lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug[:40].rstrip("-")
    if not slug:
        slug = fallback
    suffix = uuid.uuid4().hex[:6]
    return f"{slug}-{suffix}"


class IntentType(str, Enum):
    """用户意图分类 — Creator Harness 决策引擎的输入。"""

    CREATE_WORKFLOW = "create_workflow"
    MODIFY_WORKFLOW = "modify_workflow"
    REPAIR_WORKFLOW = "repair_workflow"
    OPTIMIZE_WORKFLOW = "optimize_workflow"
    CHAT_REPLY = "chat_reply"  # 闲聊，无需变更工作流
    UNKNOWN = "unknown"


@dataclass
class IntentResult:
    """意图解析结果。"""

    intent: IntentType
    confidence: float  # 0.0 ~ 1.0
    workflow_id: str | None = None  # 目标工作流 ID
    reasoning: str = ""
    raw_message: str = ""


class CreatorDecision(str, Enum):
    """Creator Harness 决策 — 每一步要执行的动作。"""

    ADD_NODE = "add_node"
    UPDATE_NODE = "update_node"
    DELETE_NODE = "delete_node"
    CONNECT_NODES = "connect_nodes"
    DISCONNECT_NODES = "disconnect_nodes"
    COMPLETE = "complete"  # 工作流创建完成
    REPLY = "reply"  # 回复用户，无需变更
    REQUEST_CLARIFY = "request_clarify"  # 请求用户澄清
    ROLLBACK = "rollback"  # 回滚上一步


@dataclass
class CreatorDecision:
    """决策引擎输出。"""

    action: CreatorDecision
    node_type: str | None = None
    node_data: dict[str, Any] = field(default_factory=dict)
    source_node_id: str | None = None
    target_node_id: str | None = None
    reasoning: str = ""
    reply_text: str = ""


@dataclass
class NodeTypeInfo:
    """节点类型描述 — 供前端动态渲染目录。"""

    type: str  # NodeKind
    label: str
    category: str
    icon: str
    description: str
    requires_agent: bool = False
    default_data: dict[str, Any] = field(default_factory=dict)
    color: str = ""


@dataclass
class AgentCapability:
    """Harness Agent 能力描述 — 供前端选择 agent 时参考。"""

    agent_id: str
    name: str
    description: str
    capability: str  # text-generation | repository-analysis | test-execution
    sandbox: str  # read-only | read-write
    supported_node_types: list[str] = field(default_factory=list)
    backend_id: str = "default"
    ready: bool = True


@dataclass
class CreatorState:
    """Creator Harness 运行时状态。"""

    generation_id: str
    workflow_id: str
    status: str = "idle"  # idle | running | paused | stalled | completed | failed
    step: int = 0
    max_steps: int = 30
    created_node_ids: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    fingerprint_history: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)