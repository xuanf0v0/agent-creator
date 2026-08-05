from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentState(str, Enum):
    STOPPED = "stopped"
    SETTING_UP = "setting_up"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class HealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "http"
    path: str = "/health"
    url: str | None = None
    host: str = "127.0.0.1"
    port: int | None = Field(default=None, ge=1, le=65535)
    command: list[str] | None = None
    expected_statuses: list[int] = Field(default_factory=lambda: list(range(200, 300)))
    timeout_seconds: float = Field(default=15, gt=0, le=300)
    interval_seconds: float = Field(default=0.15, gt=0, le=10)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("health path must start with /")
        return value

    @model_validator(mode="after")
    def built_in_health_is_valid(self):
        if self.kind == "command" and not self.command:
            raise ValueError("command health check requires command")
        if self.kind not in {"http", "tcp", "command", "process", "none"} and ":" not in self.kind:
            raise ValueError("custom health check kinds must use module:attribute")
        return self


class ConfigField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    label: str
    type: Literal["boolean", "string", "secret", "number", "select"] = "string"
    default: str = ""
    options: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def select_has_options(self) -> ConfigField:
        if self.type == "select" and not self.options:
            raise ValueError("select field requires options")
        return self


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    command: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=600, gt=0, le=7200)


class EnvironmentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # An omitted command is an explicit internal no-op setup. Agents that need
    # reproducible dependency preparation should declare a real command.
    setup_command: list[str] | None = None
    setup_timeout_seconds: float = Field(default=1800, ge=10, le=7200)
    fingerprint_files: list[Path] = Field(default_factory=list)
    auto_setup_on_drift: bool = False

    @field_validator("setup_command")
    @classmethod
    def setup_argv_is_valid(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (not value or any(not item.strip() for item in value)):
            raise ValueError("setup_command entries must not be empty")
        return value


class BackendSpec(BaseModel):
    """A built-in backend name or a module:attribute plugin plus opaque options."""

    model_config = ConfigDict(extra="forbid")
    kind: str
    options: dict[str, Any] = Field(default_factory=dict)


class DeploymentSpec(BackendSpec):
    kind: str = "local_process"


class ServiceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    endpoint: str | None = None
    deployment: DeploymentSpec = Field(default_factory=DeploymentSpec)
    health: HealthCheck = Field(default_factory=HealthCheck)

    @model_validator(mode="after")
    def deployment_is_valid(self):
        if self.deployment.kind == "local_process" and not self.command:
            raise ValueError("local_process deployment requires command")
        if self.deployment.kind == "external" and not (self.endpoint or self.health.url):
            raise ValueError("external deployment requires endpoint or health.url")
        if self.health.kind in {"http", "tcp"} and not (self.health.url or self.health.port or self.port):
            raise ValueError(f"{self.health.kind} health check requires a port or URL")
        return self


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def categories_are_disjoint(self):
        groups = {"allow": set(self.allow), "ask": set(self.ask), "deny": set(self.deny)}
        overlap = (groups["allow"] & groups["ask"]) | (groups["allow"] & groups["deny"]) | (groups["ask"] & groups["deny"])
        if overlap:
            raise ValueError(f"tool categories overlap: {', '.join(sorted(overlap))}")
        return self


class RuntimeSandbox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    backend: str = "auto"
    backend_options: dict[str, Any] = Field(default_factory=dict)
    enforcement: Literal["required", "best_effort"] = "required"
    network: Literal["deny", "allowlist", "allow"] = "deny"
    network_allowlist: list[str] = Field(default_factory=list)
    workspace_write: bool = True

    @model_validator(mode="after")
    def allowlist_matches_mode(self):
        if self.network == "allowlist" and not self.network_allowlist:
            raise ValueError("sandbox.network_allowlist is required when network=allowlist")
        if self.network != "allowlist" and self.network_allowlist:
            raise ValueError("sandbox.network_allowlist is only valid when network=allowlist")
        return self


class TaskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_timeout_seconds: float = Field(default=3600, ge=10, le=86_400)
    attempt_timeout_seconds: float = Field(default=5400, ge=30, le=172_800)
    max_log_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    max_log_lines: int = Field(default=50_000, ge=100, le=1_000_000)
    max_queue_depth: int = Field(default=100, ge=1, le=10_000)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] | None = None
    protocol: BackendSpec = Field(default_factory=lambda: BackendSpec(kind="stdin_json"))
    verification: list[VerificationCheck] = Field(min_length=1)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    sandbox: RuntimeSandbox = Field(default_factory=RuntimeSandbox)
    limits: TaskLimits = Field(default_factory=TaskLimits)

    @model_validator(mode="after")
    def denied_tools_are_enforced(self):
        denied = set(self.tools.deny)
        if denied & {"write", "edit"}:
            self.sandbox.workspace_write = False
        if "network" in denied and self.sandbox.network != "deny":
            raise ValueError("tools.deny network requires sandbox.network=deny")
        if self.protocol.kind in {"stdin_json", "argv"} and not self.command:
            raise ValueError(f"{self.protocol.kind} task protocol requires command")
        if self.protocol.kind in {"http", "mcp"} and not self.protocol.options.get("url"):
            raise ValueError(f"{self.protocol.kind} task protocol requires protocol.options.url")
        if self.protocol.kind not in {"stdin_json", "argv", "http", "mcp"} and ":" not in self.protocol.kind:
            raise ValueError("custom task protocol kinds must use module:attribute")
        return self


class AgentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    description: str = ""
    icon: str = "🤖"
    cwd: Path
    service: ServiceSpec | None = None
    task: TaskSpec | None = None
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    # Deprecated flat fields are retained for old service manifests.
    command: list[str] | None = None
    setup_command: list[str] | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    health: HealthCheck | None = None
    env_file: Path = Path(".env")
    config: list[ConfigField] = Field(default_factory=list)
    source_file: Path | None = Field(default=None, exclude=True)

    @field_validator("command", "setup_command")
    @classmethod
    def argv_is_valid(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (not value or any(not item.strip() for item in value)):
            raise ValueError("command must be a non-empty argv array")
        return value

    @model_validator(mode="after")
    def normalize_and_validate(self) -> AgentManifest:
        if self.service is None and self.command is not None:
            if self.port is None:
                raise ValueError("legacy service command requires port")
            self.service = ServiceSpec(command=self.command, port=self.port, health=self.health or HealthCheck())
        if self.service is None and self.task is None:
            raise ValueError("manifest must declare service or task")
        if self.setup_command is not None:
            self.environment.setup_command = self.setup_command
        keys = [field.key for field in self.config]
        if len(keys) != len(set(keys)):
            raise ValueError("config field keys must be unique")
        return self


class AgentStatus(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    port: int | None = None
    status: AgentState
    pid: int = 0
    started_at: float = 0
    error_message: str = ""
    url: str | None = None
