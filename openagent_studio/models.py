from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    description: str = ""
    backend_id: str = Field(default="default", pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    agent_id: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    labels: dict[str, str] = Field(default_factory=dict)
    protocol: str = "stdin_json"

    @model_validator(mode="after")
    def default_agent_id(self):
        if self.agent_id is None:
            self.agent_id = self.id
        return self

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


class EvaluationAssertion(BaseModel):
    path: str = "output"
    operator: Literal["exists", "equals", "contains", "matches", "type"] = "exists"
    expected: Any = None


class EvaluationMock(BaseModel):
    node_id: str
    response: Any = None


class EvaluationCase(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    enabled: bool = True
    input: Any = ""
    assertions: list[EvaluationAssertion] = Field(default_factory=list)
    semantic_criteria: list[str] = Field(default_factory=list)
    approvals: dict[str, bool] = Field(default_factory=dict)
    mocks: list[EvaluationMock] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, ge=1, le=1800)


class WorkflowEvaluation(BaseModel):
    cases: list[EvaluationCase] = Field(default_factory=list)


class WorkflowSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    evaluation: WorkflowEvaluation = Field(default_factory=WorkflowEvaluation)


class FeishuIntegrationSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = "飞书机器人"
    workflow_id: str
    app_id_env: str = "FEISHU_APP_ID"
    app_secret_env: str = "FEISHU_APP_SECRET"
    verification_token_env: str = "FEISHU_VERIFICATION_TOKEN"
    encrypt_key_env: str | None = "FEISHU_ENCRYPT_KEY"
    env_file: str | None = None
    auto_reply: bool = True


class QQIntegrationSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = "QQ 机器人"
    workflow_id: str
    app_id_env: str = "QQ_BOT_APP_ID"
    secret_env: str = "QQ_BOT_SECRET"
    env_file: str | None = None
    auto_reply: bool = True


class IntegrationSpec(BaseModel):
    feishu: list[FeishuIntegrationSpec] = Field(default_factory=list)
    qq: list[QQIntegrationSpec] = Field(default_factory=list)


class ProjectSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: Literal["1"] = "1"
    name: str
    project_dir: str = "."
    agents: list[AgentSpec] = Field(default_factory=list)
    providers: list[ProviderSpec] = Field(default_factory=list)
    harness: list[HarnessSpec] = Field(default_factory=list)
    workflows: list[WorkflowSpec] = Field(default_factory=list)
    integrations: IntegrationSpec = Field(default_factory=IntegrationSpec)

    @field_validator("agents", "providers", "harness", "workflows")
    @classmethod
    def unique_ids(cls, values):
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("ids must be unique within each collection")
        return values

    @field_validator("integrations")
    @classmethod
    def valid_integrations(cls, value: IntegrationSpec) -> IntegrationSpec:
        for items in (value.feishu, value.qq):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError("integration ids must be unique within each platform")
        return value

    @model_validator(mode="after")
    def integration_workflows_exist(self):
        workflow_ids = {item.id for item in self.workflows}
        invalid = [f"{platform}:{item.id}" for platform, items in (("feishu", self.integrations.feishu), ("qq", self.integrations.qq)) for item in items if item.workflow_id not in workflow_ids]
        if invalid:
            raise ValueError(f"integrations reference unknown workflows: {', '.join(invalid)}")
        return self
