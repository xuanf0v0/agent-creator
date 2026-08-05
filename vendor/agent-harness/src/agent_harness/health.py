from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .models import AgentManifest
from .plugins import resolve_plugin


async def _http(agent: AgentManifest, process: asyncio.subprocess.Process | None) -> None:
    assert agent.service is not None
    check = agent.service.health
    url = check.url or f"http://{check.host}:{check.port or agent.service.port}{check.path}"
    async with httpx.AsyncClient(timeout=1) as client:
        response = await client.get(url)
        if response.status_code not in check.expected_statuses:
            raise RuntimeError(f"unhealthy HTTP status {response.status_code}: {url}")


async def _tcp(agent: AgentManifest, process: asyncio.subprocess.Process | None) -> None:
    assert agent.service is not None
    check = agent.service.health
    reader, writer = await asyncio.open_connection(check.host, check.port or agent.service.port)
    writer.close()
    await writer.wait_closed()


async def _command(agent: AgentManifest, process: asyncio.subprocess.Process | None) -> None:
    assert agent.service is not None and agent.service.health.command
    child = await asyncio.create_subprocess_exec(
        *agent.service.health.command, cwd=agent.cwd,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    if await child.wait() != 0:
        raise RuntimeError("health command failed")


async def _process(agent: AgentManifest, process: asyncio.subprocess.Process | None) -> None:
    if process is None:
        raise RuntimeError("process health check requires an owned process")
    await asyncio.sleep(min(agent.service.health.interval_seconds, 0.05))  # type: ignore[union-attr]
    if process.returncode is not None:
        raise RuntimeError(f"agent exited with code {process.returncode}")


async def _none(agent: AgentManifest, process: asyncio.subprocess.Process | None) -> None:
    return None


async def wait_healthy(agent: AgentManifest, process: asyncio.subprocess.Process | None) -> None:
    assert agent.service is not None
    check = agent.service.health
    probe = resolve_plugin(
        check.kind, "agent_harness.health_checks",
        {"http": _http, "tcp": _tcp, "command": _command, "process": _process, "none": _none},
    )
    deadline = time.monotonic() + check.timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.returncode is not None:
            raise RuntimeError(f"agent exited with code {process.returncode}")
        try:
            await probe(agent, process)
            return
        except (OSError, httpx.HTTPError, RuntimeError) as exc:
            last_error = exc
        await asyncio.sleep(check.interval_seconds)
    raise RuntimeError(f"{check.kind} health check timed out: {last_error or 'not ready'}")
