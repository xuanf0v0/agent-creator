from __future__ import annotations

import json
import logging
import time
import threading
from typing import Any, Generator

from .errors import GenerationError
from .models import IntentResult, IntentType, slugify_workflow_id

logger = logging.getLogger(__name__)

# 事件类型常量
EVENT_STARTED = "generation.started"
EVENT_STEP = "generation.step"
EVENT_NODE_CREATED = "generation.node_created"
EVENT_NODE_UPDATED = "generation.node_updated"
EVENT_NODE_DELETED = "generation.node_deleted"
EVENT_EDGE_CREATED = "generation.edge_created"
EVENT_PROBE_RUNNING = "generation.probe_running"
EVENT_PROBE_RESULT = "generation.probe_result"
EVENT_STALLED = "generation.stalled"
EVENT_COMPLETED = "generation.completed"
EVENT_FAILED = "generation.failed"
EVENT_CANCELLED = "generation.cancelled"
EVENT_CHAT_REPLY = "generation.chat_reply"


class WorkflowGenerator:
    """Creator Harness 工作流生成器。

    职责：
    - 包装现有的 GeneratorManager，提供结构化生成接口
    - 根据 IntentParser/DecisionEngine 的意图结果自动路由
    - 提供结构化进度追踪和事件流
    - 支持增量构建、探测验证和失败恢复

    与现有 GeneratorManager 的关系：
    - GeneratorManager 是底层执行引擎（调用 OpenCode CLI）
    - WorkflowGenerator 是上层结构化接口（意图感知、进度追踪）
    """

    def __init__(
        self,
        store: Any,
        generator_manager: Any,
        registry: Any | None = None,
    ) -> None:
        self._store = store
        self._generator = generator_manager
        self._registry = registry
        self._generations: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def generate(
        self,
        message: str,
        workflow_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """根据意图路由到对应的生成方法。

        这是外部调用的统一入口。DecisionEngine 已解析好的意图
        通过本方法执行实际的生成操作。
        """
        spec = self._store.load()
        generation: Any = None

        # 创建新工作流
        if not workflow_id:
            logger.info("开始直出创建工作流: %s", message[:60])
            generation = self._generator.create_direct(
                message=message,
                workflow_id=slugify_workflow_id(name or message),
                name=name,
            )
            self._emit_system_event(generation, EVENT_STARTED, {
                "intent": "create_workflow",
                "message": message[:200],
                "workflow_id": generation.workflow_id,
            })
        # 修改/修复/优化现有工作流
        else:
            # 检查是否已有进行中的生成
            existing = self._find_running_generation(workflow_id)
            if existing:
                # 已有进行中的生成 → resume
                logger.info("继续已有生成: %s -> %s", existing.id, message[:60])
                generation = self._generator.resume(existing.id, message)
            else:
                # 新生成 → start
                logger.info("开始修改工作流 %s: %s", workflow_id, message[:60])
                generation = self._generator.start(workflow_id, message)

            self._emit_system_event(generation, EVENT_STARTED, {
                "intent": "modify_workflow",
                "message": message[:200],
                "workflow_id": workflow_id,
            })

        self._track_generation(generation)
        return {
            "generation_id": generation.id,
            "workflow_id": generation.workflow_id,
            "status": self._generation_status(generation),
        }

    def optimize(
        self,
        workflow_id: str,
    ) -> dict[str, Any]:
        """优化工作流。"""
        generation = self._generator.optimize(workflow_id)
        self._emit_system_event(generation, EVENT_STARTED, {
            "intent": "optimize_workflow",
            "workflow_id": workflow_id,
        })
        self._track_generation(generation)
        return {
            "generation_id": generation.id,
            "workflow_id": workflow_id,
            "status": self._generation_status(generation),
        }

    def cancel(self, generation_id: str) -> dict[str, Any]:
        """取消生成。"""
        generation = self._generator.require(generation_id)
        if generation.cancelled:
            return {"generation_id": generation_id, "cancelled": True}
        self._generator.cancel(generation_id)
        self._emit_system_event(generation, EVENT_CANCELLED, {})
        return {"generation_id": generation_id, "cancelled": True}

    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        """获取生成状态信息。"""
        try:
            gen = self._generator.require(generation_id)
            return self._generation_to_dict(gen)
        except KeyError:
            return None

    def list_generations(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        """列出所有生成记录。"""
        results: list[dict[str, Any]] = []
        for gen_id, gen in self._generations.items():
            if workflow_id and gen.workflow_id != workflow_id:
                continue
            results.append(self._generation_to_dict(gen))
        return sorted(results, key=lambda x: x.get("created_at", 0), reverse=True)

    # ------------------------------------------------------------------
    # 事件流
    # ------------------------------------------------------------------

    def stream_events(
        self,
        generation_id: str,
        last_event_id: str | None = None,
    ) -> Generator[str, None, None]:
        """生成结构化 SSE 事件流。

        包装现有的 GeneratorManager 事件流，添加结构化字段。
        """
        try:
            generation = self._generator.require(generation_id)
        except KeyError:
            # 生成已过期，返回终止事件
            data = json.dumps({
                "generation_id": generation_id,
                "reason": "expired",
                "message": "生成任务已失效",
            }, ensure_ascii=False)
            yield f"event: generation.failed\ndata: {data}\n\n"
            return

        cursor = None
        if last_event_id is not None:
            try:
                cursor = int(last_event_id) + 1
            except ValueError:
                cursor = None

        while True:
            reset_info = None
            with generation.event_signal:
                first_seq = (
                    generation.events[0]["sequence"]
                    if generation.events
                    else generation.next_event_sequence
                )
                if cursor is None:
                    cursor = first_seq
                elif cursor < first_seq:
                    reset_info = {"reason": "events_expired", "next_sequence": first_seq}
                    cursor = first_seq

                events = [e for e in generation.events if e["sequence"] >= cursor]

                if not events and not generation.completed:
                    generation.event_signal.wait(timeout=15)
                    first_seq = (
                        generation.events[0]["sequence"]
                        if generation.events
                        else generation.next_event_sequence
                    )
                    if cursor < first_seq:
                        reset_info = {"reason": "events_expired", "next_sequence": first_seq}
                        cursor = first_seq
                    events = [e for e in generation.events if e["sequence"] >= cursor]
                else:
                    pass  # 有事件或已完成，直接处理

                if events:
                    cursor = events[-1]["sequence"] + 1
                completed = generation.completed and cursor >= generation.next_event_sequence

            if reset_info:
                yield f"event: stream.reset\ndata: {json.dumps(reset_info, ensure_ascii=False)}\n\n"
            for item in events:
                yield f"id: {item['sequence']}\nevent: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
            if completed:
                return
            if not events:
                yield ": heartbeat\n\n"

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _track_generation(self, generation: Any) -> None:
        """在本地缓存中追踪生成状态。"""
        self._generations[generation.id] = generation
        # 清理过多历史
        if len(self._generations) > 200:
            stale = sorted(
                self._generations.keys(),
                key=lambda gid: self._generations[gid].events[-1]["timestamp"]
                if self._generations[gid].events
                else 0,
            )[:50]
            for gid in stale:
                self._generations.pop(gid, None)

    def _find_running_generation(self, workflow_id: str) -> Any | None:
        """查找指定工作流是否有进行中的生成。"""
        for gen in self._generations.values():
            if gen.workflow_id == workflow_id and (not gen.completed or getattr(gen, "awaiting_input", False)) and not gen.cancelled:
                return gen
        return None

    def _generation_status(self, generation: Any) -> str:
        """获取生成状态字符串。"""
        if generation.cancelled:
            return "cancelled"
        if getattr(generation, "awaiting_input", False):
            return "waiting_input"
        if generation.completed:
            return "completed"
        if generation.stalled:
            return "stalled"
        if generation.process and generation.process.poll() is not None:
            code = generation.process.poll()
            return "failed" if code != 0 else "completed"
        return "running"

    def _generation_to_dict(self, generation: Any) -> dict[str, Any]:
        """将 Generation 对象转为字典。"""
        return {
            "id": generation.id,
            "workflow_id": generation.workflow_id,
            "status": self._generation_status(generation),
            "stalled": generation.stalled,
            "awaiting_input": getattr(generation, "awaiting_input", False),
            "question": getattr(generation, "question", None),
            "cancelled": generation.cancelled,
            "completed": generation.completed,
            "mode": generation.mode,
            "events_count": len(generation.events),
            "messages_count": len(generation.messages),
            "last_event": generation.events[-1] if generation.events else None,
            "last_failure": generation.last_failure,
        }

    def _emit_system_event(
        self,
        generation: Any,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """向生成记录中注入系统事件。"""
        generation.emit(event, data)

    # ------------------------------------------------------------------
    # 对话历史
    # ------------------------------------------------------------------

    def get_messages(self, workflow_id: str) -> list[dict[str, Any]]:
        """获取工作流的对话历史。"""
        return self._generator.history.get(workflow_id, [])

    def get_chat_status(self, workflow_id: str) -> dict[str, Any]:
        """获取工作流的聊天状态（用于是否需要显示聊天面板）。"""
        generations = self.list_generations(workflow_id)
        active = [g for g in generations if g["status"] in {"running", "waiting_input"}]
        return {
            "has_history": len(self.get_messages(workflow_id)) > 0,
            "active_generation": active[0] if active else None,
        }


# 快捷创建函数
def create_workflow_generator(
    store: Any,
    generator_manager: Any,
    registry: Any | None = None,
) -> WorkflowGenerator:
    return WorkflowGenerator(store=store, generator_manager=generator_manager, registry=registry)
