from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from .models import AgentManifest
from .plugins import resolve_plugin


@dataclass
class DeploymentHandle:
    process: asyncio.subprocess.Process | None = None
    pid: int = 0
    url: str | None = None
    value: Any = None


class DeploymentBackend(Protocol):
    async def start(self, agent: AgentManifest, env: dict[str, str], process_options: dict[str, Any]) -> DeploymentHandle: ...
    async def stop(self, agent: AgentManifest, handle: DeploymentHandle, terminate: Any) -> None: ...


class LocalProcessBackend:
    async def start(self, agent: AgentManifest, env: dict[str, str], process_options: dict[str, Any]) -> DeploymentHandle:
        assert agent.service is not None and agent.service.command
        process = await asyncio.create_subprocess_exec(
            *agent.service.command, cwd=agent.cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **process_options,
        )
        url = agent.service.endpoint or (f"http://127.0.0.1:{agent.service.port}" if agent.service.port else None)
        return DeploymentHandle(process=process, pid=process.pid or 0, url=url)

    async def stop(self, agent: AgentManifest, handle: DeploymentHandle, terminate: Any) -> None:
        await terminate(handle.process)


class ExternalBackend:
    async def start(self, agent: AgentManifest, env: dict[str, str], process_options: dict[str, Any]) -> DeploymentHandle:
        assert agent.service is not None
        return DeploymentHandle(url=agent.service.endpoint or agent.service.health.url)

    async def stop(self, agent: AgentManifest, handle: DeploymentHandle, terminate: Any) -> None:
        return None


def deployment_backend(agent: AgentManifest) -> DeploymentBackend:
    assert agent.service is not None
    backend = resolve_plugin(
        agent.service.deployment.kind, "agent_harness.deployments",
        {"local_process": LocalProcessBackend, "external": ExternalBackend},
    )
    return backend() if isinstance(backend, type) else backend
