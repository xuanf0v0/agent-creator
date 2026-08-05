from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Literal

from .models import TaskSpec
from .plugins import resolve_plugin


@dataclass(frozen=True)
class Invocation:
    kind: Literal["process", "http"]
    argv: tuple[str, ...] = ()
    stdin: str | None = None
    method: str = "POST"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None


def _stdin_json(spec: TaskSpec, payload: dict[str, Any]) -> Invocation:
    return Invocation(kind="process", argv=tuple(spec.command or ()), stdin=json.dumps(payload, ensure_ascii=False) + "\n")


def _argv(spec: TaskSpec, payload: dict[str, Any]) -> Invocation:
    task = payload["task"]
    replacements = {
        "{task_id}": str(task["id"]), "{title}": str(task["title"]),
        "{prompt}": str(task["prompt"]), "{relative_path}": str(task["relative_path"]),
        "{payload_json}": json.dumps(payload, ensure_ascii=False),
    }
    argv = []
    for value in spec.command or ():
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        argv.append(value)
    return Invocation(kind="process", argv=tuple(argv))


def _http(spec: TaskSpec, payload: dict[str, Any]) -> Invocation:
    options = spec.protocol.options
    return Invocation(
        kind="http", method=str(options.get("method", "POST")).upper(),
        url=str(options["url"]), headers={str(k): str(v) for k, v in options.get("headers", {}).items()},
        body=payload,
    )


def _mcp(spec: TaskSpec, payload: dict[str, Any]) -> Invocation:
    options = spec.protocol.options
    tool = options.get("tool")
    if not tool:
        raise ValueError("mcp task protocol requires protocol.options.tool")
    body = {
        "jsonrpc": "2.0", "id": payload["task"]["id"], "method": "tools/call",
        "params": {"name": tool, "arguments": payload},
    }
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    headers.update({str(k): str(v) for k, v in options.get("headers", {}).items()})
    return Invocation(kind="http", method="POST", url=str(options["url"]), headers=headers, body=body)


def build_invocation(spec: TaskSpec, payload: dict[str, Any]) -> Invocation:
    factory = resolve_plugin(
        spec.protocol.kind, "agent_harness.task_protocols",
        {"stdin_json": _stdin_json, "argv": _argv, "http": _http, "mcp": _mcp},
    )
    invocation = factory(spec, payload)
    if not isinstance(invocation, Invocation):
        raise TypeError("task protocol plugin must return Invocation")
    return invocation
