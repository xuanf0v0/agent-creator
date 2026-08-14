from __future__ import annotations

import json
import ipaddress
import re
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from agent_harness_sdk import HarnessAPIError, HarnessClient

from .harness_client import DEFAULT_BACKEND_ID, create_harness_client
from .models import HarnessSpec, ProjectSpec, WorkflowEdge, WorkflowNode, WorkflowSpec


TERMINAL_TASK_STATES = {"completed", "failed", "blocked", "cancelled"}
TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}


class RunCancelled(Exception):
    pass


class HarnessContractError(RuntimeError):
    pass


@dataclass
class WorkflowRun:
    id: str
    workflow_id: str
    input: Any
    relative_path: str = "."
    status: str = "queued"
    node_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_sequence: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    error: str = ""
    error_code: str = ""
    depth: int = 0
    trigger_node_id: str | None = None
    active_task_ids: set[str] = field(default_factory=set)
    active_task_backends: dict[str, str] = field(default_factory=dict, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    event_signal: threading.Condition = field(default_factory=threading.Condition, repr=False)
    approval_signals: dict[str, threading.Event] = field(default_factory=dict, repr=False)
    approval_decisions: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    policy: "EvaluationPolicy | None" = field(default=None, repr=False)

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "input": self.input,
            "node_states": self.node_states,
            "outputs": self.outputs,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "error_code": self.error_code,
            "waiting_approvals": [node_id for node_id, state in self.node_states.items() if state.get("status") == "waiting"],
        }


@dataclass
class EvaluationPolicy:
    mocks: dict[str, Any] = field(default_factory=dict)
    approvals: dict[str, bool] = field(default_factory=dict)
    model_inference: Callable[[str, Any], Any] | None = None
    live_execution: bool = False


