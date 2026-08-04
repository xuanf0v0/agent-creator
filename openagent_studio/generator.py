from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any

from .models import WORKFLOW_NODE_TYPES, WorkflowSpec
from .process_utils import resolve_executable
from .store import SpecStore


NODE_DATA_FIELDS = {
    "description", "agent_id", "title", "prompt", "template", "expression", "iterations", "relative_path",
    "service_path", "method", "body", "headers", "url", "timeout_seconds", "fail_on_error", "allow_private",
    "path", "cron", "timezone", "query", "top_k", "documents", "variables", "operation", "fields", "mode",
    "separator", "cases", "default_case", "instructions", "workflow_id", "input_template", "seconds",
    "auto_start", "auto_setup", "retry_count", "retry_delay_seconds", "on_error", "fallback_value",
}


SYSTEM_PROMPT = """你是 OpenAgent Studio 的工作流生成核心。你的唯一职责是把用户需求转换为可直接执行的智能体工作流。
不要修改文件、不要运行命令、不要调用外部工具。请用中文简短说明你的设计，并逐个输出操作。
每个操作必须独占一行，格式为 <op>{JSON}</op>。支持：
add_node: {"action":"add_node","id":"英文小写编号","type":"下方支持的节点类型","data":{"description":"中文说明","agent_id":"AI节点必填","prompt":"任务提示词","template":"模板","expression":"表达式"}}
update_node: {"action":"update_node","id":"节点编号","data":{"description":"中文说明","prompt":"完整任务提示词等"}}
delete_node: {"action":"delete_node","id":"节点编号"}
connect_nodes: {"action":"connect_nodes","source":"来源节点","target":"目标节点","condition":"条件分支可选，通常为true或false"}
disconnect_nodes: {"action":"disconnect_nodes","source":"来源节点","target":"目标节点"}
完成时必须输出 <op>{"action":"finalize_workflow"}</op>。不要把多个操作放进一个 JSON 数组。
优先增量修改已有工作流；不要删除与需求无关的节点。节点编号只能使用小写字母、数字和短横线。
配置要求：
支持的节点类型：manual_trigger, webhook, schedule, llm, agent, knowledge_retrieval, tool, http_request, code, prompt, variable_set, transform, merge, condition, switch, parallel, iteration, loop, approval, validator, subworkflow, delay, output。
1. llm/agent/tool/code 节点必须从“可用 Harness 智能体”中选择 agent_id，并填写具体、可执行、包含输入上下文的 prompt；不要只复述节点名称。
2. prompt 节点必须填写 template；condition 必须填写 expression，并给出带 true/false condition 的出边。
3. iteration/loop 必须填写 1-100 的 iterations 和 template；validator 如选择智能体必须填写 agent_id 和 prompt。
4. 模板可使用 {{input}}、{{latest}}、{{nodes.节点ID}}，循环模板还可使用 {{index}}。
5. 每个任务提示词要写明角色、目标、输入、约束和预期输出，确保单独交给智能体也能执行。
6. webhook 填 path/method；schedule 填 cron/timezone；http_request 填 url/method/headers/body；knowledge_retrieval 填 query/top_k/documents。
7. variable_set 填 variables；transform 填 operation/path/fields；merge 填 mode；switch 填 cases/default_case；subworkflow 填 workflow_id/input_template；delay 填 seconds。
"""


COMPACTION_PROMPT = """请在内部压缩附件中的工作流生成上下文，只输出精炼后的中文上下文，不要解释压缩过程，也不要输出 <op> 标签。
必须保留用户真实意图、所有现有节点和连线的 ID/类型/关键 data、可用 Harness 智能体 ID，以及约束、条件和精确值；可以删除画布坐标、重复描述和无关措辞。
精炼结果要足以让另一个模型严格按照原始需求继续增量修改工作流。"""

