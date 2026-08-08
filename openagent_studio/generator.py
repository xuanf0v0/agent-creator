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

from pydantic import ValidationError

from .evaluation import CandidateResult, HarnessInfrastructureError, SemanticVerdict, WorkflowEvaluator
from .models import WORKFLOW_NODE_TYPES, EvaluationCase, WorkflowEvaluation, WorkflowSpec
from .process_utils import resolve_executable
from .store import SpecStore
from .workflow_runner import EvaluationPolicy, TERMINAL_RUN_STATES, WorkflowManager, validate_executable_workflow


NODE_DATA_FIELDS = {
    "description", "agent_id", "title", "prompt", "template", "expression", "iterations", "relative_path",
    "service_path", "method", "body", "headers", "url", "timeout_seconds", "fail_on_error", "allow_private",
    "path", "cron", "timezone", "query", "top_k", "documents", "variables", "operation", "fields", "mode",
    "separator", "cases", "default_case", "instructions", "workflow_id", "input_template", "seconds",
    "auto_start", "auto_setup", "retry_count", "retry_delay_seconds", "on_error", "fallback_value",
}


class StructuredResultError(RuntimeError):
    pass


class _OpenCodeTimeoutError(RuntimeError):
    """A bounded OpenCode call expired and was terminated."""


class _CompactionTimeoutError(RuntimeError):
    pass


class _EmptyCompactionError(RuntimeError):
    pass


_OPENCODE_LOG_LOCK = threading.Lock()


SYSTEM_PROMPT = """你是 OpenAgent Studio 的工作流生成核心。你的唯一职责是把用户需求转换为可直接执行的智能体工作流。
不要修改文件、不要运行命令、不要调用外部工具。请用中文简短说明你的设计，并逐个输出操作。
“当前工作流”字段是用户画板在本轮请求开始时的完整快照，包含所有既有节点、节点参数和连线；你必须先读取完整快照，再决定如何修改。
不要只关注当前会话曾经创建的节点。即使某个节点来自用户手动拖拽或更早的会话，也必须把它视为当前画板的一部分。
若历史会话记忆与“当前工作流”不一致，始终以本轮提供的完整快照为准。除非用户明确要求，不得删除、重复创建或断开无关的既有节点。
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
4. 模板只可使用 {{input}}、{{latest}}、{{nodes.节点ID}}，循环模板还可使用 {{index}}；不要发明 inputs、input_mapping、task、fields 等未声明字段。AI 节点必须把输入引用直接写入 prompt，output 节点必须把上游引用写入 template。
5. 每个任务提示词要写明角色、目标、输入、约束和预期输出，确保单独交给智能体也能执行。
6. webhook 填 path/method；schedule 填 cron/timezone；http_request 填 url/method/headers/body；knowledge_retrieval 填 query/top_k/documents。
7. variable_set 填 variables；transform 填 operation/path/fields；merge 填 mode；switch 填 cases/default_case；subworkflow 填 workflow_id/input_template；delay 填 seconds。
"""

CHAT_ROUTER_PROMPT = """你是 OpenAgent Studio 中可连续对话的 OpenCode 创作助手。先判断用户这一轮是要聊天答复，还是明确要求修改当前工作流。
只输出 <result>{JSON}</result>，JSON 只能是以下两种之一：
1. {"action":"reply","answer":"直接给用户的完整中文答复或需要追问的澄清问题"}
2. {"action":"modify","request":"结合上下文补全后的、可独立执行的工作流修改要求"}
判断规则：
- 询问概念、当前画布、节点作用、设计建议、可行性、错误原因、如何操作、打招呼或普通交流，一律 reply，直接回答，不修改画布。
- 用户意图不足以确定节点、数据流或期望效果时 reply，并提出最少且具体的澄清问题；不能擅自修改。
- 只有用户明确要求创建、增加、删除、连接、调整或优化工作流时才 modify。
- 若前文由你提出了澄清问题，本轮回答已补足修改要求，则 modify，并把多轮信息合并为完整 request。
- reply 时不得声称已经修改画布；modify 时不要回答用户，只整理 request。
- 可以利用当前工作流和历史对话回答；不得修改文件、运行命令或调用工具。
"""

CASE_PROMPT = """你是工作流验收设计师。根据用户目标和当前工作流生成可编辑的验收用例。
只输出 <result>{JSON}</result>，JSON 必须符合 {"cases":[...]}。每个 case 包含 id、name、enabled、input、assertions、semantic_criteria、approvals、mocks、timeout_seconds。
每个 case 的 id 必须是小写字母、数字和短横线组成的 slug，例如 pc-normal-full-flow；禁止使用下划线、空格、中文或其他符号。
每个 case 的 assertions 必须是非空数组；每项格式为 {"path":"output","operator":"exists"}，operator 只能是 exists、equals、contains、matches、type，path 从最终输出开始。禁止使用空数组。operator 不是 exists 时必须显式提供 expected；equals 应填写要比较的确切值，绝不能省略 expected。
每个 case 的 semantic_criteria 必须是非空字符串数组，例如 ["输出包含明确结论", "关键数据注明来源"]；数组元素禁止使用 {"description":"..."} 等对象，禁止使用空数组或空字符串。
mocks 必须是数组；每项只能使用 {"node_id":"当前工作流中的节点 id","response":任意 JSON}，禁止使用 target 代替 node_id；无需模拟时使用空数组。
timeout_seconds 必须是 1 到 1800 的整数，建议使用默认值 300，禁止填写 3600。
首次创建必须恰好生成 3 个覆盖正常、边界和失败风险的用例。已有用例时必须逐字保留其所有字段，不得删除、禁用或弱化，只能追加最多 3 个与本轮改动直接相关的新用例。
候选会沿正式运行路径真实调用 Harness、模型、工具、HTTP 和子工作流；输入必须适合在当前环境真实执行。不要输出解释。"""

INCREMENTAL_STEP_PROMPT = """你是 OpenAgent Studio 的增量工作流构建器。必须一次只处理一个节点，禁止一次输出完整替代工作流。
只输出 <result>{{JSON}}</result>，action 只能是 add_node、update_node、delete_node 或 complete：
- add_node：{{"action":"add_node","node":{{完整 WorkflowNode}},"edges":[{{"source":"...","target":"...","condition":"可选"}}],"workflow_name":"可选","probe_input":任意JSON,"probe_approvals":{{"审批节点id":true}},"summary":"本步目的"}}
- update_node：字段同 add_node，但 node.id 必须已存在；node 是该节点的完整替换内容，edges 是该节点替换后的全部关联边。
- delete_node：{{"action":"delete_node","node_id":"已有节点id","summary":"删除原因"}}
- complete：{{"action":"complete","summary":"为什么当前图已经完整满足目标"}}
硬约束：
1. add/update 每次只能包含一个 node；本步 edges 必须全部与该 node 直接相连。禁止同时改其他节点。
2. 新增节点后必须立即接入当前图；除第一个节点外禁止孤立节点。创建分支时，每轮只创建分支中的一个节点并连接，下一轮再创建另一个。
3. 优先保持当前已通过探测的节点不变。运行失败时必须先根据失败证据诊断原因，再使用 update_node 修复原节点的参数、提示词、输入映射或连线；失败重试阶段严禁 delete_node。只有用户本轮明确要求删除某个节点时才允许 delete_node。
4. 节点必须使用合法具体 type；AI 节点必须填写可用 agent_id 和完整 prompt；结束时至少有一个 output 节点。
5. 只有当前图已完整满足用户目标、包含必要分支和输出时才 complete。不要因为本层通过探测就提前 complete。
6. probe_input 和 probe_approvals 必须让本次新增/修改的节点在本层真实探测中被执行，尤其要覆盖条件分支。
7. 不得修改 evaluation；系统会在完整图上生成并锁定验收标准。
可用 Harness 智能体：{catalog}
用户完整目标：{request}
当前已接受工作流：{workflow}
最近失败证据：{feedback}
当前已接受层数：{layer}"""


