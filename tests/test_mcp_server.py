# -*- coding: utf-8 -*-
"""M2 业务 MCP 工具层测试：权限边界、幂等版本控制、规则引擎唯一计算源。"""

import pytest

from app import db, mcp_server
from app.models import TaskState


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "DB_PATH", tmp_path / "mcp-test.db")
    monkeypatch.setattr(mcp_server, "DATA_DIR", mcp_server.ROOT / "data")
    mcp_server._conn = None
    conn = mcp_server.get_conn()
    db.upsert_task(conn, "task-mcp", TaskState.ANALYZING)
    yield conn
    conn.close()
    mcp_server._conn = None


def test_inspect_dataset_returns_contract_and_hash(tool_env):
    result = mcp_server.inspect_dataset("task-mcp")
    assert result["dataset_hash"]
    assert result["sku_count"] == 30
    assert result["problems"] == []


def test_build_plan_generates_amounts_only_from_rules(tool_env):
    result = mcp_server.build_plan("task-mcp", policy={"budget_cents": 500000})
    assert result["reused"] is False
    plan = result["plan"]
    assert plan["total_cost_cents"] > 0
    assert result["plan_hash"]
    # 金额字段是 int 分，不是模型编造的字符串
    assert isinstance(plan["total_cost_cents"], int)
    for item in plan["items"]:
        if item["order_qty"] > 0:
            assert isinstance(item["line_total_cents"], int)


def test_repeated_build_plan_reuses_same_version(tool_env):
    first = mcp_server.build_plan("task-mcp", policy={"budget_cents": 500000})
    second = mcp_server.build_plan("task-mcp", policy={"budget_cents": 500000})
    assert second["reused"] is True
    assert second["plan"]["version"] == first["plan"]["version"]
    assert second["plan_hash"] == first["plan_hash"]


def test_policy_change_creates_new_version_and_invalidates_approval(tool_env):
    mcp_server.build_plan("task-mcp", policy={"budget_cents": 500000})
    db.upsert_task(tool_env, "task-mcp", TaskState.APPROVED,
                   approved_plan_hash="x" * 64, approved_version=1)
    revised = mcp_server.revise_plan("task-mcp", policy={"budget_cents": 300000})
    assert revised["reused"] is False
    assert revised["plan"]["version"] == 2
    assert revised["plan"]["policy"]["budget_cents"] == 300000
    # 旧批准被撤销
    row = db.get_task(tool_env, "task-mcp")
    assert row["state"] == TaskState.NEEDS_REVIEW.value
    assert row["approved_plan_hash"] is None


def test_executing_task_cannot_be_rebuilt(tool_env):
    mcp_server.build_plan("task-mcp", policy={"budget_cents": 500000})
    db.upsert_task(tool_env, "task-mcp", TaskState.EXECUTING,
                   approved_plan_hash="x" * 64, approved_version=1)
    with pytest.raises(ValueError, match="不允许"):
        mcp_server.build_plan("task-mcp", policy={"budget_cents": 400000})


def test_unknown_task_rejected(tool_env):
    with pytest.raises(ValueError, match="not found"):
        mcp_server.inspect_dataset("nonexistent")


def test_get_execution_result_returns_records_and_events(tool_env):
    result = mcp_server.get_execution_result("task-mcp")
    assert result["records"] == []
    assert result["events"] == []
