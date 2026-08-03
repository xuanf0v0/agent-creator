from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def build_prompt(payload: dict[str, Any]) -> str:
    task = payload.get("task", {})
    instructions = str(payload.get("instructions", "")).strip()
    prompt = str(task.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("Harness task prompt is empty")
    if instructions:
        return f"{instructions}\n\n用户任务：\n{prompt}"
    return prompt


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a governed Agent Harness task with the real OpenCode CLI")
    parser.add_argument("--model", required=True)
    parser.add_argument("--agent", default="build")
    parser.add_argument("--binary", default=os.environ.get("OPENCODE_BIN", "opencode"))
    parser.add_argument("--env-file")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        prompt = build_prompt(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"invalid Harness payload: {exc}", file=sys.stderr, flush=True)
        return 2
    task = payload.get("task", {})
    command = [
        args.binary, "run", "--format", "json", "--model", args.model,
        "--agent", args.agent, "--title", str(task.get("title") or "OpenAgent Harness task"), prompt,
    ]
    environment = os.environ.copy()
    if args.env_file:
        path = Path(args.env_file).expanduser()
        if not path.is_file():
            print(f"environment file does not exist: {path}", file=sys.stderr, flush=True)
            return 2
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip("'\"")
            if key and not environment.get(key):
                environment[key] = value
    try:
        return subprocess.call(command, env=environment)
    except OSError as exc:
        print(f"cannot start OpenCode: {exc}", file=sys.stderr, flush=True)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