COMPACTION_PROMPT = """请在内部无损压缩下方的工作流生成上下文，只输出精炼后的中文上下文，不要解释压缩过程，也不要完成原任务。
必须保留用户真实意图、所有现有节点和连线的 ID/类型/关键 data、可用 Harness 智能体 ID、失败证据、验收标准，以及约束、条件和精确值；可以删除画布坐标、重复描述和无关措辞。
必须原样保留原上下文要求的结构化输出契约（包括 result/op 标签、JSON 字段和禁止事项）。精炼结果要足以让另一个模型严格按照原始需求继续生成或修复工作流。"""

ATTACHED_PROMPT = "请严格按照附件中的系统规则和精炼上下文生成工作流操作。附件内容是本次任务的完整输入。"

WORKFLOW_CONTRACT = f"""WorkflowSpec 严格契约：顶层只能按 {{"id":"英文小写编号","name":"名称","nodes":[...],"edges":[...],"evaluation":{{"cases":[]}}}} 组织，不得把节点平铺到顶层。
每个节点必须是 {{"id":"英文小写编号","type":"合法类型","data":{{...}},"position":{{"x":数字,"y":数字}}}}；prompt、agent_id、template 等执行参数必须放在 data 内，禁止使用 config 或把参数放在节点顶层。
合法节点 type 仅限：{', '.join(sorted(WORKFLOW_NODE_TYPES))}。
`coding` 等可用 Harness 智能体 ID 只能填写为 agent/llm/tool/code/validator 节点的 data.agent_id，绝对不能作为 type。禁止 human、end、start、task、worker、assistant 等抽象别名；人工输入用 manual_trigger，需要人工确认用 approval，结束输出用 output。
每条连线必须是 {{"source":"节点 id","target":"节点 id"}}，禁止 from/to。只输出唯一的 <result>{{合法 JSON}}</result>，禁止工具调用、DSML、代码块和解释。"""


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
    optimize_only: bool = False
    chat_routing: bool = False
    compaction_disabled: bool = False

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

    def start(self, workflow_id: str, message: str, *, optimize_only: bool = False) -> Generation:
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
                optimize_only=optimize_only,
                chat_routing=not optimize_only,
            )
            self.generations[generation.id] = generation
            self.active[workflow_id] = generation.id
            self.history.setdefault(workflow_id, []).append({"role": "user", "content": message})
        threading.Thread(target=self._run, args=(generation, spec), daemon=True).start()
        return generation

    def optimize(self, workflow_id: str) -> Generation:
        return self.start(workflow_id, "在不改变目标效果和验收标准的前提下，选出最短、最清晰、效果最好的工作流。", optimize_only=True)

    def cancel(self, generation_id: str) -> Generation:
        generation = self.require(generation_id)
        generation.cancelled = True
        if generation.process and generation.process.poll() is None:
            _terminate_process_tree(generation.process)
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
        catalog = [
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "backend_id": item.backend_id,
                "agent_id": item.agent_id,
            }
            for item in spec.harness
        ]
        catalog_json = json.dumps(catalog, ensure_ascii=False)
        original = WorkflowSpec.model_validate(generation.draft)
        environment = self._environment(spec)
        binary = resolve_executable(os.environ.get("OPENCODE_BIN", "opencode"), environment)
        command = [
            binary, "run", "--pure", "--format", "json", "--agent", os.environ.get("OPENCODE_GENERATOR_AGENT", "openagent-generator"),
            "--title", f"OpenAgent生成-{generation.workflow_id}",
        ]
        workdir = Path(spec.project_dir).expanduser()
        if not workdir.is_dir():
            workdir = self.store.path.parent
        evaluator = WorkflowEvaluator(
            lambda prompt, value: self._model_inference(generation, spec, command, workdir, prompt, value),
            lambda workflow, case, output: self._semantic_verdict(generation, spec, command, workdir, workflow, case, output),
            live_execution=True,
            harness_base_url=os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765"),
        )
        evaluator.task_agent_requirements = {
            item.agent_id: {"labels": item.labels, "protocol": item.protocol}
            for item in spec.harness if item.agent_id
        }
        try:
            if generation.chat_routing:
                generation.emit("generation.stage", {"stage": "understanding"})
                decision = self._route_chat_turn(generation, spec, command, workdir, original)
                if decision["action"] == "reply":
                    answer = decision["answer"]
                    self.history.setdefault(generation.workflow_id, []).append({"role": "assistant", "content": answer})
                    generation.completed = True
                    generation.emit("chat.assistant.delta", {"text": answer})
                    generation.emit("chat.completed", {"message": answer})
                    return
                generation.prompt = decision["request"]
            if spec.harness:
                generation.emit("generation.stage", {"stage": "checking_runtime"})
                evaluator.ensure_harness_ready({item.backend_id for item in spec.harness})
            winner = self._build_incrementally(
                generation, spec, command, workdir, original, evaluator, catalog_json,
            )
            generation.emit("generation.stage", {"stage": "saving"})
            generation.draft = winner.model_dump(mode="json")
            summary = "已按你的要求逐节点构建工作流；每个节点和层级均通过连通探测，完整工作流也已通过真实验收。"
            self._finalize(generation)
            if generation.events[-1]["event"] == "generation.completed":
                generation.events[-1]["data"]["assistant_message"] = summary
                self.history.setdefault(generation.workflow_id, []).append({"role": "assistant", "content": summary})
        except Exception as exc:
            if generation.cancelled:
                return
            generation.completed = True
            generation.emit("generation.failed", {"message": str(exc)})

    def _build_incrementally(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        original: WorkflowSpec,
        evaluator: WorkflowEvaluator,
        catalog_json: str,
    ) -> WorkflowSpec:
        current = original.model_copy(deep=True)
        failures: list[dict[str, Any]] = []
        accepted_layers = 0
        iteration = 0
        evaluation_locked = False
        max_iterations = _incremental_max_iterations()
        while True:
            if generation.cancelled:
                raise RuntimeError("生成已取消")
            iteration += 1
            if max_iterations and iteration > max_iterations:
                raise RuntimeError(
                    f"增量构建达到运维配置的最大迭代次数 {max_iterations}，原工作流未改变"
                )
            generation.emit("generation.stage", {
                "stage": "planning_layer", "layer": accepted_layers + 1, "iteration": iteration,
            })
            prompt = INCREMENTAL_STEP_PROMPT.format(
                catalog=catalog_json,
                request=generation.prompt,
                workflow=json.dumps(current.model_dump(mode="json"), ensure_ascii=False),
                feedback=json.dumps(failures[-5:], ensure_ascii=False),
                layer=accepted_layers,
            )
            if failures and not _explicit_delete_request(generation.prompt):
                latest_failure = failures[-1]
                failed_node = str(latest_failure.get("node_id") or "")
                candidate_was_accepted = bool(latest_failure.get("candidate_accepted"))
                if latest_failure.get("action") == "add_node" and not candidate_was_accepted and failed_node not in {node.id for node in current.nodes}:
                    repair_rule = f"该 add_node 候选已回滚，节点 {failed_node or '<unknown>'} 不在当前图中；必须使用 add_node 以相同合法 id 重新创建它。"
                else:
                    repair_rule = "失败节点已存在于当前 accepted graph；必须使用 update_node 修复其参数、prompt、输入映射或连线。"
                prompt += f"\n本轮处于失败修复阶段。请先阅读最近失败证据并明确诊断原因；{repair_rule}禁止返回 delete_node，也不要用删除节点来规避运行错误。"
            raw = self._invoke_result(
                generation, spec, command, workdir, prompt,
                f"增量构建第 {accepted_layers + 1} 层（迭代 {iteration}）",
            )
            raw_action = raw.get("action") if isinstance(raw, dict) else None
            if failures and raw_action == "delete_node" and not _explicit_delete_request(generation.prompt):
                failure = {
                    "phase": "repair_policy",
                    "message": "运行失败后的修复阶段禁止删除节点；必须根据 accepted graph 判断使用 add_node 重建或 update_node 修复",
                    "node_id": raw.get("node_id") or raw.get("id"),
                    "iteration": iteration,
                    "action": raw_action,
                    "candidate_accepted": False,
                }
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": "repair_policy",
                    "node_id": failure["node_id"],
                    "message": failure["message"],
                    "strategy": "recreate_or_update_accepted_node",
                })
                continue
            try:
                candidate, action, touched_node_id, probe_input, probe_approvals, summary = _apply_incremental_step(
                    current, raw, generation.harness_agent_ids,
                )
            except (ValidationError, ValueError, RuntimeError) as exc:
                failure = {"phase": "step_contract", "message": str(exc), "iteration": iteration, "action": raw_action, "candidate_accepted": False}
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": failure["phase"], "node_id": failure.get("node_id"),
                    "message": failure.get("message") or "已收集失败证据，下一轮将诊断并修复原节点",
                    "strategy": "recreate_or_update_accepted_node",
                })
                continue

            # Expose the model's validated incremental result immediately so
            # the Studio canvas can render the new node/parameters while the
            # runtime probe is still running. A later preview event restores
            # the last accepted graph if the probe rejects this candidate.
            generation.emit("workflow.preview", {
                "workflow": candidate.model_dump(mode="json"),
                "layer": accepted_layers + 1,
                "action": action,
                "node_id": touched_node_id,
                "summary": summary,
            })

            generation.emit("generation.step_proposed", {
                "action": action, "node_id": touched_node_id, "summary": summary,
                "layer": accepted_layers + 1, "iteration": iteration,
            })
            generation.emit("generation.stage", {
                "stage": "validating_node", "layer": accepted_layers + 1,
                "node_id": touched_node_id, "iteration": iteration,
            })
            static_errors = _incremental_connectivity_errors(
                spec, current, candidate, action, touched_node_id,
                require_output=action == "complete",
            )
            if static_errors:
                failure = {
                    "phase": "connectivity", "errors": static_errors, "action": action,
                    "node_id": touched_node_id, "iteration": iteration, "candidate_accepted": False,
                }
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": failure["phase"], "node_id": failure.get("node_id"),
                    "message": "结构检查未通过，下一轮将根据错误修复节点或连线",
                    "strategy": "recreate_or_update_accepted_node",
                })
                generation.emit("workflow.preview", {
                    "workflow": current.model_dump(mode="json"),
                    "layer": accepted_layers,
                    "reverted": True,
                    "reason": "connectivity",
                })
                continue

            if action == "complete":
                if not evaluation_locked:
                    generation.emit("generation.stage", {"stage": "preparing_cases"})
                    case_prompt = (
                        f"{CASE_PROMPT}\n已有验收用例：{json.dumps(original.evaluation.model_dump(mode='json'), ensure_ascii=False)}"
                        f"\n当前完整工作流：{json.dumps(candidate.model_dump(mode='json'), ensure_ascii=False)}"
                        f"\n本轮目标：{generation.prompt}"
                    )
                    candidate.evaluation = self._generate_evaluation(
                        generation, spec, command, workdir, case_prompt, original.evaluation,
                    )
                    current.evaluation = candidate.evaluation.model_copy(deep=True)
                    evaluation_locked = True
                else:
                    candidate.evaluation = current.evaluation.model_copy(deep=True)
                generation.emit("generation.stage", {
                    "stage": "full_evaluating", "layer": accepted_layers, "iteration": iteration,
                })
                project = spec.model_copy(deep=True)
                project.workflows = [candidate if item.id == candidate.id else item for item in project.workflows]
                result = evaluator.evaluate(project, candidate, iteration)
                if result.passed:
                    generation.emit("generation.workflow_verified", {
                        "layers": accepted_layers, "iterations": iteration,
                    })
                    return candidate
                failure = {
                    "phase": "full_evaluation", "feedback": self._result_feedback(result),
                    "iteration": iteration, "action": action, "node_id": touched_node_id,
                    "candidate_accepted": False,
                }
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": failure["phase"], "node_id": failure.get("node_id"),
                    "message": "完整验收未通过，下一轮将保留节点并修复失败原因",
                    "strategy": "recreate_or_update_accepted_node",
                })
                current.evaluation = candidate.evaluation.model_copy(deep=True)
                generation.emit("workflow.preview", {
                    "workflow": current.model_dump(mode="json"),
                    "layer": accepted_layers,
                    "reverted": True,
                    "reason": "full_evaluation",
                })
                continue

            generation.emit("generation.stage", {
                "stage": "probing_layer", "layer": accepted_layers + 1,
                "node_id": touched_node_id, "iteration": iteration,
            })
            probe_errors = self._probe_incremental_workflow(
                generation, spec, candidate, touched_node_id, probe_input, probe_approvals,
                evaluator,
            )
            if probe_errors:
                failure = {
                    "phase": "runtime_probe", "errors": probe_errors, "action": action,
                    "node_id": touched_node_id, "iteration": iteration, "candidate_accepted": False,
                }
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": failure["phase"], "node_id": failure.get("node_id"),
                    "message": "运行探测失败，下一轮将诊断 Harness/参数原因并修复原节点",
                    "strategy": "recreate_or_update_accepted_node",
                })
                generation.emit("workflow.preview", {
                    "workflow": current.model_dump(mode="json"),
                    "layer": accepted_layers,
                    "reverted": True,
                    "reason": "runtime_probe",
                })
                continue
            current = candidate
            generation.draft = current.model_dump(mode="json")
            accepted_layers += 1
            failures.clear()
            generation.emit("generation.layer_completed", {
                "layer": accepted_layers, "action": action, "node_id": touched_node_id,
                "summary": summary, "nodes": len(current.nodes), "edges": len(current.edges),
            })
            generation.emit("workflow.updated", {
                "workflow": current.model_dump(mode="json"),
                "layer": accepted_layers,
                "action": action,
                "node_id": touched_node_id,
                "summary": summary,
            })

    def _probe_incremental_workflow(
        self,
        generation: Generation,
        spec: Any,
        workflow: WorkflowSpec,
        touched_node_id: str | None,
        probe_input: Any,
        probe_approvals: dict[str, bool],
        evaluator: WorkflowEvaluator,
    ) -> list[str]:
        project = spec.model_copy(deep=True)
        project.workflows = [workflow if item.id == workflow.id else item for item in project.workflows]
        approvals = {node.id: True for node in workflow.nodes if node.type == "approval"}
        approvals.update(probe_approvals)
        body: dict[str, Any] = {"input": probe_input}
        trigger_node_id = _incremental_trigger_for_node(workflow, touched_node_id)
        if trigger_node_id:
            body["_trigger_node_id"] = trigger_node_id
        manager = WorkflowManager(base_url=evaluator.harness_base_url, poll_interval=0.1)
        policy = EvaluationPolicy(
            approvals=approvals,
            model_inference=evaluator.model_inference,
            live_execution=True,
        )
        try:
            # Incremental layers are intentionally probed before an output
            # node exists. The final `complete` path is still required to
            # contain an output node by the strict connectivity checks.
            run = manager.start(
                project,
                workflow.id,
                body,
                policy=policy,
                record=False,
                require_output=False,
            )
        except (RuntimeError, ValueError) as exc:
            message = str(exc)
            if _is_harness_infrastructure_message(message):
                raise HarnessInfrastructureError(
                    f"Harness 增量层探测基础设施失败：{message}。当前层未进入无效重建。"
                ) from exc
            return [message]
        deadline = time.monotonic() + _incremental_probe_timeout_seconds()
        while run.status not in TERMINAL_RUN_STATES and time.monotonic() < deadline:
            if generation.cancelled:
                run.cancel_event.set()
                raise RuntimeError("生成已取消")
            time.sleep(0.02)
        if run.status not in TERMINAL_RUN_STATES:
            run.cancel_event.set()
            return [f"第 {touched_node_id or '当前'} 层真实探测超时"]
        if run.status != "completed":
            if run.error_code or _is_harness_infrastructure_message(run.error):
                raise HarnessInfrastructureError(
                    f"Harness 增量层探测基础设施失败（code={run.error_code or 'unknown'}）：{run.error}。当前层未进入无效重建。"
                )
            return [run.error or f"增量层状态为 {run.status}"]
        if touched_node_id and touched_node_id in run.node_states:
            status = str(run.node_states[touched_node_id].get("status", ""))
            if status != "completed":
                return [f"本层节点 {touched_node_id} 未被真实执行，状态为 {status or 'unknown'}；请调整探测输入或连线"]
        return []

    def _route_chat_turn(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        workflow: WorkflowSpec,
    ) -> dict[str, str]:
        history = self.history.get(generation.workflow_id, [])[-20:]
        if history and history[-1].get("role") == "user" and history[-1].get("content") == generation.prompt:
            history = history[:-1]
        prompt = (
            f"{CHAT_ROUTER_PROMPT}\n"
            f"当前工作流：{json.dumps(workflow.model_dump(mode='json'), ensure_ascii=False)}\n"
            f"最近对话：{json.dumps(history, ensure_ascii=False)}\n"
            f"用户本轮消息：{generation.prompt}"
        )
        raw = self._invoke_result(generation, spec, command, workdir, prompt, "OpenCode 对话")
        if not isinstance(raw, dict) or raw.get("action") not in {"reply", "modify"}:
            raise RuntimeError("OpenCode 对话结果缺少有效 action（应为 reply 或 modify）")
        action = str(raw["action"])
        field = "answer" if action == "reply" else "request"
        text = str(raw.get(field, "")).strip()
        if not text:
            raise RuntimeError(f"OpenCode 对话结果缺少非空 {field}")
        return {"action": action, field: text}

    def _compact_prompt(
        self,
        generation: Generation,
        binary: str,
        model: str,
        context: str,
        workdir: Path,
        environment: dict[str, str],
    ) -> str:
        command = [
            binary, "run", "--pure", "--format", "json", "--agent", os.environ.get("OPENCODE_COMPACTION_AGENT", "openagent-generator"),
            "--title", "OpenAgent内部上下文提炼",
            "--model", model,
        ]
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        process = subprocess.Popen(
            command, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, bufsize=1, env=environment, encoding="utf-8", errors="replace",
        )
        generation.process = process
        assert process.stdout is not None
        assert process.stdin is not None
        process.stdin.write(f"{COMPACTION_PROMPT}\n\n待压缩原始上下文：\n{context}")
        process.stdin.close()
        text = ""
        diagnostics: list[str] = []
        timed_out = threading.Event()

        def stop_timed_out_process() -> None:
            timed_out.set()
            if process.poll() is None:
                _terminate_process_tree(process)

        timer = threading.Timer(_compaction_timeout_seconds(), stop_timed_out_process)
        timer.daemon = True
        timer.start()
        self._write_opencode_log(generation, {
            "call_id": call_id, "purpose": "上下文提炼", "status": "started",
            "pid": process.pid, "timeout_seconds": _compaction_timeout_seconds(), "model": model,
        })
        code: int | None = None
        try:
            for raw in process.stdout:
                if generation.cancelled:
                    _terminate_process_tree(process)
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
        finally:
            timer.cancel()
            self._write_opencode_log(generation, {
                "call_id": call_id,
                "purpose": "上下文提炼",
                "status": "timeout" if timed_out.is_set() else ("completed" if code == 0 else "failed"),
                "pid": process.pid,
                "exit_code": code,
                "timeout_seconds": _compaction_timeout_seconds(),
                "duration_ms": _elapsed_ms(started),
                "output_chars": len(text),
                "diagnostics": [_redact_log_text(item) for item in diagnostics[-10:]],
                "response_tail": _redact_log_text(text[-4000:]),
            })
        if generation.cancelled:
            raise RuntimeError("生成已取消")
        if timed_out.is_set():
            raise _CompactionTimeoutError(f"OpenCode 上下文提炼超时（{_compaction_timeout_seconds()} 秒）")
        if code != 0:
            detail = diagnostics[-1] if diagnostics else "没有返回错误详情"
            raise RuntimeError(f"OpenCode 内部上下文提炼失败，代码 {code}：{detail}")
        text = text.strip()
        if not text:
            raise _EmptyCompactionError("OpenCode 内部上下文提炼没有返回内容")
        return text

    def _prepare_prompt(
        self,
        generation: Generation,
        spec: Any,
        base_command: list[str],
        workdir: Path,
        prompt: str,
    ) -> str:
        if len(prompt) < _compact_prompt_length() or generation.compaction_disabled:
            return prompt
        generation.emit("generation.context_compacting", {"before_chars": len(prompt)})
        try:
            try:
                compacted = self._compact_prompt(
                    generation, base_command[0], generation.model, prompt, workdir,
                    self._environment(spec),
                ).strip()
            except _EmptyCompactionError:
                generation.emit("generation.context_compaction_retry", {
                    "before_chars": len(prompt), "reason": "empty_output", "attempt": 2,
                })
                strict_context = (
                    "上一次提炼进程成功退出但返回了空内容。本次必须输出非空的中文提炼结果；"
                    "不要只执行内部压缩，不要沉默结束，不要输出工具调用。必须保留原上下文中的全部硬约束。"
                    f"\n\n待提炼的完整原始上下文：\n{prompt}"
                )
                try:
                    compacted = self._compact_prompt(
                        generation, base_command[0], generation.model, strict_context, workdir,
                        self._environment(spec),
                    ).strip()
                except _EmptyCompactionError as exc:
                    raise _EmptyCompactionError(
                        "OpenCode 内部上下文提炼自动严格重试 1 次后仍没有返回内容"
                    ) from exc
        except _CompactionTimeoutError as exc:
            generation.compaction_disabled = True
            generation.emit("generation.context_compaction_failed", {
                "before_chars": len(prompt), "message": str(exc), "fallback": "original",
            })
            return prompt
        use_compacted = bool(compacted) and len(compacted) < len(prompt)
        generation.emit("generation.context_compacted", {
            "before_chars": len(prompt), "after_chars": len(compacted), "used": use_compacted,
        })
        return compacted if use_compacted else prompt

    def _invoke(
        self,
        generation: Generation,
        spec: Any,
        base_command: list[str],
        workdir: Path,
        prompt: str,
        *,
        timeout_seconds: int | None = None,
        purpose: str = "OpenCode 调用",
    ) -> str:
        if generation.cancelled:
            raise RuntimeError("生成已取消")
        environment = self._environment(spec)
        command_base = [*base_command, "--model", generation.model]
        prompt = self._prepare_prompt(generation, spec, base_command, workdir, prompt)
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        try:
            current_process = subprocess.Popen(
                command_base, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, bufsize=1, env=environment, encoding="utf-8", errors="replace",
            )
        except Exception as exc:
            self._write_opencode_log(generation, {
                "call_id": call_id, "purpose": purpose, "status": "spawn_failed", "duration_ms": _elapsed_ms(started),
                "error": _redact_log_text(str(exc)),
            })
            raise
        generation.process = current_process
        assert current_process.stdout is not None
        assert current_process.stdin is not None
        current_process.stdin.write(prompt)
        current_process.stdin.close()
        timed_out = threading.Event()

        def stop_timed_out_process() -> None:
            timed_out.set()
            if current_process.poll() is None:
                _terminate_process_tree(current_process)

        call_timeout = timeout_seconds or _invoke_timeout_seconds()
        timer = threading.Timer(call_timeout, stop_timed_out_process)
        timer.daemon = True
        timer.start()
        self._write_opencode_log(generation, {
            "call_id": call_id, "purpose": purpose, "status": "started",
            "pid": current_process.pid, "timeout_seconds": call_timeout, "model": generation.model,
        })
        assistant_text, diagnostics = "", []
        event_counts: dict[str, int] = {}
        tool_counts: dict[str, int] = {}
        protocol_events = 0
        reasoning_chars = 0
        text_events = 0
        tool_events = 0
        last_event_at = started
        last_event_type = "process_started"
        last_tool: str | None = None
        code: int | None = None
        try:
            for raw in current_process.stdout:
                if generation.cancelled:
                    return ""
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    if raw.strip():
                        diagnostics.append(raw.strip())
                    continue
                protocol_events += 1
                event_type = str(item.get("type") or "unknown")
                event_counts[event_type] = event_counts.get(event_type, 0) + 1
                last_event_at = time.monotonic()
                last_event_type = event_type
                part = item.get("part") or item.get("properties", {}).get("part")
                if isinstance(part, dict):
                    part_type = str(part.get("type") or "")
                    if part_type:
                        event_counts[part_type] = event_counts.get(part_type, 0) + 1
                    if part_type == "reasoning":
                        reasoning_chars += len(str(part.get("text") or ""))
                    elif part_type == "text":
                        text_events += 1
                    elif part_type == "tool":
                        tool_events += 1
                        last_tool = str(part.get("tool") or part.get("name") or "unknown")
                        tool_counts[last_tool] = tool_counts.get(last_tool, 0) + 1
                if error := _extract_error(item):
                    diagnostics.append(error)
                text = _extract_text(item)
                if text:
                    assistant_text = text if text.startswith(assistant_text) else assistant_text + text
            code = current_process.wait()
        finally:
            timer.cancel()
            self._write_opencode_log(generation, {
                "call_id": call_id,
                "purpose": purpose,
                "status": "timeout" if timed_out.is_set() else ("completed" if code == 0 else "failed"),
                "pid": current_process.pid,
                "exit_code": code,
                "timeout_seconds": call_timeout,
                "duration_ms": _elapsed_ms(started),
                "output_chars": len(assistant_text),
                "diagnostics": [_redact_log_text(item) for item in diagnostics[-10:]],
                "response_tail": _redact_log_text(assistant_text[-4000:]),
                "protocol_events": protocol_events,
                "event_counts": event_counts,
                "tool_counts": tool_counts,
                "reasoning_chars": reasoning_chars,
                "text_events": text_events,
                "tool_events": tool_events,
                "last_event_type": last_event_type,
                "last_tool": last_tool,
                "last_activity_ms": round((last_event_at - started) * 1000),
                "idle_at_exit_ms": round((time.monotonic() - last_event_at) * 1000),
            })
        if timed_out.is_set():
            activity = (
                f"协议事件 {protocol_events} 个，工具调用 {tool_events} 次，"
                f"reasoning {reasoning_chars} 字，最终文本 {len(assistant_text)} 字；"
                f"最后事件 {last_event_type}，最后工具 {last_tool or '无'}，"
                f"最后活动距启动 {round((last_event_at - started) * 1000)} ms，"
                f"超时前空闲 {round((time.monotonic() - last_event_at) * 1000)} ms"
            )
            raise _OpenCodeTimeoutError(
                f"OpenCode 单次调用超时（{call_timeout} 秒）；{activity}。"
                "详见 .openagent-logs/opencode.jsonl"
            )
        if tool_events:
            raise RuntimeError(
                f"OpenCode 生成器违反无工具契约：检测到 {tool_events} 次工具调用（最后工具 {last_tool or 'unknown'}）；"
                "请使用 openagent-generator，而不是带工具的 Agent。"
            )
        permission_diagnostics = [item for item in diagnostics if "permission requested" in item.lower() or "external_directory" in item.lower()]
        if permission_diagnostics:
            raise RuntimeError("OpenCode 生成器触发了被禁止的权限请求；请检查无工具 Agent 配置")
        if code == 0:
            return assistant_text
        detail = diagnostics[-1] if diagnostics else "没有返回错误详情"
        raise RuntimeError(f"OpenCode 退出，代码 {code}：{detail}")

    def _invoke_result(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        prompt: str,
        purpose: str,
        *,
        timeout_seconds: int | None = None,
    ) -> Any:
        # A timeout is an infrastructure/model latency failure, not a malformed
        # response. Retrying it would simply multiply the wait time.
        text = self._invoke_for_result(generation, spec, command, workdir, prompt, timeout_seconds, purpose)
        try:
            return _parse_result(text)
        except StructuredResultError:
            retry_prompt = (
                f"{prompt}\n\n你上一次没有返回可解析的结构化 JSON。请重新完成同一任务，只输出一个 "
                "<result>{合法 JSON}</result>，不要使用注释、尾随逗号、单引号或任何额外说明。"
            )
            retry_text = self._invoke_for_result(
                generation, spec, command, workdir, retry_prompt, timeout_seconds, f"{purpose}（严格重试）",
            )
            try:
                return _parse_result(retry_text)
            except StructuredResultError as exc:
                raise StructuredResultError(f"{purpose}未返回有效的结构化 JSON（已自动严格重试 1 次）") from exc

    def _invoke_for_result(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        prompt: str,
        timeout_seconds: int | None,
        purpose: str,
    ) -> str:
        """Invoke while remaining compatible with test/integration overrides."""
        try:
            kwargs: dict[str, Any] = {"purpose": purpose}
            if timeout_seconds is not None:
                kwargs["timeout_seconds"] = timeout_seconds
            return self._invoke(generation, spec, command, workdir, prompt, **kwargs)
        except TypeError as exc:
            # Existing embedders may override _invoke with the historical
            # five-argument signature. Preserve that extension point.
            detail = str(exc)
            if "timeout_seconds" not in detail and "purpose" not in detail:
                raise
            return self._invoke(generation, spec, command, workdir, prompt)

    def _write_opencode_log(self, generation: Generation, data: dict[str, Any]) -> None:
        configured = os.environ.get("OPENAGENT_OPENCODE_LOG")
        path = Path(configured).expanduser() if configured else self.store.path.parent / ".openagent-logs" / "opencode.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            record = _redact_log_value({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "generation_id": generation.id,
                "workflow_id": generation.workflow_id,
                **data,
            })
            with _OPENCODE_LOG_LOCK:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Diagnostics must never turn a model failure into a different failure.
            return

    def _generate_evaluation(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        prompt: str,
        previous: WorkflowEvaluation,
    ) -> WorkflowEvaluation:
        raw = self._invoke_result(generation, spec, command, workdir, prompt, "验收用例")
        try:
            return self._validate_evaluation_result(previous, raw)
        except (ValidationError, RuntimeError) as exc:
            retry_prompt = (
                f"{prompt}\n\n你上一次返回的验收用例未通过严格校验。"
                f"\n校验错误：{exc}"
                f"\n无效结果：{json.dumps(raw, ensure_ascii=False)}"
                "\n请修复全部错误后重新输出完整 {\"cases\":[...]}。每个用例必须同时包含至少一条 "
                "assertions 确定性断言和至少一条非空 semantic_criteria 语义质量标准；"
                "operator 不是 exists 时必须显式填写 expected，equals 不得省略 expected；"
                "已有用例仍必须逐字段原样保留。只输出 <result>{合法 JSON}</result>。"
            )
            repaired = self._invoke_result(generation, spec, command, workdir, retry_prompt, "验收用例自动修复")
            try:
                return self._validate_evaluation_result(previous, repaired)
            except (ValidationError, RuntimeError) as repaired_exc:
                raise RuntimeError(f"验收用例自动修复后仍不合格：{repaired_exc}") from repaired_exc

    @classmethod
    def _validate_evaluation_result(cls, previous: WorkflowEvaluation, value: Any) -> WorkflowEvaluation:
        evaluation = WorkflowEvaluation.model_validate(_normalize_evaluation_result(value))
        cls._validate_case_update(previous, evaluation)
        return evaluation

    @staticmethod
    def _validate_case_update(previous: WorkflowEvaluation, current: WorkflowEvaluation) -> None:
        old = [item.model_dump(mode="json") for item in previous.cases]
        new = [item.model_dump(mode="json") for item in current.cases]
        if not old and len(new) != 3:
            raise RuntimeError("首次生成必须创建恰好 3 个验收用例")
        if old and (new[:len(old)] != old or len(new) > len(old) + 3):
            raise RuntimeError("OpenCode 试图删除、修改或一次追加超过 3 个既有验收用例")
        ids = [item.id for item in current.cases]
        if len(ids) != len(set(ids)):
            raise RuntimeError("验收用例 id 重复")
        incomplete = []
        for item in current.cases:
            missing = []
            if not item.assertions:
                missing.append("assertions 确定性断言")
            invalid_assertions = [
                f"{assertion.path}:{assertion.operator}"
                for assertion in item.assertions
                if assertion.operator != "exists" and "expected" not in assertion.model_fields_set
            ]
            if invalid_assertions:
                missing.append(
                    "非 exists 断言缺少 expected（" + ", ".join(invalid_assertions) + "）"
                )
            if not item.semantic_criteria or any(not criterion.strip() for criterion in item.semantic_criteria):
                missing.append("semantic_criteria 语义质量标准")
            if missing:
                incomplete.append(f"{item.id} 缺少 {' 和 '.join(missing)}")
        if incomplete:
            raise RuntimeError(f"每个验收用例都必须同时包含确定性断言和语义质量标准：{'；'.join(incomplete)}")

    def _model_inference(self, generation: Generation, spec: Any, command: list[str], workdir: Path, prompt: str, value: Any) -> Any:
        request = f"执行以下工作流 AI 节点。只输出 <result>{{JSON}}</result>。不得修改文件、运行命令、调用工具或访问非模型网络。\n任务：{prompt}\n输入：{json.dumps(value, ensure_ascii=False)}"
        return self._invoke_result(generation, spec, command, workdir, request, "工作流 AI 节点")

    def _semantic_verdict(self, generation: Generation, spec: Any, command: list[str], workdir: Path, workflow: WorkflowSpec, case: EvaluationCase, output: Any) -> SemanticVerdict:
        prompt = (
            "你是独立 OpenCode 验证智能体，不参与工作流生成。必须根据实际试运行输出逐条验证语义标准。"
            "只输出 <result>{\"passed\":布尔值,\"score\":0到100的整数,\"issues\":[\"未通过原因\"]}</result>。"
            "只有所有标准都满足时 passed 才能为 true；不确定一律判定 false。"
            f"\n工作流：{json.dumps(workflow.model_dump(mode='json', exclude={'evaluation'}), ensure_ascii=False)}"
            f"\n验收标准：{json.dumps(case.semantic_criteria, ensure_ascii=False)}\n实际试运行输出：{json.dumps(output, ensure_ascii=False)}"
        )
        result = self._invoke_result(generation, spec, command, workdir, prompt, "独立语义验证")
        if not isinstance(result, dict):
            return SemanticVerdict(False, 0, ["OpenCode 验证结果格式无效"])
        score = max(0, min(int(result.get("score", 0)), 100))
        issues = [str(item) for item in result.get("issues", [])] if isinstance(result.get("issues", []), list) else []
        return SemanticVerdict(result.get("passed") is True, score, issues)

    def _evaluate_candidates(self, generation: Generation, spec: Any, candidates: list[WorkflowSpec], evaluator: WorkflowEvaluator) -> list[CandidateResult]:
        generation.emit("generation.stage", {"stage": "validating"})
        generation.emit("generation.stage", {"stage": "evaluating"})
        generation.emit("generation.stage", {"stage": "verifying"})
        results: list[CandidateResult] = []
        for index, candidate in enumerate(candidates):
            project = spec.model_copy(deep=True)
            project.workflows = [candidate if item.id == candidate.id else item for item in project.workflows]
            results.append(evaluator.evaluate(project, candidate, index))
        generation.emit("generation.stage", {"stage": "selecting"})
        return results

    @staticmethod
    def _result_feedback(result: CandidateResult) -> dict[str, Any]:
        return {
            "validation_errors": result.errors,
            "case_failures": [
                {"case_id": case.case_id, "errors": case.errors, "semantic_score": case.semantic_score, "opencode_verified": case.opencode_verified}
                for case in result.cases if not case.passed
            ],
        }

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


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _redact_log_text(value: Any) -> str:
    """Keep diagnostics useful without writing common credentials to disk."""
    text = str(value)
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "<redacted-key>", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|secret)\s*[=:]\s*)[^\s,;]+",
        r"\1<redacted>",
        text,
    )
    return text


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_log_text(value)
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_log_value(item) for key, item in value.items()}
    return value


