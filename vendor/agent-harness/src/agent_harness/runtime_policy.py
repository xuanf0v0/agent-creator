from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from .plugins import resolve_plugin


class SandboxUnavailable(RuntimeError):
    pass


def _scheme(value: str) -> str:
    return json.dumps(value)


def _macos_command(argv: list[str], cwd: Path, agent: Any, proxy_port: int | None = None) -> list[str]:
    sandbox = agent.task.sandbox
    writable = [Path(os.environ.get("TMPDIR", "/private/tmp")).resolve(), Path("/tmp").resolve()]
    if sandbox.workspace_write: writable.insert(0, agent.cwd.resolve())
    destinations = " ".join(f"(subpath {_scheme(str(path))})" for path in writable)
    rules = ["(version 1)", "(allow default)", f"(deny file-write* (require-not (require-any {destinations})))"]
    if sandbox.network == "deny":
        rules.append("(deny network*)")
    elif sandbox.network == "allowlist":
        if proxy_port is None:
            raise SandboxUnavailable("network allowlist requires the managed proxy")
        rules.append(f'(deny network-outbound (require-not (remote ip "localhost:{proxy_port}")))')
    return ["/usr/bin/sandbox-exec", "-p", "".join(rules), *argv]


def _linux_command(argv: list[str], cwd: Path, agent: Any) -> list[str]:
    binary = shutil.which("bwrap")
    if not binary:
        raise SandboxUnavailable("runtime sandbox requires bubblewrap on Linux")
    sandbox = agent.task.sandbox
    if sandbox.network == "allowlist":
        raise SandboxUnavailable("network allowlist requires a platform firewall backend on Linux")
    command = [binary, "--die-with-parent", "--new-session", "--ro-bind", "/", "/"]
    if sandbox.workspace_write: command.extend(["--bind", str(agent.cwd), str(agent.cwd)])
    command.extend(["--bind", "/tmp", "/tmp", "--chdir", str(cwd)])
    if sandbox.network == "deny": command.append("--unshare-net")
    return [*command, "--", *argv]


def enforce_command(argv: list[str], cwd: Path, agent: Any, proxy_port: int | None = None) -> list[str]:
    sandbox = agent.task.sandbox
    if not sandbox.enabled:
        return argv
    try:
        backend = sandbox.backend
        if backend == "auto":
            if sys.platform == "darwin": backend = "seatbelt"
            elif sys.platform.startswith("linux"): backend = "bubblewrap"
            else: raise SandboxUnavailable(f"runtime sandbox is unsupported on {sys.platform}")
        factory = resolve_plugin(
            backend, "agent_harness.sandbox_backends",
            {
                "seatbelt": lambda values, directory, manifest, port, _options: _macos_command(values, directory, manifest, port),
                "bubblewrap": lambda values, directory, manifest, _port, _options: _linux_command(values, directory, manifest),
                "none": lambda values, _directory, _manifest, _port, _options: values,
            },
        )
        return list(factory(argv, cwd, agent, proxy_port, sandbox.backend_options))
    except (SandboxUnavailable, ValueError):
        if sandbox.enforcement == "best_effort": return argv
        raise
