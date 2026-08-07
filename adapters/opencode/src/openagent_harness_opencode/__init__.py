from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import tempfile
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


def _event_text(item: dict[str, Any]) -> str:
    part = item.get("part")
    if not isinstance(part, dict):
        properties = item.get("properties")
        part = properties.get("part") if isinstance(properties, dict) else None
    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
        return part["text"]
    if item.get("type") == "text" and isinstance(item.get("text"), str):
        return item["text"]
    return ""


def extract_final_text(lines: list[str]) -> tuple[str, list[str]]:
    """Extract only assistant text from OpenCode's JSONL event stream.

    Tool events, step metadata, and diagnostics are intentionally not passed
    into the workflow. Returning the raw stream makes every downstream node
    consume protocol logs instead of the model result.
    """
    chunks: list[str] = []
    diagnostics: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            diagnostics.append(line[:1000])
            continue
        text = _event_text(item)
        if text:
            chunks.append(text)
        if "error" in str(item.get("type", "")).lower() or item.get("error"):
            diagnostics.append(json.dumps(item, ensure_ascii=False)[:1000])
    return "".join(chunks).strip(), diagnostics


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
        # OpenCode writes its own protocol log under XDG_DATA_HOME. Harness
        # seatbelt tasks may not write the user's home directory, so provide
        # an isolated writable runtime directory for every invocation.
        runtime_root = Path(tempfile.mkdtemp(prefix="openagent-opencode-") )
        environment.setdefault("XDG_DATA_HOME", str(runtime_root / "data"))
        environment.setdefault("XDG_CACHE_HOME", str(runtime_root / "cache"))
        environment.setdefault("XDG_STATE_HOME", str(runtime_root / "state"))
        for key in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            Path(environment[key]).mkdir(parents=True, exist_ok=True)
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
        process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_chunks: list[bytes] = []

        def drain_stderr() -> None:
            while chunk := process.stderr.read(16 * 1024):
                stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        lines: list[str] = []
        for raw_line in process.stdout:
            lines.append(raw_line.decode("utf-8", errors="replace"))
        code = process.wait()
        stderr_thread.join(timeout=2)
        final_text, diagnostics = extract_final_text(lines)
        if code != 0:
            detail = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            if detail:
                print(detail[-4000:], file=sys.stderr, flush=True)
            return code
        if not final_text:
            detail = diagnostics[-1] if diagnostics else "OpenCode 没有返回最终文本"
            print(f"OpenCode 结果无效：{detail}", file=sys.stderr, flush=True)
            return 3
        sys.stdout.write(final_text)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0
    except OSError as exc:
        print(f"cannot start OpenCode: {exc}", file=sys.stderr, flush=True)
        return 127