def _invoke_timeout_seconds() -> int:
    try:
        # Bound each incremental planning call; the outer build loop continues
        # until verification passes, cancellation, or an infrastructure error.
        return max(30, min(int(os.environ.get("OPENCODE_GENERATOR_CALL_TIMEOUT", "120")), 1800))
    except ValueError:
        return 120


def _repair_timeout_seconds() -> int:
    """Keep optimization repair attempts short enough to fail fast.

    Repair prompts are bounded and should not hold the whole optimization loop
    hostage to a slow model. Set OPENCODE_REPAIR_CALL_TIMEOUT to opt into a
    different bound (30..600 seconds).
    """
    try:
        return max(30, min(int(os.environ.get("OPENCODE_REPAIR_CALL_TIMEOUT", "60")), 600))
    except ValueError:
        return 60


def _incremental_probe_timeout_seconds() -> int:
    try:
        return max(30, min(int(os.environ.get("OPENAGENT_INCREMENTAL_PROBE_TIMEOUT", "300")), 1800))
    except ValueError:
        return 300


def _incremental_max_iterations() -> int:
    """Optional operator safety cap; zero keeps the user-requested loop unbounded."""
    try:
        return max(0, min(int(os.environ.get("OPENAGENT_INCREMENTAL_MAX_ITERATIONS", "0")), 10000))
    except ValueError:
        return 0


