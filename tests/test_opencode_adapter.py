from __future__ import annotations

import sys
from pathlib import Path


ADAPTER_SRC = Path(__file__).parents[1] / "adapters" / "opencode" / "src"
sys.path.insert(0, str(ADAPTER_SRC))

from openagent_harness_opencode import build_prompt  # noqa: E402


def test_opencode_adapter_prompt_contains_governance_and_task():
    prompt = build_prompt({"task": {"prompt": "修复测试"}, "instructions": "只能修改任务范围内的文件"})
    assert prompt == "只能修改任务范围内的文件\n\n用户任务：\n修复测试"
