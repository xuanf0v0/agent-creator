from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .errors import CreatorHarnessError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/creator", tags=["creator"])


def _get_harness(request: Request) -> Any:
    """从 app.state 获取 CreatorHarness 实例。"""
    harness = getattr(request.app.state, "creator_harness", None)
    if harness is None:
        raise HTTPException(status_code=503, detail="Creator Harness 尚未初始化")
    return harness


@router.get("/status")
def creator_status(request: Request):
    """Creator Harness 状态。"""
    return _get_harness(request).status()


@router.get("/node-types")
def creator_node_types(request: Request):
    """返回所有节点类型目录（替代前端硬编码的 nodeCatalog）。"""
    harness = _get_harness(request)
    return {"node_types": harness.get_default_node_types(), "total": len(harness.get_node_types())}


@router.get("/agents")
def creator_agents(request: Request):
    """返回注册的 Harness Agent 及其能力映射。"""
    harness = _get_harness(request)
    return {"agents": harness.get_agent_capabilities(), "total": len(harness.get_agents())}


@router.get("/agents/{agent_id}")
def creator_agent(agent_id: str, request: Request):
    """查询单个 Agent 的详细信息。"""
    harness = _get_harness(request)
    agent = harness.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"找不到 Agent: {agent_id}")
    return dataclasses.asdict(agent)


@router.get("/node-types/{node_type}/agents")
def creator_node_type_agents(node_type: str, request: Request):
    """返回能驱动指定节点类型的候选 Agents。"""
    harness = _get_harness(request)
    return {
        "node_type": node_type,
        "agents": [dataclasses.asdict(a) for a in harness.get_agents_for_node_type(node_type)],
    }


@router.post("/reload")
def creator_reload(request: Request):
    """重新加载项目配置并刷新能力注册表。"""
    try:
        _get_harness(request).reload()
        return {"ok": True, "message": "Creator Harness 配置已刷新"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/parse-intent")
def creator_parse_intent(request: Request, body: dict[str, Any]):
    """解析用户意图（不执行操作）。"""
    message = body.get("message", "")
    workflow_id = body.get("workflow_id")
    history = body.get("history", [])
    try:
        return _get_harness(request).parse_intent(message, workflow_id, history)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/decide")
def creator_decide(request: Request, body: dict[str, Any]):
    """解析用户意图并执行相应的创作操作。

    核心端点：
    1. IntentParser 解析用户意图
    2. DecisionEngine 编排执行
    3. 返回结构化结果供前端使用
    """
    message = body.get("message", "")
    workflow_id = body.get("workflow_id")
    history = body.get("history", [])
    try:
        return _get_harness(request).decide(message, workflow_id, history)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, CreatorHarnessError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ------------------------------------------------------------------
# Layer 3: 生成器 API
# ------------------------------------------------------------------


@router.post("/generate")
def creator_generate(request: Request, body: dict[str, Any]):
    """统一生成入口 — 启动工作流生成任务。

    支持创建工作流和修改现有工作流，自动路由到 WorkflowGenerator。
    返回 generation_id 供前端轮询或 SSE 订阅事件流。
    """
    harness = _get_harness(request)
    generator = harness.workflow_generator
    if generator is None:
        raise HTTPException(status_code=503, detail="Workflow Generator 不可用")

    message = body.get("message", "")
    workflow_id = body.get("workflow_id")
    name = body.get("name")
    try:
        return generator.generate(message=message, workflow_id=workflow_id, name=name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, CreatorHarnessError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/generations/{generation_id}/cancel")
def creator_cancel_generation(generation_id: str, request: Request):
    """取消生成任务。"""
    harness = _get_harness(request)
    generator = harness.workflow_generator
    if generator is None:
        raise HTTPException(status_code=503, detail="Workflow Generator 不可用")
    try:
        return generator.cancel(generation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/generations/{generation_id}/events")
async def creator_generation_events(generation_id: str, request: Request):
    """SSE 事件流 — 订阅生成任务的实时事件。"""
    harness = _get_harness(request)
    generator = harness.workflow_generator
    if generator is None:
        raise HTTPException(status_code=503, detail="Workflow Generator 不可用")

    last_event_id = request.headers.get("Last-Event-ID")
    event_stream = generator.stream_events(generation_id, last_event_id)
    return StreamingResponse(
        event_stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/generations/{generation_id}")
def creator_get_generation(generation_id: str, request: Request):
    """查询单个生成任务的状态。"""
    harness = _get_harness(request)
    generator = harness.workflow_generator
    if generator is None:
        raise HTTPException(status_code=503, detail="Workflow Generator 不可用")
    gen = generator.get_generation(generation_id)
    if gen is None:
        raise HTTPException(status_code=404, detail=f"找不到生成任务: {generation_id}")
    return gen


@router.get("/workflows/{workflow_id}/generations")
def creator_list_generations(workflow_id: str, request: Request):
    """列出工作流的所有生成历史。"""
    harness = _get_harness(request)
    generator = harness.workflow_generator
    if generator is None:
        raise HTTPException(status_code=503, detail="Workflow Generator 不可用")
    return {"generations": generator.list_generations(workflow_id)}


@router.get("/workflows/{workflow_id}/chat-status")
def creator_chat_status(workflow_id: str, request: Request):
    """获取工作流的聊天状态（历史记录与活跃生成）。"""
    harness = _get_harness(request)
    generator = harness.workflow_generator
    if generator is None:
        raise HTTPException(status_code=503, detail="Workflow Generator 不可用")
    return generator.get_chat_status(workflow_id)


@router.get("/workflows/{workflow_id}/messages")
def creator_messages(workflow_id: str, request: Request):
    """获取工作流的对话历史。"""
    harness = _get_harness(request)
    generator = harness.workflow_generator
    if generator is None:
        raise HTTPException(status_code=503, detail="Workflow Generator 不可用")
    return generator.get_messages(workflow_id)