def _compaction_timeout_seconds() -> int:
    try:
        return max(30, min(int(os.environ.get("OPENCODE_COMPACTION_TIMEOUT", "30")), 300))
    except ValueError:
        return 30


def _compact_prompt_length() -> int:
    try:
        return max(4000, min(int(os.environ.get("OPENCODE_COMPACT_PROMPT_LENGTH", "12000")), 200000))
    except ValueError:
        return 12000


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        process.terminate()


def _normalize_workflow_result(
    value: Any,
    harness_agent_ids: set[str],
    *,
    expected_workflow_id: str | None = None,
) -> Any:
    """Accept a small set of common workflow aliases before strict validation."""
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    if expected_workflow_id and value.get("id") != expected_workflow_id:
        # The route-owned workflow identity is not model-editable. Keep the
        # generated graph while binding its envelope back to the requested
        # workflow so a cosmetic model id change cannot discard the candidate.
        normalized["id"] = expected_workflow_id
    normalized.setdefault("name", str(value.get("title") or value.get("workflow_name") or value.get("id") or "生成工作流"))
    raw_nodes = value.get("nodes")
    if isinstance(raw_nodes, list):
        nodes: list[Any] = []
        available_agents = sorted(harness_agent_ids)
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict):
                nodes.append(raw_node)
                continue
            node = dict(raw_node)
            data = dict(raw_node.get("data")) if isinstance(raw_node.get("data"), dict) else {}
            for alias in ("config", "parameters"):
                if isinstance(raw_node.get(alias), dict):
                    for key, item in raw_node[alias].items():
                        data.setdefault(key, item)
            for key in NODE_DATA_FIELDS:
                if key not in data and key in raw_node:
                    data[key] = raw_node[key]
            if "prompt" not in data and isinstance(data.get("task"), str):
                data["prompt"] = data["task"]
            if "inputs" not in data and isinstance(data.get("input_mapping"), dict):
                data["inputs"] = data["input_mapping"]
            description = str(data.get("description") or raw_node.get("name") or raw_node.get("title") or raw_node.get("id") or "任务")
            data.setdefault("description", description)
            raw_type = str(raw_node.get("type", "")).strip().lower()
            if raw_type in harness_agent_ids:
                node["type"] = "agent"
                data.setdefault("agent_id", raw_type)
            elif raw_type in {"task", "worker", "agent_task", "assistant"}:
                node["type"] = "agent"
                agent: Any = data.get("agent_id") or raw_node.get("agent") or raw_node.get("worker") or raw_node.get("assignee")
                if isinstance(agent, dict):
                    agent = agent.get("id") or agent.get("agent_id")
                if not isinstance(agent, str) or agent not in harness_agent_ids:
                    if len(available_agents) == 1:
                        agent = available_agents[0]
                    else:
                        node_id = raw_node.get("id", "unknown")
                        raise RuntimeError(f"通用 task 节点 {node_id} 无法确定 Harness 智能体，请明确填写 agent_id")
                data["agent_id"] = agent
                task_prompt = raw_node.get("instructions") or raw_node.get("task")
                data.setdefault(
                    "prompt",
                    str(task_prompt or f"你负责{description}。请基于工作流输入与上游结果完成任务。\n\n工作流输入：{{{{input}}}}\n上游结果：{{{{latest}}}}\n\n请输出结构清晰、可供后续节点使用的结果。"),
                )
            elif raw_type in {"human", "user_input", "input", "start", "begin", "trigger"}:
                node["type"] = "manual_trigger"
            elif raw_type in {"end", "finish", "terminal", "result", "final"}:
                node["type"] = "output"
            if node.get("type") == "output" and "template" not in data:
                mapping = data.get("input_mapping")
                if isinstance(mapping, dict) and len(mapping) == 1:
                    data["template"] = next(iter(mapping.values()))
            node["data"] = data
            nodes.append(node)
        normalized["nodes"] = nodes
    raw_edges = value.get("edges")
    if isinstance(raw_edges, list):
        edges: list[Any] = []
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, dict):
                edges.append(raw_edge)
                continue
            edge = dict(raw_edge)
            if "source" not in edge and "from" in edge:
                edge["source"] = edge.pop("from")
            if "target" not in edge and "to" in edge:
                edge["target"] = edge.pop("to")
            edges.append(edge)
        normalized["edges"] = edges
    return normalized


