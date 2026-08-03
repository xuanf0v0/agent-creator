from pathlib import Path
from fastapi.testclient import TestClient
from openagent_studio.app import create_app
from openagent_studio.generator import Generation, GeneratorManager
from openagent_studio.store import SpecStore


def test_spec_round_trip_and_conflict(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    response = client.get("/api/spec")
    assert response.status_code == 200
    etag = response.json()["etag"]
    spec = response.json()["spec"]
    spec["name"] = "Demo"
    saved = client.put("/api/spec", headers={"if-match": etag}, json=spec)
    assert saved.status_code == 200
    assert client.put("/api/spec", headers={"if-match": etag}, json=spec).status_code == 409


def test_compile_artifacts(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    payload = {"version":"1", "name":"Demo", "agents":[{"id":"builder","name":"Builder","model":"kax/grok","max_steps":12}], "providers":[], "harness":[{"id":"builder","name":"Builder","cwd":"."}]}
    assert client.put("/api/spec", json=payload).status_code == 200
    output = client.get("/api/compile/opencode").json()
    assert output["agent"]["builder"]["maxSteps"] == 12
    assert client.get("/api/compile/harness/builder").status_code == 200


def test_ui_is_chinese(tmp_path: Path):
    body = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False)).get("/").text
    assert "智能体工作流画布" in body
    assert 'lang="zh-CN"' in body
    assert 'id="root"' in body
    assert "/assets/" in body


def test_form_options_and_validation(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    options = client.get("/api/form-options").json()
    assert any(item["label"] == "主智能体" for item in options["agent_modes"])
    assert client.post("/api/spec/validate", json={"version": "1", "name": "示例"}).json()["valid"] is True


def test_workflow_canvas_api(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "project.yaml", auto_start_harness=False))
    payload = {"version": "1", "name": "示例", "workflows": [{"id": "flow", "name": "测试流程", "nodes": [{"id": "a", "type": "agent", "data": {}, "position": {"x": 1, "y": 2}}], "edges": []}]}
    assert client.put("/api/spec", json=payload).status_code == 200
    assert client.get("/api/workflows").json()[0]["id"] == "flow"
    assert client.post("/api/workflows/flow/validate").json()["valid"] is True


def test_generator_applies_incremental_operations(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    manager = GeneratorManager(store)
    generation = Generation(id="g", workflow_id="flow", base_etag=store.etag(), draft={"id": "flow", "name": "流程", "nodes": [], "edges": []}, prompt="创建流程")
    manager._apply(generation, {"action": "add_node", "id": "review", "type": "agent", "description": "代码审查"})
    manager._apply(generation, {"action": "add_node", "id": "done", "type": "output", "description": "输出结果"})
    manager._apply(generation, {"action": "connect_nodes", "source": "review", "target": "done"})
    assert [node["id"] for node in generation.draft["nodes"]] == ["review", "done"]
    assert generation.draft["edges"] == [{"source": "review", "target": "done"}]
    assert [event["event"] for event in generation.events] == ["workflow.node.added", "workflow.node.added", "workflow.edge.added"]
