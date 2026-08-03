from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


WORKFLOW_NODE_TYPES = {
    "manual_trigger", "webhook", "schedule",
    "llm", "agent", "knowledge_retrieval", "tool", "http_request", "code",
    "prompt", "variable_set", "transform", "merge",
    "condition", "switch", "parallel", "iteration", "loop",
    "approval", "validator", "subworkflow", "delay", "output",
}


class PermissionConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    edit: Literal["ask", "allow", "deny"] = "ask"
    bash: Literal["ask", "allow", "deny"] = "ask"
    webfetch: Literal["ask", "allow", "deny"] = "ask"


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    description: str = ""
    mode: Literal["primary", "subagent", "all"] = "all"
    model: str | None = None
    prompt: str = ""
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_steps: int | None = Field(default=None, ge=1, le=1000)
    tools: dict[str, bool] = Field(default_factory=dict)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str | None = None
    npm: str = "@ai-sdk/openai-compatible"
    base_url: str | None = None
    api_key_env: str | None = None
    env_file: str | None = None
    models: dict[str, dict[str, Any]] = Field(default_factory=dict)


class HarnessSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    description: str = ""
    cwd: str
    service: dict[str, Any] | None = None
    task: dict[str, Any] | None = None
    env_file: str = ".env"
    config: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowNode(BaseModel):
    id: str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    position: dict[str, float] = Field(default_factory=lambda: {"x": 80, "y": 80})

    @field_validator("type")
    @classmethod
    def supported_type(cls, value: str) -> str:
        if value not in WORKFLOW_NODE_TYPES:
            raise ValueError(f"unsupported workflow node type: {value}")
        return value


class WorkflowEdge(BaseModel):
    source: str
    target: str
    condition: str | None = None


class WorkflowSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)


class ProjectSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: Literal["1"] = "1"
    name: str
    project_dir: str = "."
    agents: list[AgentSpec] = Field(default_factory=list)
    providers: list[ProviderSpec] = Field(default_factory=list)
    harness: list[HarnessSpec] = Field(default_factory=list)
    workflows: list[WorkflowSpec] = Field(default_factory=list)

    @field_validator("agents", "providers", "harness", "workflows")
    @classmethod
    def unique_ids(cls, values):
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("ids must be unique within each collection")
        return values
