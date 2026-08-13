"""命令链目录 + 批次 add_node 测试 — Creator Harness Phase 1"""

from __future__ import annotations

from openagent_studio.creator.chains import (
    COMMAND_CHAINS,
    MAX_CHAIN_NODES,
    chain_example,
    command_chain_catalog_text,
)
from openagent_studio.generator import _apply_incremental_step
from openagent_studio.models import WorkflowSpec


def _workflow() -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "flow", "name": "f", "nodes": [
            {"id": "start", "type": "manual_trigger", "data": {}},
            {"id": "done", "type": "output", "data": {}},
        ], "edges": [{"source": "start", "target": "done"}],
    })


def test_command_chain_catalog_is_nonempty_and_renderable():
    assert len(COMMAND_CHAINS) >= 4
    text = command_chain_catalog_text()
    assert "add_approval_gate" in text
    assert "一次可产出多个关联节点" in text
    for chain in COMMAND_CHAINS:
        assert chain["name"] and chain["label"] and chain["description"]
        assert isinstance(chain["example"]["nodes"], list) and chain["example"]["nodes"]


def test_chain_example_roundtrips():
    example = chain_example("add_approval_gate")
    assert example is not None
    assert example["action"] == "add_node"
    assert len(example["nodes"]) == 2
    assert any(n["type"] == "approval" for n in example["nodes"])


def test_batch_add_node_appends_all_and_touches_all():
    wf = _workflow()
    candidate, action, touched, *_ = _apply_incremental_step(
        wf, {
            "action": "add_node",
            "nodes": [
                {"id": "approve", "type": "approval", "data": {"description": "审批"}},
                {"id": "gate", "type": "condition", "data": {"description": "分支", "expression": "latest.approved == true"}},
            ],
            "edges": [
                {"source": "start", "target": "approve"},
                {"source": "approve", "target": "gate"},
                {"source": "gate", "target": "done", "condition": "true"},
            ],
        }, set(),
    )
    assert action == "add_node"
    assert touched == "approve,gate"
    assert [n.id for n in candidate.nodes] == ["start", "done", "approve", "gate"]
    # 新链完整接入
    edges = {(e.source, e.target, e.condition) for e in candidate.edges}
    assert ("start", "approve", None) in edges
    assert ("approve", "gate", None) in edges
    assert ("gate", "done", "true") in edges


def test_single_node_add_still_supported():
    wf = _workflow()
    candidate, action, touched, *_ = _apply_incremental_step(
        wf, {"action": "add_node", "node": {"id": "mid", "type": "prompt", "data": {"template": "x"}},
             "edges": [{"source": "start", "target": "mid"}]}, set(),
    )
    assert action == "add_node"
    assert touched == "mid"
    assert [n.id for n in candidate.nodes] == ["start", "done", "mid"]


def test_batch_must_connect_to_existing_graph():
    import pytest
    wf = _workflow()
    # 两个新节点互相连接但不接入既有图 → 拒绝
    with pytest.raises(RuntimeError, match="接入既有"):
        _apply_incremental_step(
            wf, {
                "action": "add_node",
                "nodes": [
                    {"id": "a", "type": "prompt", "data": {"template": "a"}},
                    {"id": "b", "type": "prompt", "data": {"template": "b"}},
                ],
                "edges": [{"source": "a", "target": "b"}],
            }, set(),
        )


def test_batch_rejects_duplicate_ids_in_batch():
    import pytest
    wf = _workflow()
    with pytest.raises(RuntimeError, match="重复"):
        _apply_incremental_step(
            wf, {
                "action": "add_node",
                "nodes": [
                    {"id": "a", "type": "prompt", "data": {"template": "a"}},
                    {"id": "a", "type": "prompt", "data": {"template": "b"}},
                ],
                "edges": [{"source": "start", "target": "a"}],
            }, set(),
        )


def test_update_node_still_single_only():
    import pytest
    wf = _workflow()
    with pytest.raises(RuntimeError, match="一次只能更新一个节点"):
        _apply_incremental_step(
            wf, {"action": "update_node", "nodes": [
                {"id": "start", "type": "manual_trigger", "data": {}},
                {"id": "done", "type": "output", "data": {}},
            ], "edges": []}, set(),
        )
