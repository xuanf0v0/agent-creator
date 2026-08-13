"""Agent Capability Registry 单元测试 — Creator Harness Layer 1"""

from __future__ import annotations

import pytest
from openagent_studio.creator import AgentCapabilityRegistry, RegistryError, AgentCapability


def test_empty_registry_not_ready():
    registry = AgentCapabilityRegistry()
    assert registry.is_ready() is False
    assert registry.get_agents() == []


def test_load_from_spec():
    registry = AgentCapabilityRegistry()
    spec = {
        "harness": [
            {
                "id": "coding-agent",
                "name": "Coding Agent",
                "description": "代码开发智能体",
                "backend_id": "default",
                "labels": {"agent-harness/capability": "text-generation", "agent-harness/sandbox": "read-write"},
            },
            {
                "id": "knowledge-agent",
                "name": "Knowledge Agent",
                "description": "知识检索智能体",
                "backend_id": "default",
                "labels": {"agent-harness/capability": "repository-analysis", "agent-harness/sandbox": "read-only"},
            },
        ]
    }
    registry.load_from_spec(spec)
    assert registry.is_ready() is True
    assert len(registry.get_agents()) == 2


def test_get_agent_by_id():
    registry = AgentCapabilityRegistry()
    spec = {
        "harness": [
            {"id": "agent-1", "name": "Agent One", "labels": {}},
        ]
    }
    registry.load_from_spec(spec)
    agent = registry.get_agent("agent-1")
    assert agent is not None
    assert agent.name == "Agent One"
    assert registry.get_agent("nonexistent") is None


def test_get_agents_by_capability():
    registry = AgentCapabilityRegistry()
    spec = {
        "harness": [
            {"id": "a1", "name": "Text Gen", "labels": {"agent-harness/capability": "text-generation"}},
            {"id": "a2", "name": "Repo Analysis", "labels": {"agent-harness/capability": "repository-analysis"}},
            {"id": "a3", "name": "Test Exec", "labels": {"agent-harness/capability": "test-execution"}},
        ]
    }
    registry.load_from_spec(spec)
    text_agents = registry.get_agents_by_capability("text-generation")
    assert len(text_agents) == 1
    assert text_agents[0].name == "Text Gen"


def test_get_agents_for_node_type():
    """text-generation capability 支持 llm, agent, tool, code 节点类型。"""
    registry = AgentCapabilityRegistry()
    spec = {
        "harness": [
            {"id": "a1", "name": "Coding", "labels": {"agent-harness/capability": "text-generation"}},
        ]
    }
    registry.load_from_spec(spec)
    llm_agents = registry.get_agents_for_node_type("llm")
    assert len(llm_agents) == 1
    tool_agents = registry.get_agents_for_node_type("tool")
    assert len(tool_agents) == 1
    # 不支持的节点类型
    http_agents = registry.get_agents_for_node_type("http_request")
    assert len(http_agents) == 0


def test_get_supported_node_types():
    registry = AgentCapabilityRegistry()
    spec = {
        "harness": [
            {"id": "a1", "name": "Coding", "labels": {"agent-harness/capability": "text-generation"}},
        ]
    }
    registry.load_from_spec(spec)
    types = registry.get_supported_node_types("a1")
    assert "llm" in types
    assert "agent" in types
    assert "tool" in types
    assert "code" in types
    assert "http_request" not in types


def test_get_supported_node_types_unknown_agent():
    registry = AgentCapabilityRegistry()
    assert registry.get_supported_node_types("nonexistent") == []


def test_load_from_empty_spec():
    registry = AgentCapabilityRegistry()
    registry.load_from_spec({"harness": []})
    assert registry.is_ready() is False


def test_load_from_spec_missing_id():
    """缺少 ID 的 agent 条目应被跳过。"""
    registry = AgentCapabilityRegistry()
    spec = {
        "harness": [
            {"name": "No ID"},
            {"id": "valid", "name": "Valid Agent"},
        ]
    }
    registry.load_from_spec(spec)
    assert len(registry.get_agents()) == 1
    assert registry.get_agent("valid") is not None


def test_capability_node_type_mapping():
    """验证能力到节点类型的映射正确性。"""
    from openagent_studio.creator.registry import CAPABILITY_NODE_TYPE_MAP
    assert "text-generation" in CAPABILITY_NODE_TYPE_MAP
    assert "llm" in CAPABILITY_NODE_TYPE_MAP["text-generation"]
    assert "agent" in CAPABILITY_NODE_TYPE_MAP["text-generation"]
    assert "repository-analysis" in CAPABILITY_NODE_TYPE_MAP
    assert "knowledge_retrieval" in CAPABILITY_NODE_TYPE_MAP["repository-analysis"]
    assert "test-execution" in CAPABILITY_NODE_TYPE_MAP
    assert "code" in CAPABILITY_NODE_TYPE_MAP["test-execution"]


def test_agent_capability_dataclass():
    cap = AgentCapability(
        agent_id="agent-1",
        name="Test Agent",
        description="测试",
        capability="text-generation",
        sandbox="read-write",
        supported_node_types=["llm", "agent"],
        backend_id="default",
        ready=True,
    )
    assert cap.agent_id == "agent-1"
    assert cap.supported_node_types == ["llm", "agent"]

    # 测试默认值
    cap_default = AgentCapability(agent_id="a1", name="A1", description="", capability="", sandbox="")
    assert cap_default.ready is True
    assert cap_default.backend_id == "default"


def test_sync_with_general_harness_unreachable():
    """通用 Harness 不可达时，应优雅降级。"""
    registry = AgentCapabilityRegistry()
    spec = {
        "harness": [
            {"id": "a1", "name": "Local Agent", "labels": {"agent-harness/capability": "text-generation"}},
        ]
    }
    registry.load_from_spec(spec)
    # 使用一个不可能连通的地址
    result = registry.sync_with_general_harness("http://127.0.0.1:1")
    assert result is False
    assert registry.is_harness_connected() is False
    # 本地数据应保留
    assert registry.is_ready() is True


def test_is_harness_connected_default():
    registry = AgentCapabilityRegistry()
    assert registry.is_harness_connected() is False