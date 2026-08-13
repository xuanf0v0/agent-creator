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
import re
from typing import Any, Callable, Mapping


def emit_diagnostic(code: str, summary: str, *, phase: str = "agent", exit_code: int | None = None) -> None:
    """Emit the optional framework-neutral Harness diagnostic envelope."""
    allowed = {
        "agent_process_failed", "agent_timeout", "agent_permission_denied",
        "sandbox_unavailable", "sandbox_denied", "protocol_output_invalid",
        "verification_failed", "setup_required",
    }
    payload: dict[str, Any] = {"code": code if code in allowed else "agent_process_failed", "phase": phase, "summary": str(summary)[-1000:]}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    print("AGENT_HARNESS_ERROR " + json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


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
        # Windows npm exposes OpenCode through a ``.cmd`` shim.  Passing that
        # shim to CreateProcess can leave stdin/prompt handling to cmd.exe and
        # silently drop positional message arguments under Harness. Resolve
        # the real executable embedded in shim before launching it.
        if Path(resolved).suffix.lower() == ".cmd":
            try:
                shim = Path(resolved)
                text = shim.read_text(encoding="utf-8", errors="replace")
                match = re.search(r'"%(?:~dp0|dp0)%\\([^"\r\n]+?\.exe)"', text, flags=re.IGNORECASE)
                if match:
                    candidate = (shim.parent / Path(match.group(1))).resolve()
                    if candidate.is_file():
                        return str(candidate)
            except OSError:
                pass
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


def is_non_execution_response(text: str) -> bool:
    """Detect agents that acknowledged governance but did not execute task."""
    normalized = " ".join(str(text).lower().split())
    if not normalized:
        return True
    markers = (
        "no task", "task empty", "what task", "give task", "task?",
        "ready for next", "no task content", "source noted",
        "rules loaded", "rules noted", "await task", "rules here:",
        "task: say what need", "task: provide", "what need",
    )
    return any(marker in normalized for marker in markers)


def run_with_protocol_retry(
    invoke: Callable[[], tuple[int, str, list[str], str]],
) -> tuple[int, str, list[str], str, int]:
    """Retry once only when OpenCode acknowledges governance without executing."""
    for attempt in (1, 2):
        code, final_text, diagnostics, stderr_text = invoke()
        if code != 0 or not final_text or not is_non_execution_response(final_text) or attempt == 2:
            return code, final_text, diagnostics, stderr_text, attempt
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an Agent Harness task through the real OpenCode CLI")
    parser.add_argument("--model", required=True)
    parser.add_argument("--agent", default="openagent-runtime-text")
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
        emit_diagnostic("agent_process_failed", str(exc), phase="invocation")
        print(f"invalid Harness invocation: {exc}", file=sys.stderr, flush=True)
        return 2
    task = payload.get("task", {})
    command = [
        binary,
        "run",
        "--pure",
        "--print-logs",
        "--log-level",
        "WARN",
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
        def invoke_once() -> tuple[int, str, list[str], str]:
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
            stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            return code, final_text, diagnostics, stderr_text

        code, final_text, diagnostics, stderr_text, attempts = run_with_protocol_retry(invoke_once)
        # 治理性提示（未真正执行任务）无论退出码都视为协议无效，避免把启动提示当结果。
        if final_text and is_non_execution_response(final_text):
            detail = f"OpenCode 连续 {attempts} 次仅返回启动/治理提示，未执行 Harness 任务"
            emit_diagnostic("protocol_output_invalid", detail, phase="protocol", exit_code=3)
            print(detail, file=sys.stderr, flush=True)
            return 3
        # 模型已产出文本（哪怕进程退出码非 0），也把该文本作为合法结果返回。
        # 对 test-runner 这类 agent，运行测试后报告"测试失败及原因"是任务成功完成，
        # 而不是基础设施崩溃；进程退出码非 0 不应吞掉已经生成的失败摘要。
        if final_text:
            if code != 0:
                emit_diagnostic("agent_process_failed", stderr_text or "OpenCode process exited non-zero", phase="opencode", exit_code=code)
            sys.stdout.write(final_text)
            sys.stdout.write("\n")
            sys.stdout.flush()
            return 0
        if code != 0:
            detail = stderr_text
            if not detail and diagnostics:
                detail = diagnostics[-1]
            lowered = detail.lower()
            diagnostic_code = "agent_permission_denied" if "permission requested" in lowered or "external_directory" in lowered else "agent_process_failed"
            emit_diagnostic(diagnostic_code, detail or "OpenCode process failed", phase="opencode", exit_code=code)
            if detail:
                print(detail[-4000:], file=sys.stderr, flush=True)
            return code
        detail = diagnostics[-1] if diagnostics else "OpenCode 没有返回最终文本"
        emit_diagnostic("protocol_output_invalid", detail, phase="protocol", exit_code=3)
        print(f"OpenCode 结果无效：{detail}", file=sys.stderr, flush=True)
        return 3
    except OSError as exc:
        emit_diagnostic("agent_process_failed", str(exc), phase="launch", exit_code=127)
        print(f"cannot start OpenCode: {exc}", file=sys.stderr, flush=True)
        return 127
