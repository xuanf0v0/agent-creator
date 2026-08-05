from __future__ import annotations

from pathlib import Path
import os
import warnings

import yaml
from pydantic import ValidationError

from .models import AgentManifest


class ManifestError(ValueError):
    pass


class AgentCatalog:
    def __init__(self, agents: list[AgentManifest], manifest_dir: Path | None = None, allowed_roots: list[Path] | None = None) -> None:
        ids = [agent.id for agent in agents]
        ports = [agent.service.port for agent in agents if agent.service is not None]
        if len(ids) != len(set(ids)):
            raise ManifestError("duplicate agent id")
        if len(ports) != len(set(ports)):
            raise ManifestError("duplicate agent port")
        self._agents = {agent.id: agent for agent in agents}
        self.manifest_dir = manifest_dir.resolve() if manifest_dir else None
        self.allowed_roots = tuple(path.resolve() for path in (allowed_roots or ([] if manifest_dir is None else [manifest_dir.resolve().parent])))

    @classmethod
    def load(cls, manifest_dir: Path, allowed_roots: list[Path] | None = None) -> AgentCatalog:
        manifest_dir = manifest_dir.resolve()
        if not manifest_dir.is_dir():
            raise ManifestError(f"manifest directory does not exist: {manifest_dir}")
        agents: list[AgentManifest] = []
        for path in sorted([*manifest_dir.glob("*.yaml"), *manifest_dir.glob("*.yml")]):
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ManifestError("manifest root must be a mapping")
                if "command" in payload or "port" in payload or "setup_command" in payload:
                    warnings.warn(
                        f"{path}: flat service fields are deprecated; use service/environment",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                agent = AgentManifest.model_validate(payload)
                agent.source_file = path.resolve()
                roots = [item.resolve() for item in (allowed_roots or [manifest_dir.parent])]
                agent.cwd = cls._resolve_path(path.parent, agent.cwd, roots)
                agent.env_file = cls._resolve_path(agent.cwd, agent.env_file, agent.cwd)
                agent.environment.fingerprint_files = [
                    cls._resolve_path(agent.cwd, value, agent.cwd)
                    for value in agent.environment.fingerprint_files
                ]
                agents.append(agent)
            except (OSError, yaml.YAMLError, ValidationError, ManifestError) as exc:
                raise ManifestError(f"{path}: {exc}") from exc
        if not agents:
            raise ManifestError(f"no YAML manifests found in {manifest_dir}")
        return cls(agents, manifest_dir, allowed_roots)

    def reload(self) -> dict[str, list[str]]:
        if self.manifest_dir is None:
            raise ManifestError("catalog has no manifest directory")
        fresh = self.load(self.manifest_dir, list(self.allowed_roots))
        before, after = set(self._agents), set(fresh._agents)
        self._agents = fresh._agents
        return {"added": sorted(after - before), "removed": sorted(before - after), "retained": sorted(before & after)}

    @staticmethod
    def _resolve_path(base: Path, value: Path, allowed_root: Path | list[Path]) -> Path:
        result = value if value.is_absolute() else base / value
        result = result.resolve()
        roots = allowed_root if isinstance(allowed_root, list) else [allowed_root]
        if not any(_is_relative_to(result, root.resolve()) for root in roots):
            raise ManifestError(f"path escapes allowed roots {', '.join(map(str, roots))}: {value}")
        return result

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._agents.get(agent_id)

    def require(self, agent_id: str) -> AgentManifest:
        agent = self.get(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        return agent

    def all(self) -> list[AgentManifest]:
        return list(self._agents.values())


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def configured_allowed_roots() -> list[Path] | None:
    """Return operator-owned workspace roots, or None for the safe legacy root."""
    value = os.environ.get("AGENT_HARNESS_ALLOWED_ROOTS", "")
    if not value.strip():
        return None
    return [Path(item).expanduser().resolve() for item in value.split(os.pathsep) if item.strip()]
