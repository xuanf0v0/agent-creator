from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid
from typing import Any

from .models import WorkflowSpec
from .store import SpecStore


SYSTEM_PROMPT = """你是 OpenAgent Studio 的工作流生成核心。你的唯一职责是把用户需求转换为智能体工作流。
不要修改文件、不要运行命令、不要调用外部工具。请用中文简短说明你的设计，并逐个输出操作。
每个操作必须独占一行，格式为 <op>{JSON}</op>。支持：
add_node: {"action":"add_node","id":"英文小写编号","type":"agent|prompt|condition|parallel|loop|approval|validator|output","description":"中文说明","agent_id":"可选"}
update_node: {"action":"update_node","id":"节点编号","description":"中文说明","agent_id":"可选"}
delete_node: {"action":"delete_node","id":"节点编号"}
connect_nodes: {"action":"connect_nodes","source":"来源节点","target":"目标节点"}
disconnect_nodes: {"action":"disconnect_nodes","source":"来源节点","target":"目标节点"}
完成时必须输出 <op>{"action":"finalize_workflow"}</op>。不要把多个操作放进一个 JSON 数组。
优先增量修改已有工作流；不要删除与需求无关的节点。节点编号只能使用小写字母、数字和短横线。
"""


@dataclass
class Generation:
    id: str
    workflow_id: str
    base_etag: str
    draft: dict[str, Any]
    prompt: str
    events: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)
    event_queue: queue.Queue = field(default_factory=queue.Queue)
    process: subprocess.Popen | None = None
    cancelled: bool = False
    completed: bool = False
    session_id: str | None = None
    operation_ids: set[str] = field(default_factory=set)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        item = {"event": event, "data": data, "sequence": len(self.events), "timestamp": time.time()}
        self.events.append(item)
        self.event_queue.put(item)


