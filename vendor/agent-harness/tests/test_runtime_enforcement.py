from __future__ import annotations

import shutil
import subprocess
import sys
import os
import uuid
import asyncio
from pathlib import Path

import pytest

from agent_harness.models import AgentManifest
from agent_harness.runtime_policy import enforce_command


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("sandbox-exec") or bool(os.environ.get("CODEX_SANDBOX")) and os.environ.get("AGENT_HARNESS_TEST_NESTED_SANDBOX") != "1",
    reason="macOS Seatbelt test requires an unnested sandbox",
)


def _agent(workspace: Path, network: str = "deny", allowlist: list[str] | None = None) -> AgentManifest:
    sandbox = {"network": network}
    if allowlist is not None: sandbox["network_allowlist"] = allowlist
    return AgentManifest.model_validate({
        "id": "worker", "name": "Worker", "cwd": workspace,
        "task": {"command": ["tool"], "sandbox": sandbox, "verification": [{"name": "ok", "command": ["tool"]}]},
    })


def test_seatbelt_blocks_write_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"; workspace.mkdir()
    outside = Path("/private/var/tmp") / f"agent-harness-{uuid.uuid4().hex}.txt"
    code = "from pathlib import Path; Path('inside.txt').write_text('ok'); Path(%r).write_text('escape')" % str(outside)
    result = subprocess.run(enforce_command([sys.executable, "-c", code], workspace, _agent(workspace)), cwd=workspace, capture_output=True, text=True)
    assert result.returncode != 0
    assert (workspace / "inside.txt").read_text() == "ok"
    try: assert not outside.exists()
    finally: outside.unlink(missing_ok=True)


async def test_seatbelt_denies_network_but_allows_allowlisted_ip(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"; workspace.mkdir()
    accepted: list[bool] = []
    async def handle(_reader, writer):
        accepted.append(True); writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"); await writer.drain(); writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    code = f"import socket; s=socket.create_connection(('127.0.0.1',{port}),1); s.close()"
    denied_process = await asyncio.create_subprocess_exec(*enforce_command([sys.executable, "-c", code], workspace, _agent(workspace)), cwd=workspace)
    denied = await denied_process.wait()
    assert denied != 0
    from agent_harness.network_proxy import AllowlistProxy
    proxy = AllowlistProxy([f"127.0.0.1:{port}"]); proxy_port = await proxy.start()
    proxy_code = f"import socket; s=socket.create_connection(('127.0.0.1',{proxy_port}),1); s.sendall(b'GET http://127.0.0.1:{port}/ HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n\\r\\n'); assert b'200 OK' in s.recv(1024); s.close()"
    allowed_process = await asyncio.create_subprocess_exec(*enforce_command([sys.executable, "-c", proxy_code], workspace, _agent(workspace, "allowlist", [f"127.0.0.1:{port}"]), proxy_port), cwd=workspace)
    allowed = await allowed_process.wait()
    await proxy.close(); server.close(); await server.wait_closed()
    assert allowed == 0
    assert accepted == [True]
