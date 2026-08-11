from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import time
from typing import Any, Callable

import httpx
from agent_harness_sdk import HarnessAPIError

from .harness_client import DEFAULT_BACKEND_ID, create_harness_client
from .models import EvaluationAssertion, EvaluationCase, ProjectSpec, WorkflowSpec
from .workflow_runner import EvaluationPolicy, WorkflowManager, validate_executable_workflow


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    output: Any = None
    errors: list[str] = field(default_factory=list)
    semantic_score: int = 0
    opencode_verified: bool = False
    duration_seconds: float = 0


@dataclass
class CandidateResult:
    index: int
    workflow: WorkflowSpec
    passed: bool
    cases: list[CaseResult]
    metrics: tuple[int, int, int, int, int, float, int]
    errors: list[str] = field(default_factory=list)


@dataclass
class SemanticVerdict:
    passed: bool
    score: int
    issues: list[str] = field(default_factory=list)


class HarnessInfrastructureError(RuntimeError):
    """The acceptance runtime is unavailable, so candidate repair cannot help."""


def _uses_harness_runtime(project: ProjectSpec, workflow: WorkflowSpec) -> bool:
    harness_ids = {item.id for item in project.harness}
    return any(
        node.type in {"llm", "agent", "tool", "code", "validator"}
        and str(node.data.get("agent_id", "")) in harness_ids
        for node in workflow.nodes
    )


def lookup(value: Any, path: str) -> tuple[bool, Any]:
    if path in {"", "output"}:
        return True, value
    current = value
    for part in path.removeprefix("output.").split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def check_assertion(output: Any, assertion: EvaluationAssertion) -> str | None:
    found, actual = lookup(output, assertion.path)
    if assertion.operator == "exists":
        return None if found else f"{assertion.path} 不存在"
    if not found:
        return f"{assertion.path} 不存在"
    expected = assertion.expected
    if assertion.operator == "equals" and actual != expected:
        return f"{assertion.path} 不等于预期值"
    if assertion.operator == "contains":
        contains = expected in actual if isinstance(actual, (list, dict, str)) else False
        if not contains:
            return f"{assertion.path} 不包含预期值"
    if assertion.operator == "matches":
        try:
            matched = re.search(str(expected), str(actual)) is not None
        except re.error as exc:
            return f"正则表达式无效：{exc}"
        if not matched:
            return f"{assertion.path} 不匹配预期格式"
    if assertion.operator == "type":
        names = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "object": dict, "array": list, "null": type(None)}
        expected_type = names.get(str(expected).lower())
        if expected_type is None or not isinstance(actual, expected_type) or str(expected).lower() == "number" and isinstance(actual, bool):
            return f"{assertion.path} 类型不是 {expected}"
    return None


def complexity_metrics(workflow: WorkflowSpec, duration: float, index: int) -> tuple[int, int, int, int, int, float, int]:
    nodes = {node.id: node for node in workflow.nodes}
    incoming = {node_id: 0 for node_id in nodes}
    outgoing = {node_id: [] for node_id in nodes}
    for edge in workflow.edges:
        if edge.source in nodes and edge.target in nodes:
            incoming[edge.target] += 1
            outgoing[edge.source].append(edge.target)
    roots = [node_id for node_id, count in incoming.items() if count == 0]
    reachable: set[str] = set()
    depths = {node_id: 1 for node_id in roots}
    stack = list(roots)
    while stack:
        node_id = stack.pop()
        reachable.add(node_id)
        for target in outgoing[node_id]:
            depths[target] = max(depths.get(target, 1), depths[node_id] + 1)
            if target not in reachable:
                stack.append(target)
    clarity_penalty = sum(not str(node.data.get("description", "")).strip() for node in workflow.nodes)
    expensive = sum(node.type in {"llm", "agent", "tool", "code", "http_request", "subworkflow"} for node in workflow.nodes)
    return (len(reachable), max(depths.values(), default=0), len(workflow.edges), clarity_penalty, expensive, round(duration, 6), index)