class GeneratorManager:
    def __init__(self, store: SpecStore):
        self.store = store
        self.generations: dict[str, Generation] = {}
        self.active: dict[str, str] = {}
        self.sessions: dict[str, str] = {}
        self.history: dict[str, list[dict[str, str]]] = {}
        self._lock = threading.RLock()

    def start(self, workflow_id: str, message: str) -> Generation:
        message = message.strip()
        if not message:
            raise ValueError("请输入你想创建的智能体或工作流需求")
        with self._lock:
            active_id = self.active.get(workflow_id)
            if active_id and not self.generations[active_id].completed:
                raise RuntimeError("这个工作流正在生成，请先等待或停止当前生成")
            spec = self.store.load()
            workflow = next((item for item in spec.workflows if item.id == workflow_id), None)
            if workflow is None:
                raise KeyError(workflow_id)
            generation = Generation(
                id=uuid.uuid4().hex,
                workflow_id=workflow_id,
                base_etag=self.store.etag(),
                draft=workflow.model_dump(mode="json"),
                prompt=message,
                messages=[{"role": "user", "content": message}],
                session_id=self.sessions.get(workflow_id),
            )
            self.generations[generation.id] = generation
            self.active[workflow_id] = generation.id
            self.history.setdefault(workflow_id, []).append({"role": "user", "content": message})
        threading.Thread(target=self._run, args=(generation, spec), daemon=True).start()
        return generation

    def cancel(self, generation_id: str) -> Generation:
        generation = self.require(generation_id)
        generation.cancelled = True
        if generation.process and generation.process.poll() is None:
            generation.process.terminate()
        generation.completed = True
        generation.emit("generation.cancelled", {"generation_id": generation.id})
        return generation

    def require(self, generation_id: str) -> Generation:
        generation = self.generations.get(generation_id)
        if generation is None:
            raise KeyError(generation_id)
        return generation

    def _run(self, generation: Generation, spec: Any) -> None:
        generation.emit("generation.started", {"generation_id": generation.id, "workflow_id": generation.workflow_id})
        model = os.environ.get("OPENCODE_GENERATOR_MODEL") or next((agent.model for agent in spec.agents if agent.model), None)
        prompt = f"{SYSTEM_PROMPT}\n当前工作流：\n{json.dumps(generation.draft, ensure_ascii=False)}\n\n用户需求：{generation.prompt}"
        command = [os.environ.get("OPENCODE_BIN", "opencode"), "run", "--format", "json", "--agent", "plan", "--title", f"OpenAgent生成-{generation.workflow_id}"]
        if model:
            command += ["--model", model]
        if generation.session_id:
            command += ["--session", generation.session_id]
        command.append(prompt)
        workdir = Path(spec.project_dir).expanduser()
        if not workdir.is_dir():
            workdir = self.store.path.parent
        assistant_text = ""
        parsed_until = 0
        try:
            generation.process = subprocess.Popen(
                command, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, env=os.environ.copy(),
            )
            assert generation.process.stdout is not None
            for raw in generation.process.stdout:
                if generation.cancelled:
                    return
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                generation.session_id = generation.session_id or _find_string(item, "sessionID")
                text = _extract_text(item)
                if not text:
                    continue
                if text.startswith(assistant_text):
                    delta = text[len(assistant_text):]
                    assistant_text = text
                else:
                    delta = text
                    assistant_text += text
                visible = re.sub(r"<op>.*?</op>", "", delta, flags=re.DOTALL)
                if visible.strip():
                    generation.emit("chat.assistant.delta", {"text": visible})
                parse_base = parsed_until
                for match in list(re.finditer(r"<op>\s*(\{.*?\})\s*</op>", assistant_text[parse_base:], re.DOTALL)):
                    parsed_until = parse_base + match.end()
                    try:
                        operation = json.loads(match.group(1))
                        self._apply(generation, operation)
                    except (ValueError, KeyError, json.JSONDecodeError) as exc:
                        generation.emit("operation.rejected", {"message": str(exc)})
            code = generation.process.wait()
            if generation.cancelled:
                return
            if code != 0:
                raise RuntimeError(f"OpenCode 退出，代码 {code}")
            if not generation.completed:
                self._finalize(generation)
            clean_text = re.sub(r"<op>.*?</op>", "", assistant_text, flags=re.DOTALL).strip()
            if clean_text:
                self.history[generation.workflow_id].append({"role": "assistant", "content": clean_text})
            if generation.session_id:
                self.sessions[generation.workflow_id] = generation.session_id
        except Exception as exc:
            generation.completed = True
            generation.emit("generation.failed", {"message": str(exc)})

    def _apply(self, generation: Generation, operation: dict[str, Any]) -> None:
        action = operation.get("action")
        op_id = operation.get("operation_id") or json.dumps(operation, ensure_ascii=False, sort_keys=True)
        if op_id in generation.operation_ids:
            return
        generation.operation_ids.add(op_id)
        nodes = generation.draft["nodes"]
        edges = generation.draft["edges"]
        node_ids = {item["id"] for item in nodes}
        if action == "add_node":
            node_id = operation["id"]
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", node_id):
                raise ValueError(f"节点编号不合法：{node_id}")
            if node_id in node_ids:
                raise ValueError(f"节点已存在：{node_id}")
            if len(nodes) >= 50:
                raise ValueError("节点数量不能超过 50")
            kind = operation["type"]
            if kind not in {"agent", "prompt", "condition", "parallel", "loop", "approval", "validator", "output"}:
                raise ValueError(f"不支持的节点类型：{kind}")
            index = len(nodes)
            data = {"description": operation.get("description", node_id)}
            if operation.get("agent_id"):
                data["agent_id"] = operation["agent_id"]
            node = {"id": node_id, "type": kind, "data": data, "position": {"x": 80 + (index % 3) * 260, "y": 80 + (index // 3) * 150}}
            nodes.append(node)
            generation.emit("workflow.node.added", {"node": node})
        elif action == "update_node":
            node = next((item for item in nodes if item["id"] == operation["id"]), None)
            if node is None:
                raise ValueError(f"找不到节点：{operation['id']}")
            if "description" in operation:
                node["data"]["description"] = operation["description"]
            if "agent_id" in operation:
                node["data"]["agent_id"] = operation["agent_id"]
            generation.emit("workflow.node.updated", {"node": node})
        elif action == "delete_node":
            node_id = operation["id"]
            generation.draft["nodes"] = [item for item in nodes if item["id"] != node_id]
            generation.draft["edges"] = [item for item in edges if item["source"] != node_id and item["target"] != node_id]
            generation.emit("workflow.node.deleted", {"node_id": node_id})
        elif action == "connect_nodes":
            source, target = operation["source"], operation["target"]
            if source == target or source not in node_ids or target not in node_ids:
                raise ValueError(f"无法连接节点：{source} → {target}")
            if len(edges) >= 100:
                raise ValueError("连线数量不能超过 100")
            edge = {"source": source, "target": target}
            if edge not in edges:
                edges.append(edge)
                generation.emit("workflow.edge.added", {"edge": edge})
        elif action == "disconnect_nodes":
            source, target = operation["source"], operation["target"]
            generation.draft["edges"] = [item for item in edges if not (item["source"] == source and item["target"] == target)]
            generation.emit("workflow.edge.deleted", {"source": source, "target": target})
        elif action == "finalize_workflow":
            self._finalize(generation)
        else:
            raise ValueError(f"未知操作：{action}")

    def _finalize(self, generation: Generation) -> None:
        if generation.completed:
            return
        workflow = WorkflowSpec.model_validate(generation.draft)
        current = self.store.load()
        if self.store.etag() != generation.base_etag:
            generation.completed = True
            generation.emit("generation.conflict", {"message": "工作流在生成期间被修改，请重新发送需求"})
            return
        current.workflows = [workflow if item.id == workflow.id else item for item in current.workflows]
        etag = self.store.save(current, generation.base_etag)
        generation.completed = True
        generation.emit("generation.completed", {"workflow": workflow.model_dump(mode="json"), "etag": etag})


def _find_string(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get(key), str):
            return value[key]
        for child in value.values():
            found = _find_string(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_string(child, key)
            if found:
                return found
    return None


def _extract_text(item: dict[str, Any]) -> str:
    part = item.get("part") or item.get("properties", {}).get("part")
    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
        return part["text"]
    if item.get("type") == "text" and isinstance(item.get("text"), str):
        return item["text"]
    return ""