def _apply_incremental_step(
    current: WorkflowSpec,
    value: Any,
    harness_agent_ids: set[str],
) -> tuple[WorkflowSpec, str, str | None, Any, dict[str, bool], str]:
    if not isinstance(value, dict):
        raise RuntimeError("增量构建结果必须是 JSON 对象")
    action = str(value.get("action", "")).strip()
    if action not in {"add_node", "update_node", "delete_node", "complete"}:
        raise RuntimeError("增量构建 action 必须是 add_node、update_node、delete_node 或 complete")
    summary = str(value.get("summary", "")).strip() or action
    if action == "complete":
        return current.model_copy(deep=True), action, None, value.get("probe_input"), {}, summary

    draft = current.model_dump(mode="json")
    if isinstance(value.get("workflow_name"), str) and value["workflow_name"].strip():
        draft["name"] = value["workflow_name"].strip()
    touched_node_id: str | None = None
    if action == "delete_node":
        touched_node_id = str(value.get("node_id", "")).strip()
        if not touched_node_id or not any(node["id"] == touched_node_id for node in draft["nodes"]):
            raise RuntimeError(f"删除目标节点不存在：{touched_node_id or '<empty>'}")
        draft["nodes"] = [node for node in draft["nodes"] if node["id"] != touched_node_id]
        draft["edges"] = [
            edge for edge in draft["edges"]
            if edge["source"] != touched_node_id and edge["target"] != touched_node_id
        ]
    else:
        raw_node = value.get("node")
        if not isinstance(raw_node, dict):
            raise RuntimeError(f"{action} 缺少完整 node 对象")
        raw_edges = value.get("edges", [])
        if not isinstance(raw_edges, list):
            raise RuntimeError("增量构建 edges 必须是数组")
        normalized_step = _normalize_workflow_result({
            "id": current.id,
            "name": current.name,
            "nodes": [raw_node],
            "edges": raw_edges,
        }, harness_agent_ids, expected_workflow_id=current.id)
        step_workflow = WorkflowSpec.model_validate(normalized_step)
        node = step_workflow.nodes[0]
        touched_node_id = node.id
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", touched_node_id):
            raise RuntimeError(f"节点编号不合法：{touched_node_id}")
        existing_ids = {item["id"] for item in draft["nodes"]}
        if action == "add_node":
            if touched_node_id in existing_ids:
                raise RuntimeError(f"新增节点已存在：{touched_node_id}")
            if len(draft["nodes"]) >= 50:
                raise RuntimeError("节点数量不能超过 50；请更新或删除现有节点")
            draft["nodes"].append(node.model_dump(mode="json"))
        else:
            if touched_node_id not in existing_ids:
                raise RuntimeError(f"更新目标节点不存在：{touched_node_id}")
            draft["nodes"] = [
                node.model_dump(mode="json") if item["id"] == touched_node_id else item
                for item in draft["nodes"]
            ]
            if "edges" in value:
                draft["edges"] = [
                    edge for edge in draft["edges"]
                    if edge["source"] != touched_node_id and edge["target"] != touched_node_id
                ]
        for edge in step_workflow.edges:
            if touched_node_id not in {edge.source, edge.target}:
                raise RuntimeError(f"本步连线必须连接当前节点 {touched_node_id}：{edge.source} → {edge.target}")
            dumped = edge.model_dump(mode="json", exclude_none=True)
            if dumped not in draft["edges"]:
                draft["edges"].append(dumped)
    candidate = WorkflowSpec.model_validate(draft)
    approvals_value = value.get("probe_approvals", {})
    if approvals_value is None:
        approvals_value = {}
    if not isinstance(approvals_value, dict):
        raise RuntimeError("probe_approvals 必须是审批节点到布尔值的对象")
    probe_approvals = {str(key): bool(item) for key, item in approvals_value.items()}
    return candidate, action, touched_node_id, value.get("probe_input"), probe_approvals, summary


