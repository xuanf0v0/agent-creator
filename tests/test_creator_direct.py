"""_build_direct 单元测试 — Creator Harness 一次性直出生成路径"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from openagent_studio.generator import Generation, GeneratorManager
from openagent_studio.models import ProjectSpec, WorkflowEvaluation, WorkflowSpec
from openagent_studio.store import SpecStore


def _valid_workflow_dict() -> dict:
    return {
        "id": "flow-1",
        "name": "代码审查",
        "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}, "position": {"x": 0, "y": 0}},
            {"id": "out", "type": "output", "data": {"template": "{{latest}}"}, "position": {"x": 200, "y": 0}},
        ],
        "edges": [{"source": "start", "target": "out"}],
    }


def _invalid_workflow_dict() -> dict:
    return {"id": "flow-1", "name": "空", "nodes": [], "edges": []}


def _make_generation(store: SpecStore) -> Generation:
    return Generation(
        id="g1",
        workflow_id="flow-1",
        base_etag=store.etag(),
        draft={"id": "flow-1", "name": "t", "nodes": [], "edges": []},
        prompt="创建一个代码审查流程",
        harness_agent_ids=set(),
        model="test-model",
        mode="create",
        direct=True,
    )


def _make_spec() -> ProjectSpec:
    return ProjectSpec.model_validate({"version": "1", "name": "t", "workflows": [], "harness": []})


def _build_direct_fixture(tmp_path: Path):
    store = SpecStore(tmp_path / "project.yaml")
    manager = GeneratorManager(store)
    gen = _make_generation(store)
    spec = _make_spec()
    original = WorkflowSpec.model_validate({"id": "flow-1", "name": "t", "nodes": [], "edges": []})
    evaluator = MagicMock()
    return manager, gen, spec, original, evaluator


def test_build_direct_success(tmp_path: Path):
    manager, gen, spec, original, evaluator = _build_direct_fixture(tmp_path)
    manager._invoke_result = MagicMock(return_value=_valid_workflow_dict())
    manager._generate_evaluation = MagicMock(return_value=WorkflowEvaluation(cases=[]))
    manager._evaluate_failed_cases_first = MagicMock(return_value=(SimpleNamespace(passed=True), set()))

    result = manager._build_direct(gen, spec, ["opencode"], tmp_path, original, evaluator, "[]")

    assert result.id == "flow-1"
    assert len(result.nodes) == 2
    assert manager._invoke_result.call_count == 1


def test_build_direct_retries_on_static_failure(tmp_path: Path):
    manager, gen, spec, original, evaluator = _build_direct_fixture(tmp_path)
    # 第一次返回空图（静态校验失败），第二次返回合法图
    manager._invoke_result = MagicMock(side_effect=[_invalid_workflow_dict(), _valid_workflow_dict()])
    manager._generate_evaluation = MagicMock(return_value=WorkflowEvaluation(cases=[]))
    manager._evaluate_failed_cases_first = MagicMock(return_value=(SimpleNamespace(passed=True), set()))

    result = manager._build_direct(gen, spec, ["opencode"], tmp_path, original, evaluator, "[]")

    assert result.id == "flow-1"
    assert len(result.nodes) == 2
    assert manager._invoke_result.call_count == 2


def test_build_direct_retries_on_evaluation_failure(tmp_path: Path):
    manager, gen, spec, original, evaluator = _build_direct_fixture(tmp_path)
    manager._invoke_result = MagicMock(return_value=_valid_workflow_dict())
    manager._generate_evaluation = MagicMock(return_value=WorkflowEvaluation(cases=[]))
    # 第一次验收失败，第二次通过
    failed = SimpleNamespace(passed=False, errors=["验收失败"], cases=[])
    passed = SimpleNamespace(passed=True)
    manager._evaluate_failed_cases_first = MagicMock(
        side_effect=[(failed, set()), (passed, set())]
    )

    result = manager._build_direct(gen, spec, ["opencode"], tmp_path, original, evaluator, "[]")

    assert result.id == "flow-1"
    assert len(result.nodes) == 2
    assert manager._invoke_result.call_count == 2
    assert manager._evaluate_failed_cases_first.call_count == 2
