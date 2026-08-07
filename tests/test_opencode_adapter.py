from __future__ import annotations

import sys
from pathlib import Path


ADAPTER_SRC = Path(__file__).parents[1] / "adapters" / "opencode" / "src"
sys.path.insert(0, str(ADAPTER_SRC))

from openagent_harness_opencode import build_prompt, extract_final_text  # noqa: E402


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
