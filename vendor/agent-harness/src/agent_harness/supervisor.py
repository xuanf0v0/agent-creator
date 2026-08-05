from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .catalog import AgentCatalog
from .deployment import DeploymentHandle, deployment_backend
from .health import wait_healthy
from .logs import LogBroker
from .models import AgentManifest, AgentState, AgentStatus


@dataclass
class Runtime:
    state: AgentState = AgentState.STOPPED
    process: asyncio.subprocess.Process | None = None
    pid: int = 0
    started_at: float = 0
    error: str = ""
    handle: DeploymentHandle | None = None
    backend: Any = None
    url: str | None = None


class AgentSupervisor:
    def __init__(self, catalog: AgentCatalog) -> None:
        self.catalog = catalog
        ids = [agent.id for agent in catalog.all()]
        self.logs = LogBroker(ids)
        self._runtime = {agent_id: Runtime() for agent_id in ids}
        self._locks = {agent_id: asyncio.Lock() for agent_id in ids}

    def status(self, agent_id: str) -> AgentStatus:
        agent = self.catalog.require(agent_id)
        runtime = self._runtime[agent_id]
        if runtime.process is not None and runtime.process.returncode is not None and runtime.state is AgentState.RUNNING:
            runtime.state, runtime.pid = AgentState.ERROR, 0
            runtime.error = f"agent exited with code {runtime.process.returncode}"
            runtime.process = None
        return AgentStatus(
            id=agent.id, name=agent.name, description=agent.description, icon=agent.icon,
            port=agent.service.port if agent.service else None, status=runtime.state, pid=runtime.pid,
            started_at=runtime.started_at, error_message=runtime.error,
            url=runtime.url if runtime.state is AgentState.RUNNING else None,
        )

    def all_statuses(self) -> list[AgentStatus]:
        return [self.status(agent.id) for agent in self.catalog.all()]

    def reload_catalog(self) -> None:
        ids = {agent.id for agent in self.catalog.all()}
        if set(self._runtime) - ids:
            running_removed = [agent_id for agent_id in set(self._runtime) - ids if self._runtime[agent_id].process is not None]
            if running_removed:
                raise RuntimeError(f"cannot remove running agents: {', '.join(sorted(running_removed))}")
        for agent_id in ids:
            self._runtime.setdefault(agent_id, Runtime())
            self._locks.setdefault(agent_id, asyncio.Lock())
            self.logs._buffers.setdefault(agent_id, deque(maxlen=2000))
            self.logs._subscribers.setdefault(agent_id, set())
        for agent_id in set(self._runtime) - ids:
            self._runtime.pop(agent_id, None); self._locks.pop(agent_id, None)

    async def setup(self, agent_id: str) -> AgentStatus:
        agent = self.catalog.require(agent_id)
        async with self._locks[agent_id]:
            runtime = self._runtime[agent_id]
            if runtime.state in {AgentState.RUNNING, AgentState.STARTING}:
                raise RuntimeError("cannot setup a running agent")
            runtime.state, runtime.error = AgentState.SETTING_UP, ""
            if agent.environment.setup_command is None:
                runtime.state = AgentState.STOPPED
                self.logs.publish(agent_id, "setup skipped: internal no-op")
                return self.status(agent_id)
            try:
                code = await asyncio.wait_for(
                    self._run_setup(agent), timeout=agent.environment.setup_timeout_seconds
                )
            except asyncio.TimeoutError:
                runtime.state, runtime.error = AgentState.ERROR, "setup timed out"
                return self.status(agent_id)
            runtime.state = AgentState.STOPPED if code == 0 else AgentState.ERROR
            if code:
                runtime.error = f"setup exited with code {code}"
            return self.status(agent_id)

    async def start(self, agent_id: str) -> AgentStatus:
        agent = self.catalog.require(agent_id)
        if agent.service is None:
            raise ValueError("agent has no service runtime")
        async with self._locks[agent_id]:
            runtime = self._runtime[agent_id]
            if runtime.state is AgentState.RUNNING:
                return self.status(agent_id)
            if agent.service.deployment.kind == "local_process" and agent.service.port and self._port_in_use(agent.service.port):
                runtime.state, runtime.error = AgentState.ERROR, f"port {agent.service.port} is already in use"
                return self.status(agent_id)
            runtime.state, runtime.error = AgentState.STARTING, ""
            try:
                backend = deployment_backend(agent)
                handle = await backend.start(agent, self._environment(agent), self._group_options())
                runtime.backend, runtime.handle = backend, handle
                runtime.process, runtime.pid, runtime.url = handle.process, handle.pid, handle.url
                runtime.started_at = time.time()
                if handle.process is not None:
                    asyncio.create_task(self._read_output(agent.id, handle.process))
                await wait_healthy(agent, handle.process)
                runtime.state = AgentState.RUNNING
            except Exception as exc:
                if runtime.backend and runtime.handle:
                    await runtime.backend.stop(agent, runtime.handle, self._terminate)
                else:
                    await self._terminate(runtime.process)
                runtime.process, runtime.pid = None, 0
                runtime.handle, runtime.backend, runtime.url = None, None, None
                runtime.state, runtime.error = AgentState.ERROR, str(exc)
            return self.status(agent_id)

    async def stop(self, agent_id: str) -> AgentStatus:
        self.catalog.require(agent_id)
        async with self._locks[agent_id]:
            runtime = self._runtime[agent_id]
            runtime.state = AgentState.STOPPING
            agent = self.catalog.require(agent_id)
            if runtime.backend and runtime.handle:
                await runtime.backend.stop(agent, runtime.handle, self._terminate)
            else:
                await self._terminate(runtime.process)
            runtime.process, runtime.pid, runtime.started_at = None, 0, 0
            runtime.handle, runtime.backend, runtime.url = None, None, None
            runtime.state, runtime.error = AgentState.STOPPED, ""
            return self.status(agent_id)

    async def restart(self, agent_id: str) -> AgentStatus:
        await self.stop(agent_id)
        return await self.start(agent_id)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.stop(agent.id) for agent in self.catalog.all()))

    async def _run_setup(self, agent: AgentManifest) -> int:
        command = list(agent.environment.setup_command or ())
        if command and command[0] in {"python", "python3"} and shutil.which(command[0], path=self._environment(agent).get("PATH")) is None:
            command[0] = sys.executable
        process = await asyncio.create_subprocess_exec(
            *command, cwd=agent.cwd, env=self._environment(agent),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **self._group_options(),
        )
        try:
            await self._read_output(agent.id, process)
            return await process.wait()
        except asyncio.CancelledError:
            await self._terminate(process)
            raise

    async def _read_output(self, agent_id: str, process: asyncio.subprocess.Process) -> None:
        if process.stdout is None:
            return
        async for raw in process.stdout:
            self.logs.publish(agent_id, raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    async def _terminate(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    def _port_in_use(port: int) -> bool:
        with socket.socket() as sock:
            return sock.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _group_options() -> dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    @staticmethod
    def _environment(agent: AgentManifest) -> dict[str, str]:
        env = dict(os.environ)
        # A managed agent owns its environment. Inheriting the harness venv
        # makes tools such as uv target (or warn about) the wrong project.
        env.pop("VIRTUAL_ENV", None)
        venv = agent.cwd / ".venv"
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        if scripts.is_dir():
            env["VIRTUAL_ENV"] = str(venv)
            env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")
        if agent.env_file.exists():
            for line in agent.env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, value = stripped.partition("=")
                    env[key.strip()] = value.strip().strip('"').strip("'")
        return env
