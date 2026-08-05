from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .catalog import AgentCatalog
from .environment import fingerprint, fingerprint_details
from .instructions import load_instructions, safe_task_directory
from .state_store import StateStore
from .supervisor import AgentSupervisor
from .protocols import Invocation, build_invocation


class TaskRunner:
    def __init__(self, catalog: AgentCatalog, store: StateStore, supervisor: AgentSupervisor | None = None) -> None:
        self.catalog, self.store = catalog, store
        self.supervisor = supervisor
        self._queues = {agent.id: asyncio.Queue() for agent in catalog.all() if agent.task}
        self._workers: list[asyncio.Task] = []
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._active_calls: set[str] = set()
        self._accepting = False
        self.ready_error = ""

    async def start(self) -> None:
        self._accepting = True
        for agent_id, queue in self._queues.items():
            self._workers.append(asyncio.create_task(self._worker(agent_id, queue)))
        for task in reversed(self.store.tasks()):
            if task["status"] == "queued" and task["agent_id"] in self._queues:
                await self._queues[task["agent_id"]].put(task["id"])

    async def shutdown(self, grace_seconds: float = 10) -> None:
        self._accepting = False
        deadline = time.monotonic() + grace_seconds
        while self._active and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        for task_id, process in list(self._active.items()):
            task = self.store.task(task_id)
            await self._terminate(process)
            try: self.store.set_task_status(task_id, "failed", "harness interrupted during shutdown")
            except Exception: pass
            self.store.write_progress(Path(task["workspace"]))
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def reload_catalog(self) -> None:
        wanted = {agent.id for agent in self.catalog.all() if agent.task}
        for agent_id in wanted - set(self._queues):
            queue: asyncio.Queue[str] = asyncio.Queue()
            self._queues[agent_id] = queue
            if self._accepting:
                self._workers.append(asyncio.create_task(self._worker(agent_id, queue)))

    async def create(self, agent_id: str, title: str, prompt: str, relative_path: str = ".", idempotency_key: str | None = None) -> dict[str, Any]:
        if not self._accepting:
            raise RuntimeError("harness is not accepting tasks")
        agent = self.catalog.require(agent_id)
        if agent.task is None:
            raise ValueError("agent has no task runtime")
        safe_task_directory(agent.cwd, relative_path)
        if idempotency_key:
            payload = {"agent_id": agent_id, "workspace": str(agent.cwd), "relative_path": relative_path, "title": title, "prompt": prompt}
            existing = self.store.idempotent_resource("task.create", idempotency_key, self.store.request_hash(payload))
            if existing: return self.store.task(existing)
        if self.store.queue_depth(agent_id) >= agent.task.limits.max_queue_depth:
            raise OverflowError("agent task queue is full")
        task = self.store.create_task(agent_id, agent.cwd, relative_path, title, prompt, idempotency_key)
        self.store.write_progress(agent.cwd)
        await self._queues[agent_id].put(task["id"])
        return task

    async def retry(self, task_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        if not self._accepting:
            raise RuntimeError("harness is not accepting tasks")
        task = self.store.task(task_id)
        if idempotency_key:
            digest = self.store.request_hash({"task_id": task_id})
            existing = self.store.idempotent_resource("task.retry", idempotency_key, digest)
            if existing: return self.store.task(existing)
        if task["status"] not in {"failed", "blocked", "cancelled"}:
            raise ValueError("only failed, blocked, or cancelled tasks can be retried")
        agent = self.catalog.require(task["agent_id"])
        assert agent.task is not None
        if self.store.queue_depth(agent.id) >= agent.task.limits.max_queue_depth:
            raise OverflowError("agent task queue is full")
        self.store.transition(task_id, {"failed", "blocked", "cancelled"}, "queued")
        if idempotency_key:
            with self.store._lock, self.store._db:
                self.store._db.execute("INSERT INTO idempotency VALUES (?,?,?,?,?)", ("task.retry", idempotency_key, digest, task_id, time.time()))
        self.store.write_progress(Path(task["workspace"]))
        await self._queues[task["agent_id"]].put(task_id)
        return self.store.task(task_id)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        if task["status"] in {"completed", "failed", "cancelled"}:
            raise ValueError("task is already terminal")
        process = self._active.get(task_id)
        if process:
            await self._terminate(process)
        self.store.transition(task_id, {"queued", "preparing", "working", "verifying", "blocked"}, "cancelled")
        self.store.write_progress(Path(task["workspace"]))
        return self.store.task(task_id)

    async def _worker(self, agent_id: str, queue: asyncio.Queue[str]) -> None:
        while True:
            task_id = await queue.get()
            try:
                if self.store.task(task_id)["status"] == "queued":
                    try:
                        await self._execute(task_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        task = self.store.task(task_id)
                        self._status(task_id, "failed", Path(task["workspace"]), f"harness execution error: {exc}")
            finally:
                queue.task_done()

    async def _execute(self, task_id: str) -> None:
        task = self.store.task(task_id)
        agent = self.catalog.require(task["agent_id"])
        assert agent.task is not None
        workdir = safe_task_directory(agent.cwd, task["relative_path"])
        self.store.transition(task_id, {"queued"}, "preparing")
        self._progress(task_id, agent.cwd)
        current, files = fingerprint(agent)
        prepared = self.store.environment(agent.id)
        if prepared is None or prepared["fingerprint"] != current:
            if not agent.environment.auto_setup_on_drift or self.supervisor is None:
                self._status(task_id, "blocked", agent.cwd, "environment drift; run setup")
                return
            operation = self.store.start_setup(agent.id, f"auto:{current}")
            created = bool(operation.pop("_created", False))
            if created:
                self.store.update_setup(operation["id"], "preparing")
                result = await self.supervisor.setup(agent.id)
                if result.status.value != "stopped":
                    self.store.update_setup(operation["id"], "error", error_code="setup_failed", error_message=result.error_message, logs=self.supervisor.logs.tail(agent.id, 2000))
                    self._status(task_id, "blocked", agent.cwd, f"automatic setup failed: {result.error_message}")
                    return
                current, files, hashes = fingerprint_details(agent)
                self.store.set_environment(agent.id, current, files, hashes)
                self.store.update_setup(operation["id"], "ready", fingerprint=current, logs=self.supervisor.logs.tail(agent.id, 2000))
                self.store.add_event(task_id, "environment.auto_setup.completed", {"setup_operation_id": operation["id"], "fingerprint": current})
            elif operation["status"] != "ready":
                self._status(task_id, "blocked", agent.cwd, "automatic setup is unavailable")
                return
        bundle = load_instructions(agent.cwd, workdir)
        attempt_id = self.store.start_attempt(task_id, bundle.content, bundle.sha256, bundle.sources)
        payload = self._prompt_payload(task, bundle.content, agent.task.tools.model_dump())
        invocation = build_invocation(agent.task, payload)
        self.store.set_attempt_status(attempt_id, "working")
        self._status(task_id, "working", agent.cwd)
        attempt_started = time.monotonic()
        try:
            exit_code = await asyncio.wait_for(
                self._run_invocation(task_id, attempt_id, invocation, workdir,
                    AgentSupervisor._environment(agent), agent.task.limits, agent=agent),
                timeout=min(agent.task.limits.command_timeout_seconds, agent.task.limits.attempt_timeout_seconds),
            )
        except asyncio.TimeoutError:
            self.store.finish_attempt(attempt_id, "failed", None, [{"kind": "agent_timeout"}])
            self._status(task_id, "failed", agent.cwd, "agent command timed out")
            return
        if self.store.task(task_id)["status"] == "cancelled":
            self.store.finish_attempt(attempt_id, "cancelled", exit_code, [])
            return
        if exit_code != 0:
            self.store.finish_attempt(attempt_id, "failed", exit_code, [])
            self._status(task_id, "failed", agent.cwd, f"agent exited with code {exit_code}")
            return
        self.store.set_attempt_status(attempt_id, "verifying")
        self._status(task_id, "verifying", agent.cwd)
        evidence: list[dict[str, Any]] = []
        for check in agent.task.verification:
            remaining = agent.task.limits.attempt_timeout_seconds - (time.monotonic() - attempt_started)
            if remaining <= 0:
                evidence.append({"name": check.name, "command": check.command, "exit_code": None, "timed_out": True, "duration_seconds": 0, "reason": "attempt timeout"})
                break
            check_started = time.monotonic()
            try:
                code = await asyncio.wait_for(
                    self._run_command(task_id, attempt_id, check.command, workdir, AgentSupervisor._environment(agent), agent.task.limits, stream="verification", agent=agent),
                    timeout=min(check.timeout_seconds, remaining),
                )
                timed_out = False
            except asyncio.TimeoutError:
                code, timed_out = None, True
            evidence.append({"name": check.name, "command": check.command, "exit_code": code, "timed_out": timed_out, "duration_seconds": time.monotonic() - check_started})
            if self.store.task(task_id)["status"] == "cancelled":
                self.store.finish_attempt(attempt_id, "cancelled", code, evidence)
                return
            if code != 0:
                break
        passed = len(evidence) == len(agent.task.verification) and all(item["exit_code"] == 0 for item in evidence)
        status = "completed" if passed else "failed"
        self.store.finish_attempt(attempt_id, status, exit_code, evidence)
        reason = "" if passed else f"verification failed: {evidence[-1]['name']}"
        self._status(task_id, status, agent.cwd, reason)

    async def _run_invocation(self, task_id: str, attempt_id: str, invocation: Invocation, cwd: Path, env: dict[str, str], limits: Any, agent: Any = None) -> int:
        if invocation.kind == "http":
            self._active_calls.add(task_id)
            try:
                async with httpx.AsyncClient(timeout=limits.command_timeout_seconds) as client:
                    response = await client.request(invocation.method, invocation.url, headers=invocation.headers, json=invocation.body)
                body = response.text[:limits.max_log_bytes]
                if body:
                    self.store.add_logs([(task_id, attempt_id, "agent", line, time.time()) for line in body.splitlines()[:limits.max_log_lines]])
                self.store.add_event(task_id, "protocol.response", {"protocol": "http", "status_code": response.status_code, "url": invocation.url})
                return 0 if 200 <= response.status_code < 300 else 1
            finally:
                self._active_calls.discard(task_id)
        if invocation.kind != "process":
            raise ValueError(f"unsupported invocation kind: {invocation.kind}")
        return await self._run_command(task_id, attempt_id, list(invocation.argv), cwd, env, limits, stdin=invocation.stdin, agent=agent)

    async def _run_command(self, task_id: str, attempt_id: str, argv: list[str], cwd: Path, env: dict[str, str], limits: Any, stdin: str | None = None, stream: str = "agent", agent: Any = None) -> int:
        proxy = None
        if agent is not None:
            from .runtime_policy import enforce_command
            proxy_port = None
            if agent.task.sandbox.enabled and agent.task.sandbox.network == "allowlist":
                from .network_proxy import AllowlistProxy
                proxy = AllowlistProxy(agent.task.sandbox.network_allowlist)
                proxy_port = await proxy.start()
                proxy_url = f"http://127.0.0.1:{proxy_port}"
                env.update({"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "ALL_PROXY": proxy_url, "NO_PROXY": ""})
            argv = enforce_command(argv, cwd, agent, proxy_port)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                **AgentSupervisor._group_options(),
            )
        except Exception:
            if proxy: await proxy.close()
            raise
        self._active[task_id] = process
        if stdin is not None and process.stdin:
            process.stdin.write(stdin.encode())
            await process.stdin.drain()
            process.stdin.close()
        try:
            rows: list[tuple[str, str, str, str, float]] = []
            line_count = byte_count = 0
            truncated = False
            if process.stdout:
                async for raw in process.stdout:
                    if line_count < limits.max_log_lines and byte_count + len(raw) <= limits.max_log_bytes:
                        rows.append((task_id, attempt_id, stream, raw.decode(errors="replace").rstrip("\r\n"), time.time()))
                        line_count += 1; byte_count += len(raw)
                        if len(rows) >= 100:
                            self.store.add_logs(rows); rows.clear()
                    elif not truncated:
                        truncated = True
                        self.store.add_event(task_id, "log.truncated", {"attempt_id": attempt_id, "max_lines": limits.max_log_lines, "max_bytes": limits.max_log_bytes})
            self.store.add_logs(rows)
            return await process.wait()
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        finally:
            self._active.pop(task_id, None)
            if proxy: await proxy.close()

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            await killer.wait()
        else:
            try: os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError: return
        try: await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill(); await process.wait()

    def _status(self, task_id: str, status: str, workspace: Path, reason: str = "") -> None:
        self.store.set_task_status(task_id, status, reason)
        self._progress(task_id, workspace)

    def _progress(self, task_id: str, workspace: Path) -> None:
        if not self.store.write_progress(workspace):
            self.store.add_event(task_id, "progress.write_failed", {"workspace": str(workspace)})

    @staticmethod
    def _prompt_payload(task: dict[str, Any], instructions: str, tools: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "task": {"id": task["id"], "title": task["title"], "prompt": task["prompt"], "relative_path": task["relative_path"]},
            "instructions": instructions,
            "tool_policy": tools,
            "contract": "Exit 0 only after work is ready for harness verification. Filesystem and network policy are enforced by the Harness runtime sandbox.",
        }
