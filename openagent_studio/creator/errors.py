from __future__ import annotations


class CreatorHarnessError(Exception):
    """Creator Harness 基类错误。"""


class RegistryError(CreatorHarnessError):
    """Agent 能力注册表错误。"""


class IntentError(CreatorHarnessError):
    """意图解析错误。"""


class DecisionError(CreatorHarnessError):
    """决策生成错误。"""


class GenerationError(CreatorHarnessError):
    """工作流生成错误。"""