class WorkflowManager:
    def __init__(
        self,
        base_url: str | None = None,
        poll_interval: float = 0.5,
        max_workers: int = 8,
        client_factory: Callable[[str], HarnessClient] | None = None,
        max_retained_runs: int = 200,
        max_run_events: int = 1000,
        max_http_response_bytes: int = 5 * 1024 * 1024,
        max_concurrent_runs: int = 8,
        max_harness_log_items: int = 500,
        max_harness_log_bytes: int = 2 * 1024 * 1024,
        max_harness_log_line_bytes: int = 64 * 1024,
        max_harness_result_bytes: int = 2 * 1024 * 1024,
        max_harness_text_bytes: int = 1024 * 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.client_factory = client_factory or (
            lambda backend_id: create_harness_client(
                backend_id,
                base_url=self.base_url if backend_id == DEFAULT_BACKEND_ID else None,
            )
        )
        self.poll_interval = poll_interval
        self.max_workers = max_workers
        self.max_retained_runs = max(1, max_retained_runs)
        self.max_run_events = max(1, max_run_events)
        self.max_http_response_bytes = max(1, max_http_response_bytes)
        self.max_concurrent_runs = max(1, max_concurrent_runs)
        self.max_harness_log_items = max(1, max_harness_log_items)
        self.max_harness_log_bytes = max(1, max_harness_log_bytes)
        self.max_harness_log_line_bytes = max(1, max_harness_log_line_bytes)
        self.max_harness_result_bytes = max(1, max_harness_result_bytes)
        self.max_harness_text_bytes = max(1, max_harness_text_bytes)
        self.runs: dict[str, WorkflowRun] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="workflow-node")
        self._run_executor = ThreadPoolExecutor(max_workers=self.max_concurrent_runs, thread_name_prefix="workflow-run")
        self._run_slots = threading.BoundedSemaphore(self.max_concurrent_runs)
        self._http_client = httpx.Client(follow_redirects=False)
        self._schedule_stop = threading.Event()
        self._schedule_thread: threading.Thread | None = None
        self._schedule_last: dict[str, str] = {}

    def start_scheduler(self, project_loader: Any) -> None:
        if self._schedule_thread and self._schedule_thread.is_alive():
            return
        self._schedule_stop.clear()
        self._schedule_thread = threading.Thread(target=self._schedule_loop, args=(project_loader,), daemon=True, name="workflow-scheduler")
        self._schedule_thread.start()

    def stop_scheduler(self) -> None:
        self._schedule_stop.set()
        if self._schedule_thread:
            self._schedule_thread.join(timeout=2)
        self._run_executor.shutdown(wait=False, cancel_futures=True)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._http_client.close()

    def _schedule_loop(self, project_loader: Any) -> None:
        while not self._schedule_stop.wait(1):
            try:
                project = project_loader()
                for workflow in project.workflows:
                    for node in workflow.nodes:
                        if node.type != "schedule":
                            continue
                        timezone = str(node.data.get("timezone", "UTC"))
                        try:
                            now = datetime.now(ZoneInfo(timezone))
                        except ZoneInfoNotFoundError:
                            continue
                        minute_key = now.strftime("%Y-%m-%dT%H:%M%z")
                        key = f"{workflow.id}:{node.id}"
                        if self._schedule_last.get(key) == minute_key or not _cron_matches(str(node.data.get("cron", "")), now):
                            continue
                        self._schedule_last[key] = minute_key
                        self.start(project, workflow.id, {"input": {"trigger": "schedule", "node_id": node.id, "scheduled_at": now.isoformat()}, "_trigger_node_id": node.id})
            except Exception:
                continue

    def start(
        self,
        project: ProjectSpec,
        workflow_id: str,
        body: dict[str, Any] | None = None,
        *,
        policy: EvaluationPolicy | None = None,
        record: bool = True,
        require_output: bool = True,
        _inline: bool = False,
    ) -> WorkflowRun:
        workflow = next((item for item in project.workflows if item.id == workflow_id), None)
        if workflow is None:
            raise KeyError(workflow_id)
        errors = validate_executable_workflow(project, workflow, runtime=True, require_output=require_output)
        if errors:
            raise ValueError("；".join(errors))
        body = body or {}
        # 验收/增量探测可使用空的合成输入；外部人工启动必须提供真实输入。
        if policy is None and not _inline and self._manual_trigger_requires_input(workflow, body):
            if not self._has_manual_trigger_input(body.get("input")):
                raise ValueError("手动触发器必须填写输入信息后才能启动工作流")
        slot_acquired = False
        if not _inline:
            slot_acquired = self._run_slots.acquire(blocking=False)
            if not slot_acquired:
                raise RuntimeError(f"工作流运行已达到 {self.max_concurrent_runs} 个并发上限，请稍后重试")
        try:
            run = WorkflowRun(
                id=uuid4().hex,
                workflow_id=workflow_id,
                input=body.get("input", ""),
                relative_path=str(body.get("relative_path", ".")),
                depth=int(body.get("_depth", 0)),
                trigger_node_id=str(body["_trigger_node_id"]) if body.get("_trigger_node_id") else None,
                node_states={node.id: {"status": "pending"} for node in workflow.nodes},
                policy=policy,
            )
            if record:
                with self._lock:
                    self.runs[run.id] = run
                    self._trim_runs_locked()
            self._emit(run, "run.queued", {"run": run.payload()})
            if _inline:
                self._execute(run, project, workflow)
            else:
                self._run_executor.submit(self._execute_coordinated, run, project, workflow)
            return run
        except BaseException:
            if slot_acquired:
                self._run_slots.release()
            raise

    @staticmethod
    def _manual_trigger_requires_input(workflow: WorkflowSpec, body: dict[str, Any]) -> bool:
        trigger_node_id = body.get("_trigger_node_id")
        if trigger_node_id:
            trigger = next((node for node in workflow.nodes if node.id == str(trigger_node_id)), None)
            return trigger is not None and trigger.type == "manual_trigger"
        return any(node.type == "manual_trigger" for node in workflow.nodes)

    @staticmethod
    def _has_manual_trigger_input(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (dict, list, tuple, set)):
            return bool(value)
        return True

    def _trim_runs_locked(self) -> None:
        if len(self.runs) <= self.max_retained_runs:
            return
        terminal = sorted(
            (run for run in self.runs.values() if run.status in TERMINAL_RUN_STATES),
            key=lambda run: run.completed_at or run.created_at,
        )
        for run in terminal[:max(0, len(self.runs) - self.max_retained_runs)]:
            self.runs.pop(run.id, None)

    def require(self, run_id: str) -> WorkflowRun:
        with self._lock:
            run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def cancel(self, run_id: str) -> WorkflowRun:
        run = self.require(run_id)
        if run.status in TERMINAL_RUN_STATES:
            return run
        run.cancel_event.set()
        for signal in run.approval_signals.values():
            signal.set()
        for task_id in list(run.active_task_ids):
            try:
                backend_id = run.active_task_backends.get(task_id, DEFAULT_BACKEND_ID)
                client = self.client_factory(backend_id)
                try:
                    self._cancel_task_idempotent(client, task_id)
                finally:
                    client.close()
            except (HarnessAPIError, httpx.HTTPError, RuntimeError):
                pass
        self._emit(run, "run.cancelling", {"run_id": run.id})
        return run

    @staticmethod
    def _cancel_task_idempotent(client: Any, task_id: str) -> None:
        """幂等取消：已终态的任务直接跳过，避免触发 Harness 对终态任务 cancel 的非幂等错误。"""
        try:
            task = client.task(task_id)
            if str(task.get("status")) in TERMINAL_TASK_STATES:
                return
        except (HarnessAPIError, httpx.HTTPError):
            return
        try:
            client.cancel(task_id)
        except (HarnessAPIError, httpx.HTTPError, RuntimeError):
            pass

    def approve(self, run_id: str, node_id: str, approved: bool, comment: str = "") -> WorkflowRun:
        run = self.require(run_id)
        signal = run.approval_signals.get(node_id)
        if signal is None or run.node_states.get(node_id, {}).get("status") != "waiting":
            raise ValueError("该节点当前不在等待审批")
        run.approval_decisions[node_id] = {"approved": approved, "comment": comment}
        signal.set()
        return run

    def _execute_coordinated(self, run: WorkflowRun, project: ProjectSpec, workflow: WorkflowSpec) -> None:
        try:
            self._execute(run, project, workflow)
        finally:
            self._run_slots.release()

    def _execute(self, run: WorkflowRun, project: ProjectSpec, workflow: WorkflowSpec) -> None:
        run.status, run.started_at = "running", time.time()
        self._emit(run, "run.started", {"run": run.payload()})
        nodes = {node.id: node for node in workflow.nodes}
        incoming = {node_id: [] for node_id in nodes}
        outgoing = {node_id: [] for node_id in nodes}
        for index, edge in enumerate(workflow.edges):
            incoming[edge.target].append(index)
            outgoing[edge.source].append(index)
        edge_states: list[str] = ["pending"] * len(workflow.edges)
        futures: dict[Future[Any], str] = {}
        completed: set[str] = set()
        scheduled: set[str] = set()

        try:
            pool = None if run.depth else self._executor
            while len(completed) < len(nodes):
                    self._check_cancelled(run)
                    progressed = False
                    for node_id, node in nodes.items():
                        if node_id in scheduled or node_id in completed:
                            continue
                        inbound = incoming[node_id]
                        if not inbound and node.type in {"manual_trigger", "webhook", "schedule"}:
                            selected = node_id == run.trigger_node_id if run.trigger_node_id else node.type == "manual_trigger"
                            if not selected:
                                self._set_node_state(run, node_id, "skipped")
                                completed.add(node_id)
                                scheduled.add(node_id)
                                for index in outgoing[node_id]:
                                    edge_states[index] = "inactive"
                                progressed = True
                                continue
                        if inbound and any(edge_states[index] == "pending" for index in inbound):
                            continue
                        if inbound and not any(edge_states[index] == "active" for index in inbound):
                            self._set_node_state(run, node_id, "skipped")
                            completed.add(node_id)
                            for index in outgoing[node_id]:
                                edge_states[index] = "inactive"
                            progressed = True
                            continue
                        scheduled.add(node_id)
                        if run.depth:
                            future: Future[Any] = Future()
                            try:
                                future.set_result(self._run_node(run, project, node, incoming[node_id], workflow.edges, edge_states))
                            except BaseException as exc:
                                future.set_exception(exc)
                        else:
                            future = pool.submit(self._run_node, run, project, node, incoming[node_id], workflow.edges, edge_states)
                        futures[future] = node_id
                        progressed = True

                    if not futures:
                        if len(completed) == len(nodes):
                            break
                        if not progressed:
                            raise RuntimeError("工作流无法继续调度，请检查循环或不可达连线")
                        continue

                    done, _ = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        node_id = futures.pop(future)
                        try:
                            output = future.result()
                        except Exception:
                            run.cancel_event.set()
                            for signal in run.approval_signals.values():
                                signal.set()
                            raise
                        run.outputs[node_id] = output
                        completed.add(node_id)
                        node = nodes[node_id]
                        for index in outgoing[node_id]:
                            edge = workflow.edges[index]
                            if node.type == "approval" and isinstance(output, dict) and "approved" in output:
                                matches = self._approval_edge_matches(
                                    edge,
                                    output,
                                    run,
                                    target_type=nodes[edge.target].type,
                                )
                            else:
                                matches = self._edge_matches(edge, output, run)
                            edge_states[index] = "active" if matches else "inactive"

            self._check_cancelled(run)
            run.status, run.completed_at = "completed", time.time()
            final_ids = [node.id for node in workflow.nodes if node.type == "output"]
            final = {node_id: run.outputs.get(node_id) for node_id in final_ids} or run.outputs
            self._emit(run, "run.completed", {"run": run.payload(), "output": final})
        except RunCancelled:
            run.status, run.completed_at = "cancelled", time.time()
            self._emit(run, "run.cancelled", {"run": run.payload()})
        except Exception as exc:
            run.status, run.error, run.completed_at = "failed", str(exc), time.time()
            self._emit(run, "run.failed", {"run": run.payload(), "message": str(exc)})
        finally:
            with self._lock:
                self._trim_runs_locked()

    def _run_node(
        self,
        run: WorkflowRun,
        project: ProjectSpec,
        node: WorkflowNode,
        inbound: list[int],
        edges: list[WorkflowEdge],
        edge_states: list[str],
    ) -> Any:
        self._check_cancelled(run)
        self._set_node_state(run, node.id, "running", started_at=time.time())
        inputs = [run.outputs.get(edges[index].source) for index in inbound if edge_states[index] == "active"]
        latest = inputs[-1] if inputs else run.input
        retries = max(0, min(int(node.data.get("retry_count", 0)), 5))
        output: Any = None
        for attempt in range(retries + 1):
            try:
                output = self._execute_node(run, project, node, inputs, latest)
                break
            except RunCancelled:
                raise
            except Exception as exc:
                if attempt < retries:
                    self._emit(run, "node.retry", {"run_id": run.id, "node_id": node.id, "attempt": attempt + 1, "message": str(exc)})
                    delay = max(0.0, min(float(node.data.get("retry_delay_seconds", 1)), 30.0))
                    if run.cancel_event.wait(delay):
                        raise RunCancelled()
                    continue
                if node.data.get("on_error") == "continue":
                    output = node.data.get("fallback_value")
                    self._set_node_state(run, node.id, "completed", warning=str(exc), output=output, completed_at=time.time())
                    return output
                self._set_node_state(run, node.id, "failed", error=str(exc), completed_at=time.time())
                raise
        self._set_node_state(run, node.id, "completed", output=output, completed_at=time.time())
        return output

    def _execute_node(self, run: WorkflowRun, project: ProjectSpec, node: WorkflowNode, inputs: list[Any], latest: Any) -> Any:
        if node.type in {"manual_trigger", "webhook", "schedule"}:
            return run.input
        if node.type == "prompt":
            return self._render(str(node.data.get("template") or node.data.get("prompt") or node.data.get("description") or "{{input}}"), run, latest)
        if node.type in {"llm", "agent", "tool", "code", "validator"}:
            if run.policy and node.id in run.policy.mocks:
                return run.policy.mocks[node.id]
            if run.policy and not run.policy.live_execution:
                if node.type in {"tool", "code"}:
                    return self._evaluation_mock(run, node)
                if run.policy.model_inference is None:
                    raise RuntimeError("评估模式未配置模型推理器")
                prompt = self._render(str(node.data.get("prompt") or node.data.get("description") or "{{latest}}"), run, latest)
                return run.policy.model_inference(prompt, latest)
            return self._agent_or_validator(run, project, node, latest)
        if node.type == "knowledge_retrieval":
            return self._retrieve_knowledge(run, node, latest)
        if node.type == "http_request":
            if run.policy and node.id in run.policy.mocks:
                return run.policy.mocks[node.id]
            if run.policy and not run.policy.live_execution:
                return self._evaluation_mock(run, node)
            return self._http_request(run, node, latest)
        if node.type == "variable_set":
            values = node.data.get("variables", {})
            if not isinstance(values, dict):
                raise RuntimeError("变量赋值节点的 variables 必须是对象")
            return {key: self._render(str(value), run, latest) if isinstance(value, str) else value for key, value in values.items()}
        if node.type == "transform":
            return self._transform(node, latest)
        if node.type == "merge":
            return self._merge(node, inputs, latest)
        if node.type == "condition":
            return self._evaluate(str(node.data.get("expression", "")), run, latest)
        if node.type == "switch":
            cases = node.data.get("cases", [])
            if not isinstance(cases, list):
                raise RuntimeError("分支节点的 cases 必须是数组")
            for case in cases:
                if isinstance(case, dict) and self._evaluate(str(case.get("expression", "")), run, latest):
                    return str(case.get("value", ""))
            return str(node.data.get("default_case", "default"))
        if node.type == "parallel":
            return inputs if len(inputs) > 1 else latest
        if node.type in {"iteration", "loop"}:
            count = max(1, min(int(node.data.get("iterations", 1)), 100))
            template = str(node.data.get("template") or "{{latest}}")
            return [self._render(template.replace("{{index}}", str(index)), run, latest) for index in range(count)]
        if node.type == "approval":
            if run.policy:
                approved = run.policy.approvals.get(node.id, True)
                return {"approved": approved, "comment": "evaluation fixture", "value": latest}
            return self._wait_for_approval(run, node, latest)
        if node.type == "subworkflow":
            return self._run_subworkflow(run, project, node, latest)
        if node.type == "delay":
            if run.policy and not run.policy.live_execution:
                return latest
            seconds = max(0.0, min(float(node.data.get("seconds", 1)), 300.0))
            if run.cancel_event.wait(seconds):
                raise RunCancelled()
            return latest
        if node.type == "output":
            template = str(node.data.get("template", ""))
            return self._render(template, run, latest) if template else _display_harness_output(latest)
        raise RuntimeError(f"不支持的节点类型：{node.type}")

    @staticmethod
    def _evaluation_mock(run: WorkflowRun, node: WorkflowNode) -> Any:
        assert run.policy is not None
        if node.id not in run.policy.mocks:
            raise RuntimeError(f"评估模式禁止真实副作用，节点 {node.id} 缺少 mock")
        return run.policy.mocks[node.id]

    def _retrieve_knowledge(self, run: WorkflowRun, node: WorkflowNode, latest: Any) -> dict[str, Any]:
        documents = node.data.get("documents", [])
        if not isinstance(documents, list):
            raise RuntimeError("知识检索节点的 documents 必须是数组")
        query = self._render(str(node.data.get("query") or "{{latest}}"), run, latest)
        terms = set(re.findall(r"[\w\u4e00-\u9fff]+", query.lower()))
        scored: list[tuple[int, dict[str, Any]]] = []
        for index, item in enumerate(documents):
            document = item if isinstance(item, dict) else {"id": str(index), "content": str(item)}
            content = str(document.get("content", ""))
            words = set(re.findall(r"[\w\u4e00-\u9fff]+", content.lower()))
            scored.append((len(terms & words), {**document, "score": len(terms & words)}))
        top_k = max(1, min(int(node.data.get("top_k", 3)), 20))
        matches = [item for score, item in sorted(scored, key=lambda pair: pair[0], reverse=True) if score > 0][:top_k]
        return {"query": query, "matches": matches}

    def _http_request(self, run: WorkflowRun, node: WorkflowNode, latest: Any) -> dict[str, Any]:
        url = self._render(str(node.data.get("url", "")), run, latest)
        self._validate_http_url(url, bool(node.data.get("allow_private", False)))
        method = str(node.data.get("method", "GET")).upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise RuntimeError(f"不支持的 HTTP 方法：{method}")
        headers = node.data.get("headers", {})
        if not isinstance(headers, dict):
            raise RuntimeError("HTTP headers 必须是对象")
        rendered_headers = {str(key): self._render(str(value), run, latest) for key, value in headers.items()}
        body = node.data.get("body")
        if isinstance(body, str):
            rendered = self._render(body, run, latest)
            try:
                body = json.loads(rendered)
            except json.JSONDecodeError:
                body = rendered
        elif isinstance(body, (dict, list)):
            body = self._render_body_value(body, run, latest)
        timeout = max(1.0, min(float(node.data.get("timeout_seconds", 30)), 120.0))
        try:
            with self._http_client.stream(
                method,
                url,
                headers=rendered_headers,
                json=body if not isinstance(body, str) else None,
                content=body if isinstance(body, str) else None,
                timeout=timeout,
            ) as response:
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > self.max_http_response_bytes:
                            raise RuntimeError(f"HTTP 响应超过 {self.max_http_response_bytes} 字节限制")
                    except ValueError:
                        pass
                raw_buffer = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw_buffer) + len(chunk) > self.max_http_response_bytes:
                        raise RuntimeError(f"HTTP 响应超过 {self.max_http_response_bytes} 字节限制")
                    raw_buffer.extend(chunk)
                raw = bytes(raw_buffer)
                status_code = response.status_code
                response_headers = dict(response.headers)
                encoding = response.encoding or "utf-8"
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP 请求失败：{exc}") from exc
        content_type = response_headers.get("content-type", "")
        try:
            result: Any = json.loads(raw) if "json" in content_type else raw.decode(encoding, errors="replace")
        except (ValueError, UnicodeError):
            result = raw.decode(encoding, errors="replace")
        payload = {"status": status_code, "headers": response_headers, "body": result}
        if bool(node.data.get("fail_on_error", True)) and status_code >= 400:
            raise RuntimeError(f"HTTP 请求返回 {status_code}：{str(result)[:500]}")
        return payload

    @staticmethod
    def _validate_http_url(url: str, allow_private: bool) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise RuntimeError("HTTP 节点需要有效的 http/https URL")
        if parsed.scheme != "https" and not allow_private:
            raise RuntimeError("默认只允许 HTTPS；访问受信任的内网 HTTP 时请显式启用 allow_private")
        if allow_private:
            return
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise RuntimeError(f"无法解析 HTTP 主机：{parsed.hostname}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise RuntimeError("HTTP 节点默认禁止访问私网、回环或保留地址")

    @staticmethod
    def _transform(node: WorkflowNode, latest: Any) -> Any:
        operation = str(node.data.get("operation", "json_stringify"))
        if operation == "json_parse":
            return json.loads(latest if isinstance(latest, str) else str(latest))
        if operation == "json_stringify":
            return json.dumps(latest, ensure_ascii=False)
        if operation == "extract":
            return _lookup(latest, str(node.data.get("path", "")))
        if operation == "pick":
            fields = node.data.get("fields", [])
            if not isinstance(latest, dict) or not isinstance(fields, list):
                raise RuntimeError("pick 操作要求对象输入和 fields 数组")
            return {str(key): latest.get(str(key)) for key in fields}
        if operation == "flatten":
            if not isinstance(latest, list):
                raise RuntimeError("flatten 操作要求数组输入")
            return [value for group in latest for value in (group if isinstance(group, list) else [group])]
        raise RuntimeError(f"不支持的数据转换操作：{operation}")

    @staticmethod
    def _merge(node: WorkflowNode, inputs: list[Any], latest: Any) -> Any:
        mode = str(node.data.get("mode", "array"))
        values = inputs or [latest]
        if mode == "array":
            return values
        if mode == "concat":
            separator = str(node.data.get("separator", "\n"))
            return separator.join(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) for value in values)
        if mode == "object":
            result: dict[str, Any] = {}
            for value in values:
                if not isinstance(value, dict):
                    raise RuntimeError("object 合并模式要求所有输入都是对象")
                result.update(value)
            return result
        raise RuntimeError(f"不支持的合并模式：{mode}")

    def _run_subworkflow(self, run: WorkflowRun, project: ProjectSpec, node: WorkflowNode, latest: Any) -> Any:
        if run.depth >= 10:
            raise RuntimeError("子工作流嵌套不能超过 10 层")
        workflow_id = str(node.data.get("workflow_id", ""))
        if not workflow_id or workflow_id == run.workflow_id:
            raise RuntimeError("子工作流节点需要选择其他工作流")
        template = str(node.data.get("input_template", ""))
        child_input: Any = self._render(template, run, latest) if template else latest
        child = self.start(
            project, workflow_id,
            {"input": child_input, "relative_path": run.relative_path, "_depth": run.depth + 1},
            policy=run.policy, record=run.policy is None, _inline=True,
        )
        if run.cancel_event.is_set():
            raise RunCancelled()
        if child.status != "completed":
            raise RuntimeError(f"子工作流 {workflow_id} 执行失败：{child.error or child.status}")
        return child.outputs

    def _agent_or_validator(self, run: WorkflowRun, project: ProjectSpec, node: WorkflowNode, latest: Any) -> Any:
        agent_id = str(node.data.get("agent_id", ""))
        if node.type == "validator" and not agent_id:
            if isinstance(latest, dict) and latest.get("task", {}).get("status") == "completed":
                return {"valid": True, "task": latest["task"], "logs": latest.get("logs", [])}
            expression = str(node.data.get("expression", ""))
            if expression and self._evaluate(expression, run, latest):
                return {"valid": True, "value": latest}
            raise RuntimeError("验证器需要已完成的 Harness 任务、agent_id 或可通过的 expression")
        if not agent_id:
            raise RuntimeError(f"节点 {node.id} 未选择智能体")
        harness = next((item for item in project.harness if item.id == agent_id), None)
        if harness is None:
            raise RuntimeError(f"智能体 {agent_id} 没有 Harness 配置")
        prompt = self._render(str(node.data.get("prompt") or node.data.get("description") or "{{input}}"), run, latest)
        configured_inputs = node.data.get("inputs")
        if configured_inputs is not None:
            if not isinstance(configured_inputs, dict):
                raise RuntimeError(f"节点 {node.id} 的 inputs 必须是对象")
            rendered_inputs = {
                str(key): self._render(value, run, latest) if isinstance(value, str) else value
                for key, value in configured_inputs.items()
            }
            prompt = f"{prompt}\n\n输入数据：\n{json.dumps(rendered_inputs, ensure_ascii=False)}"
        return self._run_harness_task(run, node, harness, prompt)

    def _run_harness_task(self, run: WorkflowRun, node: WorkflowNode, harness: HarnessSpec, prompt: str) -> dict[str, Any]:
        client = self.client_factory(harness.backend_id)
        try:
            task = client.submit(
                harness.agent_id or harness.id,
                str(node.data.get("title") or node.data.get("description") or node.id),
                prompt,
                relative_path=str(node.data.get("relative_path") or run.relative_path),
                metadata={"workflow_run_id": run.id, "workflow_node_id": node.id},
                idempotency_key=f"openagent-{run.id}-{node.id}",
            )
        except HarnessAPIError as exc:
            client.close()
            if exc.status_code in {404, 405} and exc.code == "invalid_error_response":
                raise HarnessContractError(
                    f"Harness 任务 API 契约不兼容：SDK 调用 /api/v1/tasks 返回 {exc.status_code}。"
                    "请确认运行的是固定版本 my-harness；候选工作流无法修复此问题"
                ) from exc
            raise RuntimeError(f"Harness 基础设施错误：{exc}") from exc
        except httpx.HTTPError as exc:
            client.close()
            raise RuntimeError(f"Harness 不可用：{exc}") from exc
        try:
            task_id = str(task["id"])
        except (KeyError, TypeError) as exc:
            client.close()
            raise HarnessContractError("Harness v1 submit 响应缺少 task id") from exc
        run.active_task_ids.add(task_id)
        run.active_task_backends[task_id] = harness.backend_id
        self._emit(run, "node.harness_task", {"run_id": run.id, "node_id": node.id, "task": task})
        try:
            while task.get("status") not in TERMINAL_TASK_STATES:
                self._check_cancelled(run)
                time.sleep(self.poll_interval)
                task = client.task(task_id)
                self._emit(run, "node.progress", {"run_id": run.id, "node_id": node.id, "status": task.get("status"), "task_id": task_id})
        except RunCancelled:
            try:
                self._cancel_task_idempotent(client, task_id)
            except (HarnessAPIError, httpx.HTTPError, RuntimeError):
                pass
            client.close()
            raise
        except HarnessAPIError as exc:
            client.close()
            raise RuntimeError(f"Harness 基础设施错误：{exc}") from exc
        except httpx.HTTPError as exc:
            client.close()
            raise RuntimeError(f"Harness 不可用：{exc}") from exc
        finally:
            run.active_task_ids.discard(task_id)
            run.active_task_backends.pop(task_id, None)
        if task.get("status") != "completed":
            error = task.get("error") if isinstance(task.get("error"), dict) else {}
            reason = error.get("message") or task.get("blocked_reason") or f"Harness 任务状态为 {task.get('status')}"
            code = str(error.get("code") or task.get("error_code") or "task_failed")
            client.close()
            infrastructure_codes = {"setup_required", "agent_process_failed", "agent_timeout", "agent_permission_denied", "sandbox_unavailable", "sandbox_denied", "protocol_output_invalid"}
            if code in infrastructure_codes:
                run.error_code = code
                if code == "setup_required":
                    raise RuntimeError(str(reason))
                raise RuntimeError(f"Harness 基础设施错误：code={code}；{reason}")
            raise RuntimeError(f"Harness 任务错误（code={code}）：{reason}")
        try:
            logs_response = client.logs(task_id, cursor=0, limit=self.max_harness_log_items)
            raw_logs = logs_response.get("items", []) if isinstance(logs_response, dict) else []
            logs, logs_truncated = self._bounded_harness_logs(raw_logs)
            result = client.result(task_id)
            output = result.get("output") if isinstance(result, dict) else None
            raw_text = output.get("text", "") if isinstance(output, dict) and output.get("type") == "text" else ""
            text, text_truncated = self._truncate_utf8(str(raw_text), self.max_harness_text_bytes)
            if not text:
                text, fallback_truncated = self._truncate_utf8(
                    "\n".join(str(item.get("line", "")) for item in logs),
                    self.max_harness_text_bytes,
                )
                text_truncated = text_truncated or fallback_truncated
            bounded_result = self._bounded_json_value(result, self.max_harness_result_bytes)
            return {
                "task": self._bounded_json_value(task, self.max_harness_result_bytes),
                "result": bounded_result,
                "logs": logs,
                "text": text,
                "truncated": {
                    "logs": logs_truncated,
                    "result": bounded_result is not result,
                    "text": text_truncated,
                },
            }
        except HarnessAPIError as exc:
            raise RuntimeError(f"Harness 基础设施错误：{exc}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Harness 不可用：{exc}") from exc
        finally:
            client.close()

    @staticmethod
    def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= limit:
            return value, False
        return encoded[:limit].decode("utf-8", errors="ignore"), True

    def _bounded_harness_logs(self, raw_logs: Any) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(raw_logs, list):
            return [], bool(raw_logs)
        bounded: list[dict[str, Any]] = []
        used = 0
        truncated = len(raw_logs) > self.max_harness_log_items
        for raw_item in raw_logs[:self.max_harness_log_items]:
            item = dict(raw_item) if isinstance(raw_item, dict) else {"line": str(raw_item)}
            line, line_truncated = self._truncate_utf8(str(item.get("line", "")), self.max_harness_log_line_bytes)
            item["line"] = line
            encoded = json.dumps(item, ensure_ascii=False, default=str).encode("utf-8")
            if used + len(encoded) > self.max_harness_log_bytes:
                truncated = True
                break
            if line_truncated:
                item["truncated"] = True
                truncated = True
            bounded.append(item)
            used += len(encoded)
        return bounded, truncated

    @staticmethod
    def _bounded_json_value(value: Any, limit: int) -> Any:
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) <= limit:
            return value
        preview = encoded[:limit].decode("utf-8", errors="ignore")
        return {"truncated": True, "size_bytes": len(encoded), "preview": preview}

    def _wait_for_approval(self, run: WorkflowRun, node: WorkflowNode, latest: Any) -> Any:
        signal = threading.Event()
        run.approval_signals[node.id] = signal
        self._set_node_state(run, node.id, "waiting", input=latest)
        self._emit(run, "node.approval_required", {"run_id": run.id, "node_id": node.id, "message": node.data.get("description") or "需要人工审批", "input": latest})
        while not signal.wait(timeout=0.2):
            self._check_cancelled(run)
        self._check_cancelled(run)
        decision = run.approval_decisions[node.id]
        self._emit(run, "node.approval_resolved", {"run_id": run.id, "node_id": node.id, **decision})
        return {"approved": bool(decision["approved"]), "comment": decision["comment"], "value": latest}

    def _set_node_state(self, run: WorkflowRun, node_id: str, status: str, **extra: Any) -> None:
        with self._lock:
            run.node_states[node_id] = {**run.node_states.get(node_id, {}), "status": status, **extra}
        self._emit(run, "node.status", {"run_id": run.id, "node_id": node_id, "state": run.node_states[node_id]})

    def _emit(self, run: WorkflowRun, event: str, data: dict[str, Any]) -> None:
        with run.event_signal:
            item = {"event": event, "data": data, "sequence": run.next_event_sequence, "created_at": time.time()}
            run.next_event_sequence += 1
            run.events.append(item)
            if len(run.events) > self.max_run_events:
                del run.events[:len(run.events) - self.max_run_events]
            run.event_signal.notify_all()

    @staticmethod
    def _check_cancelled(run: WorkflowRun) -> None:
        if run.cancel_event.is_set():
            raise RunCancelled()

    @staticmethod
    def _render(template: str, run: WorkflowRun, latest: Any) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            if key in {"input", "latest"}:
                value = latest if key == "latest" else run.input
            elif key.startswith("nodes."):
                value = _lookup(run.outputs, key[6:])
            else:
                value = _lookup({"input": run.input, "latest": latest, "nodes": run.outputs}, key)
                if value is None:
                    value = _lookup_node_reference(run.outputs, key)
            if key == "latest" or key.startswith("nodes.") or key.split(".", 1)[0] in run.outputs:
                value = _display_harness_output(value)
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replace, template)

    @classmethod
    def _render_body_value(cls, value: Any, run: WorkflowRun, latest: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._render_body_value(item, run, latest) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._render_body_value(item, run, latest) for item in value]
        if not isinstance(value, str):
            return value

        expression = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", value)
        if not expression:
            return cls._render(value, run, latest)
        key = expression.group(1).strip()
        if key in {"input", "latest"}:
            resolved = latest if key == "latest" else run.input
        elif key.startswith("nodes."):
            resolved = _lookup(run.outputs, key[6:])
        else:
            resolved = _lookup({"input": run.input, "latest": latest, "nodes": run.outputs}, key)
            if resolved is None:
                resolved = _lookup_node_reference(run.outputs, key)
        if key == "latest" or key.startswith("nodes.") or key.split(".", 1)[0] in run.outputs:
            return _display_harness_output(resolved)
        return resolved

    @staticmethod
    def _evaluate(expression: str, run: WorkflowRun, latest: Any) -> bool:
        expression = expression.strip()
        if not expression:
            return bool(latest)
        lowered = expression.lower()
        if lowered in {"true", "success", "passed"}:
            return True
        if lowered in {"false", "failure", "failed"}:
            return False
        match = re.fullmatch(r"(.+?)\s*(==|!=|contains)\s*(.+)", expression)
        context = {"input": run.input, "latest": latest, "nodes": run.outputs}
        if not match:
            return bool(_lookup(context, expression))
        left = _lookup(context, match.group(1).strip())
        raw_right = match.group(3).strip()
        try:
            right = json.loads(raw_right)
        except json.JSONDecodeError:
            right = raw_right.strip("'\"")
        if match.group(2) == "==":
            return left == right
        if match.group(2) == "!=":
            return left != right
        return str(right) in str(left)

    def _edge_matches(self, edge: WorkflowEdge, output: Any, run: WorkflowRun) -> bool:
        if not edge.condition:
            return True
        condition = edge.condition.strip().lower()
        if condition in {"true", "yes", "success", "passed"}:
            return bool(output)
        if condition in {"false", "no", "failure", "failed"}:
            return not bool(output)
        if isinstance(output, (str, int, float)) and str(output).lower() == condition:
            return True
        return self._evaluate(edge.condition, run, output)

    def _approval_edge_matches(
        self,
        edge: WorkflowEdge,
        output: dict[str, Any],
        run: WorkflowRun,
        *,
        target_type: str | None = None,
    ) -> bool:
        approved = bool(output.get("approved"))
        condition = str(edge.condition or "").strip().lower()
        if not condition:
            # An approval feeding a condition must pass the complete decision
            # object for both approved and rejected outcomes. Other legacy
            # unconditional approval edges remain approve-only gates.
            return target_type == "condition" or approved
        if condition in {"true", "yes", "success", "passed"}:
            return approved
        if condition in {"false", "no", "failure", "failed"}:
            return not approved
        return self._edge_matches(edge, approved, run)


