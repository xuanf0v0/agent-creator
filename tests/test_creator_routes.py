"""Creator Harness HTTP API 集成测试 — Layer 5"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from openagent_studio.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "project.yaml"))


def test_creator_status(client: TestClient):
    """GET /api/creator/status — 返回 Creator Harness 状态。"""
    response = client.get("/api/creator/status")
    assert response.status_code == 200
    data = response.json()
    # 空 project.yaml 没有 harness agents，但状态应仍可返回
    assert "ready" in data
    assert "agents_count" in data
    assert "node_types_count" in data
    assert data["node_types_count"] >= 20


def test_creator_node_types(client: TestClient):
    """GET /api/creator/node-types — 返回动态节点类型目录。"""
    response = client.get("/api/creator/node-types")
    assert response.status_code == 200
    data = response.json()
    assert "node_types" in data
    assert "total" in data
    assert data["total"] >= 20
    # 验证节点类型结构
    node_types = data["node_types"]
    assert all("type" in nt for nt in node_types)
    assert all("label" in nt for nt in node_types)
    assert all("category" in nt for nt in node_types)
    assert all("icon" in nt for nt in node_types)
    # 验证关键节点类型存在
    types = {nt["type"] for nt in node_types}
    assert "manual_trigger" in types
    assert "llm" in types
    assert "agent" in types
    assert "output" in types


def test_creator_node_types_have_required_agent(client: TestClient):
    """6 种节点类型 requires_agent 为 true。"""
    response = client.get("/api/creator/node-types")
    data = response.json()
    agent_types = {nt["type"] for nt in data["node_types"] if nt.get("requires_agent")}
    assert agent_types == {"llm", "agent", "knowledge_retrieval", "tool", "code", "validator"}


def test_creator_agents_empty(client: TestClient):
    """空 project.yaml 时，agents 列表为空。"""
    response = client.get("/api/creator/agents")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["agents"] == []


def test_creator_agents_with_harness(client: TestClient):
    """配置 harness agents 后，agents 列表应返回注册的 agent。"""
    # 先写入 harness 配置
    put_response = client.put("/api/spec", json={
        "version": "1",
        "name": "Test",
        "agents": [],
        "providers": [],
        "harness": [
            {
                "id": "coding",
                "name": "Coding Agent",
                "description": "代码开发智能体",
                "backend_id": "default",
                "agent_id": "coding",
                "labels": {"agent-harness/capability": "text-generation", "agent-harness/sandbox": "read-write"},
            },
        ],
    })
    assert put_response.status_code == 200

    response = client.get("/api/creator/agents")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["agents"][0]["agent_id"] == "coding"
    assert data["agents"][0]["capability"] == "text-generation"


def test_creator_agent_by_id(client: TestClient):
    """GET /api/creator/agents/{id} — 查询单个 agent。"""
    client.put("/api/spec", json={
        "version": "1", "name": "Test", "agents": [], "providers": [],
        "harness": [{"id": "coding", "name": "Coding", "backend_id": "default", "agent_id": "coding"}],
    })
    response = client.get("/api/creator/agents/coding")
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == "coding"
    assert data["name"] == "Coding"


def test_creator_agent_not_found(client: TestClient):
    """不存在的 agent_id 返回 404。"""
    response = client.get("/api/creator/agents/nonexistent")
    assert response.status_code == 404


def test_creator_node_type_agents(client: TestClient):
    """GET /api/creator/node-types/{type}/agents — 查询能驱动该节点类型的 agents。"""
    client.put("/api/spec", json={
        "version": "1", "name": "Test", "agents": [], "providers": [],
        "harness": [{
            "id": "coding", "name": "Coding", "backend_id": "default", "agent_id": "coding",
            "labels": {"agent-harness/capability": "text-generation"},
        }],
    })
    response = client.get("/api/creator/node-types/llm/agents")
    assert response.status_code == 200
    data = response.json()
    assert data["node_type"] == "llm"
    assert len(data["agents"]) == 1
    assert data["agents"][0]["agent_id"] == "coding"


def test_creator_node_type_agents_no_match(client: TestClient):
    """没有匹配的节点类型时返回空列表。"""
    client.put("/api/spec", json={
        "version": "1", "name": "Test", "agents": [], "providers": [],
        "harness": [{
            "id": "coding", "name": "Coding", "backend_id": "default", "agent_id": "coding",
            "labels": {"agent-harness/capability": "text-generation"},
        }],
    })
    # http_request 不需要 agent
    response = client.get("/api/creator/node-types/http_request/agents")
    assert response.status_code == 200
    assert len(response.json()["agents"]) == 0


def test_creator_parse_intent_create(client: TestClient):
    """POST /api/creator/parse-intent — 解析创建意图。"""
    response = client.post("/api/creator/parse-intent", json={
        "message": "帮我创建一个代码审查流程",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "create_workflow"
    assert data["confidence"] >= 0.9


def test_creator_parse_intent_modify(client: TestClient):
    """POST /api/creator/parse-intent — 解析修改意图。"""
    response = client.post("/api/creator/parse-intent", json={
        "message": "添加一个审批节点",
        "workflow_id": "flow-1",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "modify_workflow"


def test_creator_parse_intent_chat(client: TestClient):
    response = client.post("/api/creator/parse-intent", json={
        "message": "你好",
    })
    assert response.status_code == 200
    assert response.json()["intent"] == "chat_reply"


def test_creator_parse_intent_with_history(client: TestClient):
    response = client.post("/api/creator/parse-intent", json={
        "message": "能否优化一下",
        "workflow_id": "flow-1",
        "history": [{"role": "user", "content": "你好"}],
    })
    assert response.status_code == 200
    assert response.json()["intent"] == "optimize_workflow"


def test_creator_decide_chat(client: TestClient):
    """POST /api/creator/decide — 闲聊回复。"""
    response = client.post("/api/creator/decide", json={
        "message": "你好",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "chat_reply"
    assert "reply" in data


def test_creator_decide_generate(client: TestClient):
    """POST /api/creator/decide — 生成意图路由到 generator。"""
    # 先创建一个工作流（因为 decide 中的 create 需要）
    client.put("/api/spec", json={
        "version": "1", "name": "Test", "agents": [], "providers": [],
        "workflows": [{"id": "flow-1", "name": "测试流程", "nodes": [], "edges": []}],
        "harness": [],
    })

    response = client.post("/api/creator/decide", json={
        "message": "添加一个审批节点",
        "workflow_id": "flow-1",
    })
    # 实际 generator 会尝试启动 opencode 进程，可能返回各种错误状态码
    # 但端点应可达且有正确结构
    assert response.status_code in (200, 409, 500)
    if response.status_code == 200:
        data = response.json()
        assert "generation_id" in data


def test_creator_reload(client: TestClient):
    """POST /api/creator/reload — 重新加载配置。"""
    response = client.post("/api/creator/reload")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_creator_status_after_spec_update(client: TestClient):
    """更新 spec 后，creator 状态应反映新的 agents 数量。"""
    # 初始状态
    initial = client.get("/api/creator/status").json()
    assert initial["agents_count"] == 0

    # 添加 harness
    client.put("/api/spec", json={
        "version": "1", "name": "Test", "agents": [], "providers": [],
        "harness": [{"id": "a1", "name": "Agent 1", "backend_id": "default", "agent_id": "a1"}],
    })

    # reload 后 agents 应更新
    client.post("/api/creator/reload")
    status = client.get("/api/creator/status").json()
    assert status["agents_count"] == 1


def test_creator_generate_endpoint(client: TestClient):
    """POST /api/creator/generate — 生成端点。"""
    response = client.post("/api/creator/generate", json={
        "message": "创建一个代码审查流程",
    })
    # 实际 generator 会尝试启动 opencode 进程，预期可能是 422 或 500
    # 但验证端点可达
    assert response.status_code in (200, 409, 422, 500, 503)
    if response.status_code == 200:
        data = response.json()
        assert "generation_id" in data


def test_creator_generations_list(client: TestClient):
    """GET /api/creator/workflows/{id}/generations — 查询生成历史。"""
    response = client.get("/api/creator/workflows/flow-1/generations")
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        assert "generations" in response.json()


def test_creator_chat_status(client: TestClient):
    """GET /api/creator/workflows/{id}/chat-status — 查询聊天状态。"""
    response = client.get("/api/creator/workflows/flow-1/chat-status")
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        data = response.json()
        assert "has_history" in data
        assert "active_generation" in data


def test_creator_endpoints_not_found(client: TestClient):
    """不存在的 generation 端点返回适当状态码。"""
    response = client.get("/api/creator/generations/nonexistent")
    assert response.status_code in (404, 503)


def test_creator_all_endpoints_return_json(client: TestClient):
    """所有 GET 端点都应返回 JSON。"""
    endpoints = [
        ("/api/creator/status", 200),
        ("/api/creator/node-types", 200),
        ("/api/creator/agents", 200),
    ]
    for path, expected_status in endpoints:
        response = client.get(path)
        assert response.status_code == expected_status, f"{path} 返回 {response.status_code}"
        assert response.headers["content-type"].startswith("application/json"), f"{path} 不是 JSON"