def _incremental_connectivity_errors(
    project: Any,
    previous: WorkflowSpec,
    candidate: WorkflowSpec,
    action: str,
    touched_node_id: str | None,
    *,
    require_output: bool,
) -> list[str]:
    errors = validate_executable_workflow(project, candidate, runtime=True, require_output=require_output)
    node_ids = {node.id for node in candidate.nodes}
    edge_keys = [(edge.source, edge.target, edge.condition) for edge in candidate.edges]
    if len(edge_keys) != len(set(edge_keys)):
        errors.append("工作流包含重复连线")
    if action == "add_node" and previous.nodes and touched_node_id:
        if not any(touched_node_id in {edge.source, edge.target} for edge in candidate.edges):
            errors.append(f"新增节点 {touched_node_id} 没有接入当前工作流")
    if node_ids:
        adjacency = {node_id: set() for node_id in node_ids}
        for edge in candidate.edges:
            if edge.source in adjacency and edge.target in adjacency:
                adjacency[edge.source].add(edge.target)
                adjacency[edge.target].add(edge.source)
        visited: set[str] = set()
        stack = [next(iter(node_ids))]
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            stack.extend(adjacency[node_id] - visited)
        disconnected = sorted(node_ids - visited)
        if disconnected:
            errors.append(f"工作流存在未连通节点：{', '.join(disconnected)}")
    if require_output:
        output_ids = {node.id for node in candidate.nodes if node.type == "output"}
        if not output_ids:
            errors.append("完整工作流至少需要一个 output 节点")
        sources = {edge.source for edge in candidate.edges}
        invalid_sinks = sorted(
            node.id for node in candidate.nodes
            if node.id not in sources and node.type != "output"
        )
        if invalid_sinks:
            errors.append(f"完整工作流的终点必须是 output 节点：{', '.join(invalid_sinks)}")
    return list(dict.fromkeys(errors))


