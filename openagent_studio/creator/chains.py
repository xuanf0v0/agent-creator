"""Creator Harness 命令链目录 — 借鉴 Maestro-Flow 的语义宏操作。

Maestro-Flow 把自然语言意图分类到 40+ 命令链（analyze → plan → execute → verify …），
在 decision 节点根据实际结果动态决策。本模块把同样的「语义宏操作」思想应用到
**工作流节点图**领域：把一个有语义的编辑意图（如「添加审批门」）表达为一个
「命令链」——一条由多个关联节点 + 连线组成的原子批次，一次应用、一次验证。

与 Maestro-Flow 的对应关系：
- 命令链 = 一组语义相关、必须一起生效的节点/连线批次（不再是「每轮一个节点」）
- 服务端仍做确定性校验与运行时探测兜底（对应 Maestro 的 decision 节点）
- 失败证据回填模型，驱动下一轮修复（对应 Maestro 的 debug → fix → retry 循环）

注意：本目录是**引导性**的（注入 prompt 供模型参照），不是硬编码的分发表。
模型既可以按链名组织一个批次，也可以自由组合节点；最终统一走 _apply_incremental_step
的批次校验。这样既放宽了「一次一个节点」的硬约束，又不引入新的不可控抽象。
"""

from __future__ import annotations

import json
from typing import Any

# 每条命令链：name（模型可引用的标识）、label（中文说明）、description（何时用）、
# node_types（涉及的节点类型）、example（一个 add_node 批次的 JSON 示例，nodes 数组 + edges）。
COMMAND_CHAINS: list[dict[str, Any]] = [
    {
        "name": "add_linear_chain",
        "label": "线性链路",
        "description": "按顺序新增多个节点并串成一条链（A→B→C）。用于需要连续处理步骤的场景。",
        "node_types": ["manual_trigger", "llm", "prompt", "transform", "output"],
        "example": {
            "action": "add_node",
            "nodes": [
                {"id": "parse", "type": "transform", "data": {"description": "解析输入"}},
                {"id": "summarize", "type": "llm", "data": {"description": "总结", "agent_id": "<agent>", "prompt": "请总结：{{latest}}"}},
            ],
            "edges": [
                {"source": "start", "target": "parse"},
                {"source": "parse", "target": "summarize"},
            ],
            "summary": "新增解析与总结两个连续节点",
        },
    },
    {
        "name": "add_approval_gate",
        "label": "人工审批门",
        "description": "新增 approval 节点 + condition 分支（true/false），并按审批结果决定后续走向。",
        "node_types": ["approval", "condition"],
        "example": {
            "action": "add_node",
            "nodes": [
                {"id": "approve", "type": "approval", "data": {"description": "人工审批"}},
                {"id": "gate", "type": "condition", "data": {"description": "审批分支", "expression": "latest.approved == true"}},
            ],
            "edges": [
                {"source": "upstream", "target": "approve"},
                {"source": "approve", "target": "gate"},
                {"source": "gate", "target": "downstream", "condition": "true"},
            ],
            "summary": "新增审批门与 true 分支，后续再补 false 分支",
        },
    },
    {
        "name": "add_conditional_branch",
        "label": "条件分支",
        "description": "新增 condition 节点并同时接好 true 与 false 两条分支节点。",
        "node_types": ["condition", "llm", "output", "prompt"],
        "example": {
            "action": "add_node",
            "nodes": [
                {"id": "check", "type": "condition", "data": {"description": "条件判断", "expression": "latest.score > 60"}},
                {"id": "pass", "type": "prompt", "data": {"description": "通过分支", "template": "通过：{{latest}}"}},
                {"id": "fail", "type": "prompt", "data": {"description": "失败分支", "template": "失败：{{latest}}"}},
            ],
            "edges": [
                {"source": "upstream", "target": "check"},
                {"source": "check", "target": "pass", "condition": "true"},
                {"source": "check", "target": "fail", "condition": "false"},
            ],
            "summary": "新增条件节点及 true/false 双分支",
        },
    },
    {
        "name": "add_validation_step",
        "label": "验证步骤",
        "description": "在输出前新增 validator 节点，校验上游结果满足业务规则或终态。",
        "node_types": ["validator", "output"],
        "example": {
            "action": "add_node",
            "nodes": [
                {"id": "validate", "type": "validator", "data": {"description": "结果校验", "agent_id": "<agent>", "prompt": "校验结果是否符合要求：{{latest}}"}},
            ],
            "edges": [
                {"source": "upstream", "target": "validate"},
                {"source": "validate", "target": "done"},
            ],
            "summary": "输出前新增校验节点",
        },
    },
    {
        "name": "add_parallel_fanout",
        "label": "并行扇出",
        "description": "新增 parallel 节点及其后的多个并行子任务节点。",
        "node_types": ["parallel", "llm", "agent"],
        "example": {
            "action": "add_node",
            "nodes": [
                {"id": "parallel", "type": "parallel", "data": {"description": "并行处理"}},
                {"id": "branch-a", "type": "llm", "data": {"description": "分支A", "agent_id": "<agent>", "prompt": "任务A：{{latest}}"}},
                {"id": "branch-b", "type": "llm", "data": {"description": "分支B", "agent_id": "<agent>", "prompt": "任务B：{{latest}}"}},
            ],
            "edges": [
                {"source": "upstream", "target": "parallel"},
                {"source": "parallel", "target": "branch-a"},
                {"source": "parallel", "target": "branch-b"},
            ],
            "summary": "新增并行扇出及两个并行分支",
        },
    },
    {
        "name": "rewire_output",
        "label": "重连输出",
        "description": "把某个上游的连线改接到新增的节点，再连回下游，用于插入中间处理。",
        "node_types": ["prompt", "transform", "output"],
        "example": {
            "action": "add_node",
            "nodes": [
                {"id": "enrich", "type": "transform", "data": {"description": "补充字段", "operation": "merge"}},
            ],
            "edges": [
                {"source": "upstream", "target": "enrich"},
                {"source": "enrich", "target": "downstream"},
            ],
            "summary": "在上下游之间插入补充节点",
        },
    },
]

# 单条命令链内部允许的最大节点数（防止模型把整张图塞进一个批次）
MAX_CHAIN_NODES = 12


def command_chain_catalog_text() -> str:
    """渲染命令链目录为 prompt 片段。"""
    lines = ["可用命令链（语义宏操作，一次可产出多个关联节点+连线）："]
    for chain in COMMAND_CHAINS:
        lines.append(
            f"- {chain['name']}（{chain['label']}）：{chain['description']} "
            f"涉及节点类型 {', '.join(chain['node_types'])}"
        )
    lines.append(
        "你可以按命令链组织一个批次（action=add_node，nodes 为数组，一次输出该链需要的全部节点和连线），"
        f"也可以自由组合；单批 nodes 不超过 {MAX_CHAIN_NODES} 个。"
    )
    return "\n".join(lines)


def chain_example(name: str) -> dict[str, Any] | None:
    """按命令链名返回示例批次（供诊断或 prompt 强化用）。"""
    for chain in COMMAND_CHAINS:
        if chain["name"] == name:
            return json.loads(json.dumps(chain["example"], ensure_ascii=False))
    return None
