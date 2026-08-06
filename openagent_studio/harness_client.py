from __future__ import annotations

import os
import re
from dataclasses import dataclass

from agent_harness_sdk import HarnessClient


DEFAULT_BACKEND_ID = "default"
DEFAULT_HARNESS_URL = "http://127.0.0.1:8765"


@dataclass(frozen=True)
class HarnessBackendSettings:
    backend_id: str
    base_url: str
    task_token: str


def _backend_env_prefix(backend_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", backend_id).strip("_").upper()
    return f"AGENT_HARNESS_{normalized}"


def harness_backend_settings(backend_id: str = DEFAULT_BACKEND_ID, *, base_url: str | None = None) -> HarnessBackendSettings:
    if backend_id == DEFAULT_BACKEND_ID:
        url = base_url or os.environ.get("AGENT_HARNESS_URL", DEFAULT_HARNESS_URL)
        token = os.environ.get("AGENT_HARNESS_TASK_TOKEN", "")
    else:
        prefix = _backend_env_prefix(backend_id)
        url = base_url or os.environ.get(f"{prefix}_URL", "")
        token = os.environ.get(f"{prefix}_TASK_TOKEN", "")
        if not url:
            raise RuntimeError(
                f"Harness 后端 {backend_id} 未配置；请设置 {prefix}_URL 和 {prefix}_TASK_TOKEN"
            )
    return HarnessBackendSettings(backend_id, url.rstrip("/"), token)


def create_harness_client(
    backend_id: str = DEFAULT_BACKEND_ID,
    *,
    base_url: str | None = None,
    timeout: float | None = 30,
) -> HarnessClient:
    settings = harness_backend_settings(backend_id, base_url=base_url)
    return HarnessClient(settings.base_url, token=settings.task_token, timeout=timeout)
