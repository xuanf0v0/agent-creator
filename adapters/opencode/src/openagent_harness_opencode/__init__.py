from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping


def build_prompt(payload: dict[str, Any]) -> str:
    task = payload.get("task", {})
    instructions = str(payload.get("instructions", "")).strip()
    prompt = str(task.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("Harness task prompt is empty")
    return f"{instructions}\n\n用户任务：\n{prompt}" if instructions else prompt


def resolve_executable(command: str, environment: Mapping[str, str]) -> str:
    expanded = str(Path(command).expanduser())
    resolved = shutil.which(expanded, path=environment.get("PATH"))
    if resolved:
        return resolved
    if environment.get("PATHEXT") and not Path(expanded).suffix:
        for directory in environment.get("PATH", "").split(os.pathsep):
            for extension in environment["PATHEXT"].split(";"):
                candidate = Path(directory or ".") / f"{expanded}{extension}"
                if extension and candidate.is_file():
                    return str(candidate)
    raise FileNotFoundError(f"cannot find executable: {command}")


def _load_env_file(path: str | None, environment: dict[str, str]) -> None:
    if not path:
        return
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        raise FileNotFoundError(f"environment file does not exist: {env_path}")
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and not environment.get(key):
            environment[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an Agent Harness task through the real OpenCode CLI")
    parser.add_argument("--model", required=True)
    parser.add_argument("--agent", default="build")
    parser.add_argument("--binary", default=os.environ.get("OPENCODE_BIN", "opencode"))
    parser.add_argument("--env-file")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        prompt = build_prompt(payload)
        environment = os.environ.copy()
        _load_env_file(args.env_file, environment)
        binary = resolve_executable(args.binary, environment)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"invalid Harness invocation: {exc}", file=sys.stderr, flush=True)
        return 2
    task = payload.get("task", {})
    command = [
        binary,
        "run",
        "--format",
        "json",
        "--model",
        args.model,
        "--agent",
        args.agent,
        "--title",
        str(task.get("title") or "OpenAgent Harness task"),
        prompt,
    ]
    try:
        process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert process.stdout is not None
        while chunk := process.stdout.read(16 * 1024):
            # Harness consumes newline-delimited logs with a bounded reader.
            # OpenCode JSON events can contain very large tool results on one
            # line, so insert a delimiter at a safe byte boundary while
            # preserving the complete child output.
            sys.stdout.buffer.write(chunk)
            if not chunk.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
        return process.wait()
    except OSError as exc:
        print(f"cannot start OpenCode: {exc}", file=sys.stderr, flush=True)
        return 127