def _explicit_delete_request(prompt: str) -> bool:
    return bool(re.search(r"(?:删除|移除|删掉|去掉|delete|remove)\s*[^。！？\n]{0,80}(?:节点|node)?", prompt, re.IGNORECASE))


def _incremental_trigger_for_node(workflow: WorkflowSpec, touched_node_id: str | None) -> str | None:
    nodes = {node.id: node for node in workflow.nodes}
    parents: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in workflow.edges:
        if edge.target in parents:
            parents[edge.target].append(edge.source)
    queue_ids = [touched_node_id] if touched_node_id in nodes else list(nodes)
    visited: set[str] = set()
    while queue_ids:
        node_id = queue_ids.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        node = nodes[node_id]
        if node.type in {"webhook", "schedule"}:
            return node_id
        queue_ids.extend(parents[node_id])
    return None


def _is_harness_infrastructure_message(message: str) -> bool:
    return str(message).startswith((
        "Harness 不可用：",
        "Harness 基础设施错误：",
        "Harness 任务 API 契约不兼容：",
        "Harness 请求失败（502）",
        "Harness 请求失败（503）",
        "Harness 请求失败（504）",
        "environment drift",
        "setup required",
        "setup_required",
        "agent environment setup is required",
        "agent setup or runtime is in error",
    )) or any(f"code={code}" in str(message) for code in {
        "setup_required", "agent_process_failed", "agent_timeout", "agent_permission_denied",
        "sandbox_unavailable", "sandbox_denied", "protocol_output_invalid",
    })