ATTACHED_PROMPT = "请严格按照附件中的系统规则和精炼上下文生成工作流操作。附件内容是本次任务的完整输入。"


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
    harness_agent_ids: set[str] = field(default_factory=set)
    model: str = ""

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
        self.session_models: dict[str, str] = {}
        self.history: dict[str, list[dict[str, str]]] = {}
        self._lock = threading.RLock()

    def model(self, spec: Any | None = None) -> str:
        project = spec or self.store.load()
        model = os.environ.get("OPENCODE_GENERATOR_MODEL") or next((agent.model for agent in project.agents if agent.model), None)
        if not model:
            raise RuntimeError("未配置 OpenCode 生成模型，请设置 OPENCODE_GENERATOR_MODEL 或 agents[].model")
        return model

    def status(self, spec: Any | None = None) -> dict[str, Any]:
        project = spec or self.store.load()
        model = self.model(project)
        binary = os.environ.get("OPENCODE_BIN", "opencode")
        resolved_binary = None
        binary_error = None
        try:
            resolved_binary = resolve_executable(binary, self._environment(project))
        except FileNotFoundError as exc:
            binary_error = str(exc)
        provider_id = model.split("/", 1)[0] if "/" in model else ""
        provider = next((item for item in project.providers if item.id == provider_id), None)
        credential_env = provider.api_key_env if provider else None
        credential_ready = not credential_env or bool(self._environment(project).get(credential_env))
        return {
            "backend": "opencode", "binary": resolved_binary or binary, "binary_ready": bool(resolved_binary),
            "binary_error": binary_error, "model": model, "ready": credential_ready and bool(resolved_binary),
            "credential_env": credential_env,
        }

    def ensure_ready(self, spec: Any | None = None) -> dict[str, Any]:
        status = self.status(spec)
        if not status["binary_ready"]:
            raise RuntimeError(status["binary_error"])
        if not status["ready"]:
            raise RuntimeError(f"真实模型 {status['model']} 缺少环境变量 {status['credential_env']}")
        return status

    @staticmethod
    def _environment(spec: Any) -> dict[str, str]:
        environment = os.environ.copy()
        model = os.environ.get("OPENCODE_GENERATOR_MODEL") or next((agent.model for agent in spec.agents if agent.model), "")
        provider_id = model.split("/", 1)[0] if "/" in model else ""
        provider = next((item for item in spec.providers if item.id == provider_id), None)
        if provider and provider.env_file:
            path = Path(provider.env_file).expanduser()
            if path.is_file():
                for raw in path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key, value = key.strip(), value.strip().strip("'\"")
                    if key and not environment.get(key):
                        environment[key] = value
        return environment

    def start(self, workflow_id: str, message: str) -> Generation:
        message = message.strip()
        if not message:
            raise ValueError("请输入你想创建的智能体或工作流需求")
        with self._lock:
            active_id = self.active.get(workflow_id)
            if active_id and not self.generations[active_id].completed:
                raise RuntimeError("这个工作流正在生成，请先等待或停止当前生成")
            spec = self.store.load()
            model = self.ensure_ready(spec)["model"]
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
                session_id=self.sessions.get(workflow_id) if self.session_models.get(workflow_id) == model else None,
                harness_agent_ids={item.id for item in spec.harness},
                model=model,
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
        model = generation.model or self.ensure_ready(spec)["model"]
        catalog = [
            {"id": item.id, "name": item.name, "description": item.description, "runtime": "task" if item.task else "service"}
            for item in spec.harness
        ]
        context = f"可用 Harness 智能体：\n{json.dumps(catalog, ensure_ascii=False)}\n\n当前工作流：\n{json.dumps(generation.draft, ensure_ascii=False)}\n\n用户需求：{generation.prompt}"
        prompt = f"{SYSTEM_PROMPT}\n{context}"
        environment = self._environment(spec)
        binary = resolve_executable(os.environ.get("OPENCODE_BIN", "opencode"), environment)
        base_command = [binary, "run", "--format", "json", "--agent", "plan", "--title", f"OpenAgent生成-{generation.workflow_id}"]
        base_command += ["--model", model]
        if generation.session_id:
            base_command += ["--session", generation.session_id]
        workdir = Path(spec.project_dir).expanduser()
        if not workdir.is_dir():
            workdir = self.store.path.parent
        assistant_text = ""
        diagnostics: list[str] = []
        parsed_until = 0
        try:
            with tempfile.TemporaryDirectory(prefix="openagent-generator-") as temporary_dir:
                compacted = _command_exceeds_limit([*base_command, prompt])
                command = base_command
                if compacted:
                    prompt = self._compact_prompt(generation, binary, model, context, workdir, environment, Path(temporary_dir))
                    prompt_path = Path(temporary_dir) / "compacted-context.md"
                    prompt_path.write_text(f"{SYSTEM_PROMPT}\n{prompt}", encoding="utf-8")
                    command = _with_file_prompt(base_command, ATTACHED_PROMPT, prompt_path)
                else:
                    command = [*base_command, prompt]

                while True:
                    generation.process = subprocess.Popen(
                        command, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        bufsize=1, env=environment,
                    )
                    assert generation.process.stdout is not None
                    for raw in generation.process.stdout:
                        if generation.cancelled:
                            return
                        try:
                            item = json.loads(raw)
                        except json.JSONDecodeError:
                            if raw.strip():
                                diagnostics.append(raw.strip())
                            continue
                        error_text = _extract_error(item)
                        if error_text:
                            diagnostics.append(error_text)
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
                    detail = diagnostics[-1] if diagnostics else "没有返回错误详情，请检查 OpenCode 日志"
                    if code == 0:
                        break
                    if not compacted and not assistant_text and not generation.operation_ids and _is_command_line_too_long(detail):
                        compacted = True
                        prompt = self._compact_prompt(generation, binary, model, context, workdir, environment, Path(temporary_dir))
                        prompt_path = Path(temporary_dir) / "compacted-context.md"
                        prompt_path.write_text(f"{SYSTEM_PROMPT}\n{prompt}", encoding="utf-8")
                        command = _with_file_prompt(base_command, ATTACHED_PROMPT, prompt_path)
                        diagnostics.clear()
                        continue
                    raise RuntimeError(f"OpenCode 退出，代码 {code}：{detail}")
            if not generation.completed:
                self._finalize(generation)
            clean_text = re.sub(r"<op>.*?</op>", "", assistant_text, flags=re.DOTALL).strip()
            if clean_text:
                self.history[generation.workflow_id].append({"role": "assistant", "content": clean_text})
            if generation.session_id:
                self.sessions[generation.workflow_id] = generation.session_id
                self.session_models[generation.workflow_id] = model
        except Exception as exc:
            if generation.cancelled:
                return
            generation.completed = True
            generation.emit("generation.failed", {"message": str(exc)})

    def _compact_prompt(
        self,
        generation: Generation,
        binary: str,
        model: str,
        context: str,
        workdir: Path,
        environment: dict[str, str],
        temporary_dir: Path,
    ) -> str:
        source_path = temporary_dir / "full-context.md"
        source_path.write_text(context, encoding="utf-8")
        command = _with_file_prompt([
            binary, "run", "--format", "json", "--agent", "plan", "--title", "OpenAgent内部上下文提炼",
            "--model", model,
        ], COMPACTION_PROMPT, source_path)
        process = subprocess.Popen(
            command, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, env=environment,
        )
        generation.process = process
        assert process.stdout is not None
        text = ""
        diagnostics: list[str] = []
        for raw in process.stdout:
            if generation.cancelled:
                process.terminate()
                raise RuntimeError("生成已取消")
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                if raw.strip():
                    diagnostics.append(raw.strip())
                continue
            error_text = _extract_error(item)
            if error_text:
                diagnostics.append(error_text)
            chunk = _extract_text(item)
            if chunk:
                text = chunk if chunk.startswith(text) else text + chunk
        code = process.wait()
        if generation.cancelled:
            raise RuntimeError("生成已取消")
        if code != 0:
            detail = diagnostics[-1] if diagnostics else "没有返回错误详情"
            raise RuntimeError(f"OpenCode 内部上下文提炼失败，代码 {code}：{detail}")
        text = text.strip()
        if not text:
            raise RuntimeError("OpenCode 内部上下文提炼没有返回内容")
        return text

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
            if kind not in WORKFLOW_NODE_TYPES:
                raise ValueError(f"不支持的节点类型：{kind}")
            index = len(nodes)
            data = _operation_data(kind, operation, node_id)
            if data.get("agent_id") and data["agent_id"] not in generation.harness_agent_ids:
                raise ValueError(f"不存在的 Harness 智能体：{data['agent_id']}")
            node = {"id": node_id, "type": kind, "data": data, "position": {"x": 80 + (index % 3) * 260, "y": 80 + (index // 3) * 150}}
            nodes.append(node)
            generation.emit("workflow.node.added", {"node": node})
        elif action == "update_node":
            node = next((item for item in nodes if item["id"] == operation["id"]), None)
            if node is None:
                raise ValueError(f"找不到节点：{operation['id']}")
            updates = operation.get("data") if isinstance(operation.get("data"), dict) else operation
            node["data"].update({key: value for key, value in updates.items() if key in NODE_DATA_FIELDS})
            if node["data"].get("agent_id") and node["data"]["agent_id"] not in generation.harness_agent_ids:
                raise ValueError(f"不存在的 Harness 智能体：{node['data']['agent_id']}")
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
            if operation.get("condition"):
                edge["condition"] = str(operation["condition"])
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


def _command_exceeds_limit(command: list[str]) -> bool:
    configured = os.environ.get("OPENAGENT_COMPACT_COMMAND_LENGTH")
    try:
        limit = int(configured) if configured else (7000 if os.name == "nt" else 120000)
    except ValueError:
        limit = 7000 if os.name == "nt" else 120000
    return len(subprocess.list2cmdline(command)) >= max(1000, limit)


def _with_file_prompt(command: list[str], prompt: str, path: Path) -> list[str]:
    # OpenCode's --file option accepts multiple values greedily, so the positional
    # message must precede it or the message itself is interpreted as a file path.
    return [*command, prompt, "--file", str(path)]


def _is_command_line_too_long(detail: str) -> bool:
    lowered = detail.lower()
    return "command line is too long" in lowered or "命令行太长" in detail or "winerror 206" in lowered


def _extract_text(item: dict[str, Any]) -> str:
    part = item.get("part") or item.get("properties", {}).get("part")
    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
        return part["text"]
    if item.get("type") == "text" and isinstance(item.get("text"), str):
        return item["text"]
    return ""


def _extract_error(item: dict[str, Any]) -> str:
    event_type = str(item.get("type", "")).lower()
    if "error" not in event_type and not item.get("error"):
        return ""
    value: Any = item.get("error") or item.get("properties") or item
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, dict):
        for key in ("message", "name", "error"):
            if isinstance(value.get(key), str):
                return value[key][:1000]
        nested = value.get("data")
        if isinstance(nested, dict) and isinstance(nested.get("message"), str):
            return nested["message"][:1000]
    return "OpenCode 返回错误事件"


def _operation_data(kind: str, operation: dict[str, Any], node_id: str) -> dict[str, Any]:
    supplied = operation.get("data") if isinstance(operation.get("data"), dict) else operation
    description = str(supplied.get("description") or operation.get("description") or node_id)
    data = {key: value for key, value in supplied.items() if key in NODE_DATA_FIELDS}
    data["description"] = description
    if kind in {"llm", "agent", "tool", "code"}:
        agent_id = data.get("agent_id") or operation.get("agent_id")
        if not agent_id:
            raise ValueError(f"智能体节点 {node_id} 缺少 agent_id")
        data["agent_id"] = agent_id
        data.setdefault("prompt", f"你负责{description}。请基于工作流输入与上游结果完成任务。\n\n工作流输入：{{{{input}}}}\n上游结果：{{{{latest}}}}\n\n请输出结构清晰、可供后续节点使用的结果。")
    elif kind == "prompt":
        data.setdefault("template", f"{description}\n\n输入：{{{{input}}}}\n上游结果：{{{{latest}}}}")
    elif kind == "knowledge_retrieval":
        data.setdefault("query", "{{latest}}")
        data.setdefault("top_k", 3)
        data.setdefault("documents", [])
    elif kind == "http_request":
        data.setdefault("method", "GET")
        data.setdefault("headers", {})
        data.setdefault("timeout_seconds", 30)
        data.setdefault("fail_on_error", True)
    elif kind == "variable_set":
        data.setdefault("variables", {})
    elif kind == "transform":
        data.setdefault("operation", "json_stringify")
    elif kind == "merge":
        data.setdefault("mode", "array")
    elif kind == "condition":
        data.setdefault("expression", "latest")
    elif kind == "switch":
        data.setdefault("cases", [])
        data.setdefault("default_case", "default")
    elif kind in {"iteration", "loop"}:
        data.setdefault("iterations", 3)
        data.setdefault("template", f"{description}（第 {{{{index}}}} 次）\n{{{{latest}}}}")
    elif kind == "validator" and data.get("agent_id"):
        data.setdefault("prompt", f"你负责{description}。请验证以下上游结果并明确给出是否通过、问题清单和修正建议：\n\n{{{{latest}}}}")
    elif kind == "webhook":
        data.setdefault("path", f"/hooks/{node_id}")
        data.setdefault("method", "POST")
    elif kind == "schedule":
        data.setdefault("cron", "0 9 * * *")
        data.setdefault("timezone", "Asia/Shanghai")
    elif kind == "approval":
        data.setdefault("instructions", description)
    elif kind == "delay":
        data.setdefault("seconds", 1)
    return data
