from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


ProviderProtocol = Literal["openai-responses", "openai-chat", "anthropic-messages"]

DEFAULT_PROTOCOL: ProviderProtocol = "openai-chat"
DEFAULT_MODEL = "gpt-4o-mini"
API_KEY_ENV = "OPENAGENT_PROVIDER_API_KEY"


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: ProviderProtocol = DEFAULT_PROTOCOL
    base_url: str
    model: str = DEFAULT_MODEL
    api_key: str = Field(min_length=1, max_length=4096)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip()
        if any(character.isspace() for character in value):
            raise ValueError("Base URL 不能包含空白字符")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError("Base URL 不是有效 URL") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Base URL 必须是 http:// 或 https:// 地址")
        if parsed.username or parsed.password:
            raise ValueError("Base URL 不能包含用户名或密码")
        if parsed.query or parsed.fragment:
            raise ValueError("Base URL 不能包含 query 或 fragment")
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("模型编号不能为空且不能包含空格")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("API key 不能为空且不能包含空白字符")
        return value

    @property
    def provider_id(self) -> str:
        return {
            "openai-responses": "openai",
            "openai-chat": "openai-chat",
            "anthropic-messages": "anthropic",
        }[self.protocol]

    @property
    def provider_family(self) -> str:
        return "anthropic" if self.protocol == "anthropic-messages" else "openai"

    @property
    def npm(self) -> str:
        return {
            "openai-responses": "@ai-sdk/openai",
            "openai-chat": "@ai-sdk/openai-compatible",
            "anthropic-messages": "@ai-sdk/anthropic",
        }[self.protocol]

    @property
    def model_ref(self) -> str:
        return f"{self.provider_id}/{self.model}"

    def opencode_config(self) -> dict[str, Any]:
        return {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                self.provider_id: {
                    "name": self.provider_name,
                    "npm": self.npm,
                    "options": {
                        "baseURL": self.base_url,
                        "apiKey": f"{{env:{API_KEY_ENV}}}",
                    },
                    "models": {
                        self.model: {
                            "name": self.model,
                        },
                    },
                },
            },
        }

    @property
    def provider_name(self) -> str:
        return {
            "openai-responses": "OpenAI Responses API",
            "openai-chat": "OpenAI Chat Completions / Compatible",
            "anthropic-messages": "Anthropic Messages API",
        }[self.protocol]


class ProviderSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: ProviderProtocol = DEFAULT_PROTOCOL
    base_url: str
    model: str = DEFAULT_MODEL
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ProviderSettingsStore:
    """Persist one local Studio provider configuration without exposing its key."""

    def __init__(self, path: Path):
        self.path = path.expanduser()
        self._lock = RLock()

    def _load_unlocked(self) -> ProviderSettings | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return ProviderSettings.model_validate(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("本地模型设置文件损坏，请在全局设置中重新保存") from exc

    def load(self) -> ProviderSettings | None:
        with self._lock:
            return self._load_unlocked()

    def save(self, update: ProviderSettingsUpdate) -> ProviderSettings:
        with self._lock:
            current = self._load_unlocked()
            api_key = update.api_key or (current.api_key if current else "")
            if update.clear_api_key and not update.api_key:
                api_key = ""
            if not api_key:
                raise ValueError("请填写 API key，或先恢复项目原有配置")
            settings = ProviderSettings(
                protocol=update.protocol,
                base_url=update.base_url,
                model=update.model,
                api_key=api_key,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            return settings

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def public(self) -> dict[str, Any]:
        settings = self.load()
        if settings is None:
            return {
                "configured": False,
                "provider": "openai",
                "protocol": DEFAULT_PROTOCOL,
                "base_url": "",
                "model": DEFAULT_MODEL,
                "api_key_configured": False,
                "api_key_masked": "",
            }
        return {
            "configured": True,
            "provider": settings.provider_family,
            "protocol": settings.protocol,
            "base_url": settings.base_url,
            "model": settings.model,
            "api_key_configured": True,
            "api_key_masked": "••••••••",
        }

    def environment_overrides(self) -> dict[str, str]:
        settings = self.load()
        if settings is None:
            return {}
        return {
            API_KEY_ENV: settings.api_key,
            "OPENCODE_CONFIG_CONTENT": json.dumps(settings.opencode_config(), ensure_ascii=False, separators=(",", ":")),
        }