def _normalize_evaluation_result(value: Any) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        return value
    normalized = dict(value)
    cases: list[Any] = []
    used_ids: set[str] = set()
    for index, raw_case in enumerate(value["cases"], start=1):
        if not isinstance(raw_case, dict):
            cases.append(raw_case)
            continue
        case = dict(raw_case)
        raw_id = case.get("id")
        case_id = str(raw_id).strip().lower() if raw_id is not None else ""
        case_id = re.sub(r"[^a-z0-9]+", "-", case_id).strip("-") or f"case-{index}"
        base_case_id = case_id
        suffix = 2
        while case_id in used_ids:
            case_id = f"{base_case_id}-{suffix}"
            suffix += 1
        used_ids.add(case_id)
        case["id"] = case_id
        criteria = case.get("semantic_criteria")
        if isinstance(criteria, str):
            case["semantic_criteria"] = [criteria]
        elif isinstance(criteria, list):
            normalized_criteria: list[Any] = []
            for criterion in criteria:
                if isinstance(criterion, dict):
                    text = next((
                        criterion.get(key) for key in ("description", "criterion", "text", "content")
                        if isinstance(criterion.get(key), str)
                    ), None)
                    normalized_criteria.append(text if text is not None else criterion)
                else:
                    normalized_criteria.append(criterion)
            case["semantic_criteria"] = normalized_criteria
        assertions = case.get("assertions")
        if isinstance(assertions, dict):
            case["assertions"] = [assertions]
        approvals = case.get("approvals")
        if isinstance(approvals, list):
            decisions: dict[str, bool] = {}
            for item in approvals:
                if isinstance(item, str):
                    decisions[item] = True
                elif isinstance(item, dict):
                    node_id = item.get("node_id") or item.get("id")
                    if isinstance(node_id, str):
                        decisions[node_id] = item.get("approved", True) is not False
            case["approvals"] = decisions
        mocks = case.get("mocks")
        if isinstance(mocks, dict):
            case["mocks"] = [{"node_id": str(node_id), "response": response} for node_id, response in mocks.items()]
        elif isinstance(mocks, list):
            normalized_mocks: list[Any] = []
            for mock in mocks:
                if not isinstance(mock, dict):
                    normalized_mocks.append(mock)
                    continue
                normalized_mock = dict(mock)
                if "node_id" not in normalized_mock:
                    node_id = normalized_mock.get("target") or normalized_mock.get("id")
                    if node_id is not None:
                        normalized_mock["node_id"] = str(node_id)
                normalized_mocks.append(normalized_mock)
            case["mocks"] = normalized_mocks
        timeout_seconds = case.get("timeout_seconds")
        if isinstance(timeout_seconds, int) and not isinstance(timeout_seconds, bool):
            case["timeout_seconds"] = max(1, min(timeout_seconds, 1800))
        elif isinstance(timeout_seconds, str) and re.fullmatch(r"[+-]?\d+", timeout_seconds.strip()):
            case["timeout_seconds"] = max(1, min(int(timeout_seconds), 1800))
        cases.append(case)
    normalized["cases"] = cases
    return normalized


def _parse_result(text: str) -> Any:
    tagged = re.findall(r"<result>\s*(.*?)\s*</result>", text, re.DOTALL | re.IGNORECASE)
    payloads = [*tagged, text] if tagged else [text]
    for payload in payloads:
        cleaned = payload.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        try:
            value = json.loads(cleaned)
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    pass
            return value
        except json.JSONDecodeError:
            pass
        embedded = _largest_embedded_json(cleaned)
        if embedded is not None:
            return embedded
    raise StructuredResultError("OpenCode 未返回有效的结构化 JSON 结果")


def _largest_embedded_json(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    best_value: Any | None = None
    best_length = -1
    for match in re.finditer(r"[\[{]", text):
        try:
            value, end = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if end >= best_length:
            best_value = value
            best_length = end
    return best_value


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
