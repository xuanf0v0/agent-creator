from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .models import AgentManifest, ConfigField


def _read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return lines, values


def _masked(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}********{value[-4:]}"


class ConfigStore:
    def get(self, agent: AgentManifest) -> list[dict[str, Any]]:
        _, values = _read_env(agent.env_file)
        result = []
        for field in agent.config:
            value = values.get(field.key, field.default)
            secret = field.type == "secret"
            result.append(
                {
                    **field.model_dump(),
                    "value": _masked(value) if secret else value,
                    "is_secret": secret,
                    "is_masked": secret and bool(value),
                }
            )
        return result

    def update(self, agent: AgentManifest, updates: dict[str, str]) -> list[dict[str, Any]]:
        fields = {field.key: field for field in agent.config}
        unknown = set(updates) - set(fields)
        if unknown:
            raise ValueError(f"unknown config fields: {', '.join(sorted(unknown))}")
        lines, existing = _read_env(agent.env_file)
        cleaned: dict[str, str] = {}
        for key, value in updates.items():
            field = fields[key]
            if field.type == "secret" and "*" in value:
                continue
            self._validate(field, value)
            cleaned[key] = value
        output: list[str] = []
        replaced: set[str] = set()
        for line in lines:
            stripped = line.strip()
            key = stripped.partition("=")[0].strip() if "=" in stripped else ""
            if key in cleaned and not stripped.startswith("#"):
                output.append(f"{key}={cleaned[key]}\n")
                replaced.add(key)
            else:
                output.append(line)
        for key, value in cleaned.items():
            if key not in replaced:
                output.append(f"{key}={value}\n")
        if cleaned:
            agent.env_file.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(agent.env_file, "".join(output))
        return self.get(agent)

    @staticmethod
    def _validate(field: ConfigField, value: str) -> None:
        if field.type == "boolean" and value.lower() not in {"true", "false"}:
            raise ValueError(f"{field.key} must be true or false")
        if field.type == "number":
            try:
                float(value)
            except ValueError as exc:
                raise ValueError(f"{field.key} must be a number") from exc
        if field.type == "select" and value not in field.options:
            raise ValueError(f"{field.key} must be one of {field.options}")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            if os.name != "nt":
                os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
