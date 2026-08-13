from __future__ import annotations

import sys
from pathlib import Path


ADAPTER_SRC = Path(__file__).parents[1] / "adapters" / "opencode" / "src"
sys.path.insert(0, str(ADAPTER_SRC))

from openagent_harness_opencode import (
    build_prompt,
    extract_final_text,
    is_non_execution_response,
    resolve_executable,
    run_with_protocol_retry,
)  # noqa: E402


def test_opencode_adapter_prompt_contains_governance_and_task():
    prompt = build_prompt({"task": {"prompt": "修复测试"}, "instructions": "只能修改任务范围内的文件"})
    assert prompt == "只能修改任务范围内的文件\n\n用户任务：\n修复测试"


def test_opencode_adapter_extracts_final_text_without_protocol_logs():
    lines = [
        '{"type":"step_start","part":{"type":"step-start"}}',
        '{"type":"tool_use","part":{"type":"tool","tool":"grep"}}',
        '{"type":"text","part":{"type":"text","text":"第一段"}}',
        '{"type":"text","part":{"type":"text","text":"第二段"}}',
    ]
    text, diagnostics = extract_final_text(lines)
    assert text == "第一段第二段"
    assert diagnostics == []


def test_opencode_adapter_rejects_event_stream_without_model_text():
    text, diagnostics = extract_final_text([
        '{"type":"step_start","part":{"type":"step-start"}}',
        'not-json-diagnostic',
    ])
    assert text == ""
    assert diagnostics == ["not-json-diagnostic"]


def test_opencode_adapter_resolves_windows_cmd_shim_to_real_binary(tmp_path, monkeypatch):
    shim = tmp_path / "opencode.cmd"
    binary = tmp_path / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    shim.write_text(
        '@ECHO off\nSET dp0=%~dp0\n"%dp0%\\node_modules\\opencode-ai\\bin\\opencode.exe" %*\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    resolved = resolve_executable(str(shim), {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"})
    assert resolved == str(binary)


def test_opencode_adapter_rejects_ready_only_agent_response():
    assert is_non_execution_response("AGENTS.md loaded. Rules noted. Ready. Task?")
    assert is_non_execution_response("No task. Ready. Give input.")
    assert not is_non_execution_response("静态分析完成：发现 1 个问题，严重程度 high")


def test_opencode_adapter_retries_ready_only_response_once_then_succeeds():
    attempts = iter([
        (0, "Rules loaded. Ready. Task?", [], ""),
        (0, "检查完成：README.md 首行为 OpenAgent Studio", [], ""),
    ])
    result = run_with_protocol_retry(lambda: next(attempts))
    assert result == (0, "检查完成：README.md 首行为 OpenAgent Studio", [], "", 2)


def test_opencode_adapter_stops_after_two_ready_only_responses():
    calls = 0

    def invoke():
        nonlocal calls
        calls += 1
        return 0, "No task. Ready. Give input.", [], ""

    result = run_with_protocol_retry(invoke)
    assert calls == 2
    assert result[-1] == 2
    assert is_non_execution_response(result[1])
