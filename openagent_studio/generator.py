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

from .evaluation import CandidateResult, SemanticVerdict, WorkflowEvaluator
from .models import WORKFLOW_NODE_TYPES, EvaluationCase, WorkflowEvaluation, WorkflowSpec
from .process_utils import resolve_executable
from .store import SpecStore


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
4. 模板可使用 {{input}}、{{latest}}、{{nodes.节点ID}}，循环模板还可使用 {{index}}。
5. 每个任务提示词要写明角色、目标、输入、约束和预期输出，确保单独交给智能体也能执行。
6. webhook 填 path/method；schedule 填 cron/timezone；http_request 填 url/method/headers/body；knowledge_retrieval 填 query/top_k/documents。
7. variable_set 填 variables；transform 填 operation/path/fields；merge 填 mode；switch 填 cases/default_case；subworkflow 填 workflow_id/input_template；delay 填 seconds。
"""

CASE_PROMPT = """你是工作流验收设计师。根据用户目标和当前工作流生成可编辑的验收用例。
只输出 <result>{JSON}</result>，JSON 必须符合 {"cases":[...]}。每个 case 包含 id、name、enabled、input、assertions、semantic_criteria、approvals、mocks、timeout_seconds。
每个 case 的 id 必须是小写字母、数字和短横线组成的 slug，例如 pc-normal-full-flow；禁止使用下划线、空格、中文或其他符号。
每个 case 的 assertions 必须是非空数组；每项格式为 {"path":"output","operator":"exists"}，operator 只能是 exists、equals、contains、matches、type，path 从最终输出开始。禁止使用空数组。
每个 case 的 semantic_criteria 必须是非空字符串数组，例如 ["输出包含明确结论", "关键数据注明来源"]；数组元素禁止使用 {"description":"..."} 等对象，禁止使用空数组或空字符串。
mocks 必须是数组；每项只能使用 {"node_id":"当前工作流中的节点 id","response":任意 JSON}，禁止使用 target 代替 node_id；无需模拟时使用空数组。
timeout_seconds 必须是 1 到 1800 的整数，建议使用默认值 300，禁止填写 3600。
首次创建必须恰好生成 3 个覆盖正常、边界和失败风险的用例。已有用例时必须逐字保留其所有字段，不得删除、禁用或弱化，只能追加最多 3 个与本轮改动直接相关的新用例。
候选会沿正式运行路径真实调用 Harness、模型、工具、HTTP 和子工作流；输入必须适合在当前环境真实执行。不要输出解释。"""

CANDIDATE_PROMPT = """你是 OpenAgent Studio 的工作流架构师。请根据用户目标优化完整工作流。
只输出 <result>{{JSON}}</result>，JSON 必须是完整 WorkflowSpec，包含 id、name、nodes、edges、evaluation；不得输出操作列表或解释。
节点 type 只能使用系统列出的具体节点类型，禁止使用 task；连线字段必须是 source 和 target，禁止使用 from 和 to。
保留无关既有能力，所有节点必须可执行，图必须无环。evaluation 将由系统覆盖，不要尝试改变验收标准。
策略：{strategy}。可用 Harness 智能体：{catalog}\n当前工作流：{workflow}\n用户目标：{request}"""

REPAIR_PROMPT = """你是 OpenAgent Studio 的工作流修复工程师。下面候选已经真实试运行并被独立 OpenCode 验证判定不通过。
必须根据失败证据修复完整工作流，不得删改或弱化验收标准。只输出 <result>{{JSON}}</result>，JSON 必须是完整 WorkflowSpec。
节点 type 只能使用系统列出的具体节点类型，禁止使用 task；连线字段必须是 source 和 target，禁止使用 from 和 to。
修复轮次：{round_number}\n修复策略：{strategy}\n失败候选：{workflow}\n验证失败证据：{feedback}\n固定验收标准：{evaluation}\n可用 Harness 智能体：{catalog}\n用户目标：{request}"""


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
            binary, "run", "--format", "json", "--agent", os.environ.get("OPENCODE_GENERATOR_AGENT", "plan"),
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
        try:
            if spec.harness:
                generation.emit("generation.stage", {"stage": "checking_runtime"})
                evaluator.ensure_harness_ready({item.backend_id for item in spec.harness})
            generation.emit("generation.stage", {"stage": "preparing_cases"})
            case_prompt = (
                f"{CASE_PROMPT}\n已有验收用例：{json.dumps(original.evaluation.model_dump(mode='json'), ensure_ascii=False)}"
                f"\n当前工作流：{json.dumps(original.model_dump(mode='json'), ensure_ascii=False)}\n本轮目标：{generation.prompt}"
            )
            evaluation = self._generate_evaluation(
                generation, spec, command, workdir, case_prompt, original.evaluation,
            )
            generation.emit("generation.stage", {"stage": "generating"})
            candidates: list[WorkflowSpec] = []
            candidate_errors: list[str] = []
            for index, strategy in enumerate(("minimal：删除冗余，只保留达成目标所需的最少节点", "balanced：兼顾简洁、可读性和必要容错", "robust：强调边界处理与稳定性，但避免无效堆叠")):
                prompt = CANDIDATE_PROMPT.format(
                    strategy=strategy, catalog=catalog_json,
                    workflow=json.dumps(original.model_dump(mode="json"), ensure_ascii=False), request=generation.prompt,
                )
                prompt = f"{WORKFLOW_CONTRACT}\n{prompt}"
                try:
                    candidate = WorkflowSpec.model_validate(_normalize_workflow_result(
                        self._invoke_result(generation, spec, command, workdir, prompt, f"候选工作流 {index + 1}"), generation.harness_agent_ids,
                    ))
                    if candidate.id != original.id:
                        raise RuntimeError("候选工作流修改了 workflow id")
                    candidate.evaluation = evaluation.model_copy(deep=True)
                    candidates.append(candidate)
                except Exception as exc:
                    candidate_errors.append(f"候选 {index + 1}：{exc}")
                    generation.emit("generation.candidate_failed", {"candidate": index + 1, "message": str(exc)})
            if not candidates:
                raise RuntimeError(f"所有候选工作流均生成失败：{'；'.join(candidate_errors)}")
            results = self._evaluate_candidates(generation, spec, candidates, evaluator)
            passing = [item for item in results if item.passed]
            max_repairs = max(0, min(int(os.environ.get("OPENCODE_OPTIMIZATION_REPAIR_ROUNDS", "2")), 5))
            for repair_round in range(1, max_repairs + 1):
                if passing:
                    break
                generation.emit("generation.stage", {"stage": "repairing", "round": repair_round})
                repaired: list[WorkflowSpec] = []
                repair_errors: list[str] = []
                for index, result in enumerate(results):
                    strategy = ("最小修改并修复根因", "重新组织数据流并提高可验证性", "强化边界和失败处理但保持简洁")[index % 3]
                    prompt = REPAIR_PROMPT.format(
                        round_number=repair_round, strategy=strategy,
                        workflow=json.dumps(result.workflow.model_dump(mode="json"), ensure_ascii=False),
                        feedback=json.dumps(self._result_feedback(result), ensure_ascii=False),
                        evaluation=json.dumps(evaluation.model_dump(mode="json"), ensure_ascii=False),
                        catalog=catalog_json, request=generation.prompt,
                    )
                    prompt = f"{WORKFLOW_CONTRACT}\n{prompt}"
                    try:
                        candidate = WorkflowSpec.model_validate(_normalize_workflow_result(
                            self._invoke_result(
                                generation, spec, command, workdir, prompt,
                                f"第 {repair_round} 轮修复候选 {index + 1}",
                                timeout_seconds=_repair_timeout_seconds(),
                            ), generation.harness_agent_ids,
                        ))
                        if candidate.id != original.id:
                            raise RuntimeError("修复候选修改了 workflow id")
                        candidate.evaluation = evaluation.model_copy(deep=True)
                        repaired.append(candidate)
                    except Exception as exc:
                        repair_errors.append(f"候选 {index + 1}：{exc}")
                        generation.emit("generation.repair_candidate_failed", {
                            "round": repair_round, "candidate": index + 1, "message": str(exc),
                        })
                if not repaired:
                    raise RuntimeError(f"第 {repair_round} 轮所有修复候选均无效：{'；'.join(repair_errors)}")
                results = self._evaluate_candidates(generation, spec, repaired, evaluator)
                passing = [item for item in results if item.passed]
            if not passing:
                raise RuntimeError(f"三个候选经过 {max_repairs} 轮修复后仍未通过 OpenCode 验证，原工作流和验收标准均已保留")
            winner = min(passing, key=lambda item: item.metrics).workflow
            generation.emit("generation.stage", {"stage": "saving"})
            generation.draft = winner.model_dump(mode="json")
            self._finalize(generation)
            if generation.events[-1]["event"] == "generation.completed":
                self.history.setdefault(generation.workflow_id, []).append({"role": "assistant", "content": "已自动验收并采用最短、最清晰且通过全部标准的工作流。"})
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
    ) -> str:
        command = [
            binary, "run", "--format", "json", "--agent", os.environ.get("OPENCODE_COMPACTION_AGENT", "compaction"),
            "--title", "OpenAgent内部上下文提炼",
            "--model", model,
        ]
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
        if generation.cancelled:
            raise RuntimeError("生成已取消")
        if timed_out.is_set():
            raise _CompactionTimeoutError(f"OpenCode 上下文提炼超时（{_compaction_timeout_seconds()} 秒）")
        if code != 0:
            detail = diagnostics[-1] if diagnostics else "没有返回错误详情"
            raise RuntimeError(f"OpenCode 内部上下文提炼失败，代码 {code}：{detail}")
        text = text.strip()
        if not text:
            raise RuntimeError("OpenCode 内部上下文提炼没有返回内容")
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
            compacted = self._compact_prompt(
                generation, base_command[0], generation.model, prompt, workdir,
                self._environment(spec),
            ).strip()
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
    ) -> str:
        if generation.cancelled:
            raise RuntimeError("生成已取消")
        environment = self._environment(spec)
        command_base = [*base_command, "--model", generation.model]
        prompt = self._prepare_prompt(generation, spec, base_command, workdir, prompt)
        current_process = subprocess.Popen(
            command_base, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, bufsize=1, env=environment, encoding="utf-8", errors="replace",
        )
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
        assistant_text, diagnostics = "", []
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
                if error := _extract_error(item):
                    diagnostics.append(error)
                text = _extract_text(item)
                if text:
                    assistant_text = text if text.startswith(assistant_text) else assistant_text + text
            code = current_process.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise _OpenCodeTimeoutError(f"OpenCode 单次调用超时（{call_timeout} 秒）")
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
        text = self._invoke_for_result(generation, spec, command, workdir, prompt, timeout_seconds)
        try:
            return _parse_result(text)
        except StructuredResultError:
            retry_prompt = (
                f"{prompt}\n\n你上一次没有返回可解析的结构化 JSON。请重新完成同一任务，只输出一个 "
                "<result>{合法 JSON}</result>，不要使用注释、尾随逗号、单引号或任何额外说明。"
            )
            retry_text = self._invoke_for_result(
                generation, spec, command, workdir, retry_prompt, timeout_seconds,
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
    ) -> str:
        """Invoke while remaining compatible with test/integration overrides."""
        if timeout_seconds is None:
            return self._invoke(generation, spec, command, workdir, prompt)
        try:
            return self._invoke(
                generation, spec, command, workdir, prompt,
                timeout_seconds=timeout_seconds,
            )
        except TypeError as exc:
            # Existing embedders may override _invoke with the historical
            # five-argument signature. Preserve that extension point.
            if "timeout_seconds" not in str(exc):
                raise
            return self._invoke(generation, spec, command, workdir, prompt)

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


def _invoke_timeout_seconds() -> int:
    try:
        # 120s keeps a single stalled model request from blocking the whole
        # three-candidate optimization for many minutes. Increase explicitly
        # with OPENCODE_GENERATOR_CALL_TIMEOUT when a slow model needs it.
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


def _normalize_workflow_result(value: Any, harness_agent_ids: set[str]) -> Any:
    """Accept a small set of common workflow aliases before strict validation."""
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
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
