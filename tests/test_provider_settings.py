import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openagent_studio.app import create_app
from openagent_studio.provider_settings import ProviderSettings


def test_provider_settings_are_masked_and_injected_at_runtime(tmp_path: Path):
    app = create_app(tmp_path / "project.yaml")
    client = TestClient(app)

    assert client.get("/api/settings/provider").json() == {
        "configured": False,
        "provider": "openai",
        "protocol": "openai-chat",
        "base_url": "",
        "model": "gpt-4o-mini",
        "api_key_configured": False,
        "api_key_masked": "",
    }

    secret = "sk-local-test-secret"
    response = client.put(
        "/api/settings/provider",
        json={
            "protocol": "openai-responses",
            "base_url": "https://api.example.test/v1/",
            "model": "gpt-5-mini",
            "api_key": secret,
        },
    )
    assert response.status_code == 200
    public = response.json()
    assert public["configured"] is True
    assert public["provider"] == "openai"
    assert public["protocol"] == "openai-responses"
    assert public["base_url"] == "https://api.example.test/v1"
    assert public["model_ref"] == "openai/gpt-5-mini"
    assert secret not in response.text

    settings_path = tmp_path / ".openagent-provider-settings.json"
    assert secret in settings_path.read_text(encoding="utf-8")

    manager = app.state.generator_manager
    environment = manager._environment(app.state.store.load())
    assert environment["OPENAGENT_PROVIDER_API_KEY"] == secret
    assert secret not in environment["OPENCODE_CONFIG_CONTENT"]
    runtime_config = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    assert runtime_config["provider"]["openai"]["npm"] == "@ai-sdk/openai"
    assert runtime_config["provider"]["openai"]["options"]["apiKey"] == "{env:OPENAGENT_PROVIDER_API_KEY}"
    assert manager.model() == "openai/gpt-5-mini"


def test_provider_settings_preserve_key_when_form_leaves_it_blank(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml"))
    secret = "anthropic-local-test-secret"
    assert client.put(
        "/api/settings/provider",
        json={
            "protocol": "anthropic-messages",
            "base_url": "https://api.anthropic.example/v1",
            "model": "claude-sonnet-4-20250514",
            "api_key": secret,
        },
    ).status_code == 200

    updated = client.put(
        "/api/settings/provider",
        json={
            "protocol": "anthropic-messages",
            "base_url": "https://api.anthropic.example/v1",
            "model": "claude-3-7-sonnet-latest",
        },
    )
    assert updated.status_code == 200
    assert secret not in updated.text
    assert client.get("/api/settings/provider").json()["model"] == "claude-3-7-sonnet-latest"
    assert client.app.state.provider_settings.load().api_key == secret


def test_provider_settings_reject_unsafe_base_url_and_can_revert(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml"))
    invalid = client.put(
        "/api/settings/provider",
        json={
            "protocol": "openai-chat",
            "base_url": "https://user:password@example.test/v1?token=secret",
            "model": "gpt-4o-mini",
            "api_key": "secret",
        },
    )
    assert invalid.status_code == 422
    assert client.delete("/api/settings/provider").json()["configured"] is False


@pytest.mark.parametrize(
    ("protocol", "provider_id", "npm"),
    [
        ("openai-responses", "openai", "@ai-sdk/openai"),
        ("openai-chat", "openai-chat", "@ai-sdk/openai-compatible"),
        ("anthropic-messages", "anthropic", "@ai-sdk/anthropic"),
    ],
)
def test_all_supported_protocols_compile_to_real_opencode_providers(protocol: str, provider_id: str, npm: str):
    settings = ProviderSettings(
        protocol=protocol,
        base_url="https://api.example.test/v1",
        model="model-test",
        api_key="secret",
    )
    provider = settings.opencode_config()["provider"][provider_id]
    assert provider["npm"] == npm
    assert provider["options"]["baseURL"] == "https://api.example.test/v1"
    assert provider["options"]["apiKey"] == "{env:OPENAGENT_PROVIDER_API_KEY}"
