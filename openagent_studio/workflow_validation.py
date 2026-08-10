from __future__ import annotations

from .models import ProjectSpec


def workflow_delete_blockers(project: ProjectSpec, workflow_id: str) -> list[str]:
    blockers = [
        f"飞书集成 {item.id} 正在引用该工作流"
        for item in project.integrations.feishu
        if item.workflow_id == workflow_id
    ]
    blockers.extend(
        f"QQ 集成 {item.id} 正在引用该工作流"
        for item in project.integrations.qq
        if item.workflow_id == workflow_id
    )
    blockers.extend(
        f"工作流 {workflow.id} 的子工作流节点 {node.id} 正在引用该工作流"
        for workflow in project.workflows
        if workflow.id != workflow_id
        for node in workflow.nodes
        if node.type == "subworkflow" and node.data.get("workflow_id") == workflow_id
    )
    return blockers