class WorkflowEvaluator:
    def __init__(
        self,
        model_inference: Callable[[str, Any], Any],
        semantic_judge: Callable[[WorkflowSpec, EvaluationCase, Any], SemanticVerdict],
        *,
        live_execution: bool = True,
        harness_base_url: str | None = None,
    ):
        self.model_inference = model_inference
        self.semantic_judge = semantic_judge
        self.live_execution = live_execution
        self.harness_base_url = harness_base_url or os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765")

    def ensure_runtime_ready(self, project: ProjectSpec, workflow: WorkflowSpec) -> None:
        if not self.live_execution or not _uses_harness_runtime(project, workflow):
            return
        logical_ids = {
            str(node.data.get("agent_id", ""))
            for node in workflow.nodes
            if node.type in {"llm", "agent", "tool", "code", "validator"}
        }
        backend_ids = {item.backend_id for item in project.harness if item.id in logical_ids}
        self.ensure_harness_ready(backend_ids)

    def ensure_harness_ready(self, backend_ids: set[str] | None = None) -> None:
        if not self.live_execution:
            return
        for backend_id in sorted(backend_ids or {DEFAULT_BACKEND_ID}):
            client = None
            try:
                client = create_harness_client(
                    backend_id,
                    base_url=self.harness_base_url if backend_id == DEFAULT_BACKEND_ID else None,
                    timeout=2,
                )
                capabilities = client.capabilities()
                if str(capabilities.get("api", {}).get("selected_version", "1")) != "1":
                    raise HarnessInfrastructureError(
                        f"Harness 后端 {backend_id} 未协商到 API v1；候选工作流未进入无效修复。"
                    )
                self._ensure_task_agents(client, backend_id)
            except (httpx.HTTPError, HarnessAPIError, RuntimeError) as exc:
                if isinstance(exc, HarnessInfrastructureError):
                    raise
                raise HarnessInfrastructureError(
                    f"Harness 验收运行时不可用（backend={backend_id}）：{exc}。"
                    "请启动独立 my-harness 并检查 Studio 的 URL/任务 Token；候选工作流未进入无效修复。"
                ) from exc
            finally:
                if client is not None:
                    client.close()

    def _ensure_task_agents(self, client: Any, backend_id: str) -> None:
        discover = getattr(client, "task_agents", None)
        if discover is None:
            return
        expected = getattr(self, "task_agent_requirements", {})
        records = discover()
        by_id = {str(item.get("id")): item for item in records if isinstance(item, dict)}
        for agent_id, requirement in expected.items():
            descriptor = by_id.get(agent_id)
            if descriptor is None:
                raise HarnessInfrastructureError(f"Harness 后端 {backend_id} 缺少任务 Agent {agent_id}；请注册正确的 Agent")
            self._validate_task_descriptor(backend_id, descriptor, requirement)

    @staticmethod
    def _validate_task_descriptor(backend_id: str, descriptor: dict[str, Any], requirement: dict[str, Any]) -> None:
        readiness = descriptor.get("readiness") or {}
        state = readiness.get("state")
        if not descriptor.get("enabled", False):
            raise HarnessInfrastructureError(f"Harness 后端 {backend_id} 的任务 Agent 已禁用")
        if state != "ready" or not descriptor.get("accepts_tasks", False):
            code = readiness.get("error_code") or state or "not_ready"
            raise HarnessInfrastructureError(f"Harness 后端 {backend_id} 的任务 Agent 未就绪（{code}）；请先完成 setup")
        expected_protocol = requirement.get("protocol")
        actual_protocol = (descriptor.get("protocol") or {}).get("kind")
        if expected_protocol and actual_protocol != expected_protocol:
            raise HarnessInfrastructureError(f"Harness 后端 {backend_id} 的任务协议不匹配（需要 {expected_protocol}，实际 {actual_protocol or 'unknown'}）")
        expected_labels = requirement.get("labels") or {}
        labels = descriptor.get("labels") or {}
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            raise HarnessInfrastructureError(f"Harness 后端 {backend_id} 的任务 Agent 身份标签不匹配；请更新注册配置")

    def evaluate(
        self,
        project: ProjectSpec,
        workflow: WorkflowSpec,
        index: int,
        *,
        case_ids: set[str] | None = None,
    ) -> CandidateResult:
        validation = validate_executable_workflow(project, workflow, runtime=True)
        if validation:
            return CandidateResult(index, workflow, False, [], complexity_metrics(workflow, 0, index), validation)
        self.ensure_runtime_ready(project, workflow)
        case_results: list[CaseResult] = []
        started = time.monotonic()
        cases = [
            case for case in workflow.evaluation.cases
            if case.enabled and (case_ids is None or case.id in case_ids)
        ]
        if not cases:
            message = "没有匹配的失败验收用例" if case_ids is not None else "没有启用的验收用例"
            return CandidateResult(index, workflow, False, [], complexity_metrics(workflow, 0, index), [message])
        for case in cases:
            case_started = time.monotonic()
            manager = WorkflowManager(base_url=self.harness_base_url, poll_interval=0.1 if self.live_execution else 0.01)
            policy = EvaluationPolicy(
                mocks={item.node_id: item.response for item in case.mocks}, approvals=case.approvals,
                model_inference=self.model_inference, live_execution=self.live_execution,
            )
            try:
                run = manager.start(project, workflow.id, {"input": case.input}, policy=policy, record=False)
            except (RuntimeError, ValueError) as exc:
                case_results.append(CaseResult(case.id, False, errors=[str(exc)]))
                continue
            deadline = time.monotonic() + case.timeout_seconds
            while run.status not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
                time.sleep(0.01)
            if run.status not in {"completed", "failed", "cancelled"}:
                run.cancel_event.set()
                result = CaseResult(case.id, False, errors=["验收执行超时"])
            elif run.status != "completed":
                if run.error_code in {"setup_required", "agent_process_failed", "agent_timeout", "agent_permission_denied", "sandbox_unavailable", "sandbox_denied", "protocol_output_invalid"} or run.error.startswith((
                    "Harness 不可用：",
                    "Harness 基础设施错误：",
                    "Harness 任务 API 契约不兼容：",
                    "Harness 请求失败（502）",
                    "Harness 请求失败（503）",
                    "Harness 请求失败（504）",
                )):
                    raise HarnessInfrastructureError(
                        f"Harness 验收基础设施失败：{run.error}。候选工作流未进入无效修复。"
                    )
                result = CaseResult(case.id, False, errors=[run.error or run.status])
            else:
                final_ids = [node.id for node in workflow.nodes if node.type == "output" and node.id in run.outputs]
                if len(final_ids) == 1:
                    output = run.outputs[final_ids[0]]
                elif final_ids:
                    output = {node_id: run.outputs[node_id] for node_id in final_ids}
                else:
                    output = run.outputs
                errors = [error for assertion in case.assertions if (error := check_assertion(output, assertion))]
                verdict = self.semantic_judge(workflow, case, output)
                if not verdict.passed:
                    errors.extend(verdict.issues or ["OpenCode 未确认该用例通过"])
                if verdict.score < 80:
                    errors.append(f"语义质量分 {verdict.score} 低于 80")
                result = CaseResult(case.id, not errors and verdict.passed and verdict.score >= 80, output, errors, verdict.score, verdict.passed)
            result.duration_seconds = time.monotonic() - case_started
            case_results.append(result)
        duration = time.monotonic() - started
        passed = bool(case_results) and all(item.passed for item in case_results)
        return CandidateResult(index, workflow, passed, case_results, complexity_metrics(workflow, duration, index))