def _lookup(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


def _lookup_node_reference(outputs: dict[str, Any], path: str) -> Any:
    """Resolve generator-friendly ``node-id.field`` template references."""
    node_id, separator, remainder = path.partition(".")
    if not separator or node_id not in outputs:
        return None
    value = outputs[node_id]
    if remainder in {"output", "text"}:
        displayed = _display_harness_output(value)
        if displayed is not value or remainder == "output":
            return displayed
    return _lookup(value, remainder)


def _display_harness_output(value: Any) -> Any:
    """Return the model answer when a node receives a Harness task envelope."""
    if isinstance(value, dict) and isinstance(value.get("text"), str) and isinstance(value.get("task"), dict):
        return value["text"]
    return value


def _cron_matches(expression: str, now: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        return False
    values = [now.minute, now.hour, now.day, now.month, (now.weekday() + 1) % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(_cron_field_matches(field, value, bounds) for field, value, bounds in zip(fields, values, ranges))


def _cron_valid(expression: str) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(_cron_field_valid(field, bounds) for field, bounds in zip(fields, ranges))


def _cron_field_valid(field: str, bounds: tuple[int, int]) -> bool:
    for token in field.split(","):
        if token == "*":
            continue
        if token.startswith("*/"):
            step = token[2:]
            if not step.isdigit() or not 1 <= int(step) <= bounds[1] - bounds[0] + 1:
                return False
            continue
        if "-" in token:
            start, separator, end = token.partition("-")
            if not separator or not start.isdigit() or not end.isdigit():
                return False
            if not bounds[0] <= int(start) <= int(end) <= bounds[1]:
                return False
            continue
        if not token.isdigit() or not bounds[0] <= int(token) <= bounds[1]:
            return False
    return bool(field)


def _cron_field_matches(field: str, value: int, bounds: tuple[int, int]) -> bool:
    for token in field.split(","):
        token = token.strip()
        if token == "*":
            return True
        if token.startswith("*/") and token[2:].isdigit() and int(token[2:]) > 0:
            return value % int(token[2:]) == 0
        if "-" in token:
            start, _, end = token.partition("-")
            if start.isdigit() and end.isdigit() and int(start) <= value <= int(end):
                return True
        if token.isdigit() and bounds[0] <= int(token) <= bounds[1] and int(token) == value:
            return True
    return False


def validate_executable_workflow(
    project: ProjectSpec,
    workflow: WorkflowSpec,
    runtime: bool = False,
    require_output: bool = True,
) -> list[str]:
    errors: list[str] = []
    nodes = {node.id: node for node in workflow.nodes}
    if not nodes:
        errors.append("工作流没有节点")
        return errors
    if len(nodes) != len(workflow.nodes):
        errors.append("工作流包含重复节点编号")
    for edge in workflow.edges:
        if edge.source not in nodes or edge.target not in nodes:
            errors.append(f"连线引用了不存在的节点：{edge.source} → {edge.target}")
        if edge.source == edge.target:
            errors.append(f"节点不能连接到自身：{edge.source}")
        source_node = nodes.get(edge.source)
        if source_node is not None and source_node.type == "condition":
            condition = str(edge.condition or "").strip()
            if condition not in {"true", "false"}:
                errors.append(
                    f"条件节点 {edge.source} 的出边 {edge.target} 必须使用 true 或 false，"
                    f"不能使用 {edge.condition or '空条件'}"
                )
    agent_ids = {item.id for item in project.harness}
    workflow_ids = {item.id for item in project.workflows}
    inbound_ids = {edge.target for edge in workflow.edges}
    webhook_paths: set[str] = set()
    for node in workflow.nodes:
        if runtime and node.type in {"llm", "agent", "tool", "code", "validator"}:
            agent_id = node.data.get("agent_id")
            if node.type != "validator" or agent_id:
                if agent_id not in agent_ids:
                    errors.append(f"节点 {node.id} 未选择有效的 Harness 智能体")
            if node.type != "validator" or agent_id:
                if not str(node.data.get("prompt") or node.data.get("description") or "").strip():
                    errors.append(f"节点 {node.id} 缺少可执行 prompt")
            elif not str(node.data.get("expression") or "").strip():
                errors.append(f"验证器 {node.id} 缺少 agent_id 或 expression")
        if node.type in {"manual_trigger", "webhook", "schedule"} and node.id in inbound_ids:
            errors.append(f"触发器节点 {node.id} 不能有入边")
        if node.type == "schedule" and not str(node.data.get("cron", "")).strip():
            errors.append(f"定时触发器 {node.id} 缺少 cron 表达式")
        if node.type == "schedule" and str(node.data.get("cron", "")).strip() and not _cron_valid(str(node.data.get("cron"))):
            errors.append(f"定时触发器 {node.id} 的 cron 表达式无效")
        if node.type == "webhook":
            path = str(node.data.get("path", "")).rstrip("/")
            if not path.startswith("/hooks/"):
                errors.append(f"Webhook 节点 {node.id} 的 path 必须以 /hooks/ 开头")
            elif path in webhook_paths:
                errors.append(f"Webhook 路径重复：{path}")
            webhook_paths.add(path)
        if node.type == "http_request" and not str(node.data.get("url", "")).strip():
            errors.append(f"HTTP 节点 {node.id} 缺少 URL")
        if node.type == "subworkflow" and node.data.get("workflow_id") not in workflow_ids:
            errors.append(f"子工作流节点 {node.id} 未选择有效工作流")
        if node.type == "condition" and not str(node.data.get("expression") or "").strip():
            errors.append(f"条件节点 {node.id} 缺少 expression")
        if node.type in {"iteration", "loop"}:
            try:
                count = int(node.data.get("iterations", 1))
                if not 1 <= count <= 100:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"循环节点 {node.id} 的次数必须是 1 到 100")
    indegree: dict[str, int] = {}
    incoming: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {}
    if not errors:
        indegree = {node_id: 0 for node_id in nodes}
        incoming = {node_id: [] for node_id in nodes}
        outgoing = {node_id: [] for node_id in nodes}
        for edge in workflow.edges:
            indegree[edge.target] += 1
            incoming[edge.target].append(edge.source)
            outgoing[edge.source].append(edge.target)
        queue = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = 0
        while queue:
            node_id = queue.pop()
            visited += 1
            for target in outgoing[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(nodes):
            errors.append("工作流包含图循环；请使用循环节点表达有限次数循环")
    if require_output and not errors:
        output_ids = {node_id for node_id, node in nodes.items() if node.type == "output"}
        if not output_ids:
            errors.append("工作流至少需要一个 output 节点")
        elif len(nodes) > 1:
            for node_id, node in nodes.items():
                if node.type != "output" and not outgoing[node_id]:
                    errors.append(f"节点 {node_id} 没有连接到后续节点")
                if node.type == "output" and incoming[node_id] == []:
                    errors.append(f"输出节点 {node_id} 没有上游输入")
            reverse: dict[str, set[str]] = {node_id: set() for node_id in nodes}
            for edge in workflow.edges:
                reverse[edge.target].add(edge.source)
            reaches_output: set[str] = set(output_ids)
            stack = list(output_ids)
            while stack:
                node_id = stack.pop()
                for parent in reverse[node_id]:
                    if parent not in reaches_output:
                        reaches_output.add(parent)
                        stack.append(parent)
            missing = sorted(set(nodes) - reaches_output)
            if missing:
                errors.append(f"节点无法到达 output：{', '.join(missing)}")
        for node_id, node in nodes.items():
            if node.type != "condition":
                continue
            branch_conditions = {
                str(edge.condition or "").strip()
                for edge in workflow.edges if edge.source == node_id
            }
            missing_conditions = {"true", "false"} - branch_conditions
            if missing_conditions:
                errors.append(
                    f"完整工作流中的条件节点 {node_id} 缺少分支："
                    f"{', '.join(sorted(missing_conditions))}"
                )
    return errors
