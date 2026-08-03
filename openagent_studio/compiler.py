from __future__ import annotations

from typing import Any
import yaml
from .models import ProjectSpec


def compile_opencode(spec: ProjectSpec) -> dict[str, Any]:
    agents: dict[str, Any] = {}
    for agent in spec.agents:
        value = agent.model_dump(exclude_none=True, by_alias=False)
        value.pop("id", None)
        value["maxSteps"] = value.pop("max_steps", None) if "max_steps" in value else None
        value = {k: v for k, v in value.items() if v is not None}
        if "top_p" in value:
            value["top_p"] = value.pop("top_p")
        agents[agent.id] = value
    providers: dict[str, Any] = {}
    for provider in spec.providers:
        value = provider.model_dump(exclude_none=True)
        value.pop("id", None)
        if provider.base_url:
            value.setdefault("options", {})["baseURL"] = provider.base_url
        if provider.api_key_env:
            value.setdefault("env", []).append(provider.api_key_env)
        value.pop("base_url", None)
        value.pop("api_key_env", None)
        providers[provider.id] = value
    return {"$schema": "https://opencode.ai/config.json", "agent": agents, "provider": providers}


def compile_harness(spec: ProjectSpec, agent_id: str) -> str:
    item = next((x for x in spec.harness if x.id == agent_id), None)
    if item is None:
        raise KeyError(agent_id)
    data = item.model_dump(exclude_none=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
