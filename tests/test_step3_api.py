# -*- coding: utf-8 -*-
"""Step 3：FastAPI 与真实 runner 接线、修订和批准版本执行测试。"""

import json

import pytest
from fastapi.testclient import TestClient
from tests.support import SyncClient

from app import db, main
from app.execution import build_supplier_orders, summarize_execution
from app.models import Policy


class _FakeAdapter:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.executed_versions = []
        self.__class__.instances.append(self)

    def execute_plan(self, plan):
        self.executed_versions.append(plan.version)
        results = []
        for order in build_supplier_orders(plan):
            receipt = {
                "order_id": f"PO-{order['supplier_id']}",
                "external_ref": order["external_ref"],
                "supplier_id": order["supplier_id"],
                "status": "SUBMITTED",
                "lines": order["lines"],
                "total_cost_cents": order["total_cost_cents"],
            }
            results.append({
                "supplier_id": order["supplier_id"],
                "external_ref": order["external_ref"],
                "status": "created", "order_id": receipt["order_id"],
                "receipt": receipt, "problems": [], "error": None,
            })
        return {"results": results, **summarize_execution(results)}


@pytest.fixture
def client(tmp_path, monkeypatch):
    main._conn = db.init_db(tmp_path / "test.db")
    main._tasks = {}
    _FakeAdapter.instances = []
    monkeypatch.setattr(main, "PlaywrightProcurementAdapter", _FakeAdapter)
    from app.business_tools import active_session
    agent_calls = []

    def fake_agent(stage, context, user_input):
        agent_calls.append(stage)
        if stage == "planner":
            # 模拟 codex 通过 MCP build_plan 生成方案（与真实 MCP 同一入口与数据源）
            ctx = json.loads(context)
            active_session().call('inspect_dataset', {'task_id':ctx['task_id']})
            result = active_session().call('build_plan', {'task_id':ctx['task_id'], 'dataset_id':ctx['dataset_id'], 'policy':ctx['policy']})
            if "error" in result:
                raise RuntimeError(f"MCP build_plan failed: {result}")
            return {"status": "ok", "summary": "planner summary",
                    "tool_calls": ["mcp__restock__build_plan"], "duration_s": 0.1}
        if stage == "reviser":
            ctx = json.loads(context)
            policy = dict(ctx["current_policy"])
            policy["cover_days"] -= 1
            if "task_id" in ctx:
                # revise 场景：模拟 Reviser 通过 MCP revise_plan 生成 v+1
                result = active_session().call('revise_plan', {'task_id':ctx['task_id'], 'policy':policy, 'dataset_id':ctx['dataset_id']})
                if "error" in result:
                    raise RuntimeError(f"MCP revise_plan failed: {result}")
            # create 的理解要求（无 task_id）：只转 policy，不调 revise_plan
            return {"status": "ok", "tool_calls": ["mcp__restock__revise_plan"] if "task_id" in ctx else [], "duration_s": 0.1,
                    "summary": json.dumps({"policy": policy, "explanation": "覆盖天数减 1", "unsupported_requests": []})}
        return {"status": "ok", "summary": f"{stage} summary",
                "tool_calls": [], "duration_s": 0.1}

    monkeypatch.setattr(main, "_run_agent_stage", fake_agent)
    with SyncClient(main.app) as test_client:
        test_client.agent_calls = agent_calls
        yield test_client
    main._conn.close()
    main._conn = None
    main._tasks = {}


def _create(client):
    response = client.post("/tasks", json={
        "policy": {"budget_cents": 500000, "cover_days": 30, "safety_days": 7,
                   "priority_categories": [], "excluded_skus": []}
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_create_approve_execute_and_audit(client):
    created = _create(client)
    task_id = created["task_id"]
    assert client.agent_calls == ["planner"]
    approved = client.post(f"/tasks/{task_id}/approve").json()
    assert approved["approved_version"] == 1

    executed = client.post(f"/tasks/{task_id}/execute")
    assert executed.status_code == 200, executed.text
    payload = executed.json()
    assert payload["state"] == "COMPLETED"
    assert payload["executed_plan_hash"] == approved["approved_plan_hash"]
    assert payload["execution"]["succeeded"] == payload["execution"]["total"]
    assert client.agent_calls == ["planner", "executor"]

    audit = client.get(f"/tasks/{task_id}/executions").json()["records"]
    assert len(audit) == payload["execution"]["total"]
    assert all(record["receipt"]["problems"] == [] for record in audit)


def test_reviser_changes_only_policy_and_invalidates_old_approval(client):
    created = _create(client)
    task_id = created["task_id"]
    old_hash = created["plan_hash"]
    client.post(f"/tasks/{task_id}/approve")
    revised = client.post(f"/tasks/{task_id}/revise", json={"comment": "覆盖天数减少一天"})
    assert revised.status_code == 200, revised.text
    new_plan = revised.json()["plan"]
    assert new_plan["version"] == 2
    assert new_plan["policy"]["cover_days"] == 29
    assert revised.json()["plan_hash"] != old_hash
    assert client.post(f"/tasks/{task_id}/execute").status_code == 409

    client.post(f"/tasks/{task_id}/approve")
    executed = client.post(f"/tasks/{task_id}/execute")
    assert executed.status_code == 200
    assert _FakeAdapter.instances[-1].executed_versions == [2]


def test_execute_uses_approved_version_not_newer_database_plan(client):
    created = _create(client)
    task_id = created["task_id"]
    client.post(f"/tasks/{task_id}/approve")
    plan, _ = db.get_plan(main.get_conn(), task_id, 1)
    newer = plan.model_copy(update={"version": 99})
    db.save_plan(main.get_conn(), newer)  # 模拟未经审批出现的新版本

    response = client.post(f"/tasks/{task_id}/execute")
    assert response.status_code == 200, response.text
    assert _FakeAdapter.instances[-1].executed_versions == [1]


def test_execute_rejects_plan_content_modified_after_approval(client):
    created = _create(client)
    task_id = created["task_id"]
    client.post(f"/tasks/{task_id}/approve")
    plan, _ = db.get_plan(main.get_conn(), task_id, 1)
    tampered = plan.model_copy(deep=True)
    tampered.items[0].order_qty += 1
    with main.get_conn():
        main.get_conn().execute(
            "UPDATE plans SET plan_json = ? WHERE task_id = ? AND version = 1",
            (tampered.model_dump_json(), task_id),
        )
    response = client.post(f"/tasks/{task_id}/execute")
    assert response.status_code == 409
    assert "plan hash mismatch" in response.json()["detail"]
    assert _FakeAdapter.instances == []


def test_main_agent_stage_calls_real_runner(monkeypatch):
    monkeypatch.setenv("RESTOCK_AGENT_MODE", "required")
    captured = {}

    class Result:
        timed_out = False
        exit_code = 0
        final_text = "真实 Agent 摘要"
        tool_calls = ["tool-a"]
        duration_s = 1.2
        stderr = ""

    def fake_run(**kwargs):
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(main.runner, "run_codex_task", fake_run)
    import sys
    monkeypatch.setattr(main.runner, "resolve_codex_bin", lambda _: sys.executable)
    result = main._run_agent_stage("planner", "权威上下文", "用户输入")
    assert result["status"] == "ok"
    assert result["summary"] == "真实 Agent 摘要"
    assert "你是补货方案规划智能体" in captured["prompt"]
    # Runtime config is temporary and uses only scoped broker credentials.
    from pathlib import Path
    wd = Path(captured["workdir"])
    assert wd.name == 'work'
    assert not wd.exists()  # cleaned after the stage, not shared between tasks
    assert 'INTERN_API_KEY' not in captured['child_env']
    assert 'RESTOCK_APPROVAL_TOKEN' not in captured['child_env']


def test_reviser_rejects_non_json_or_extra_policy_fields():
    with pytest.raises(main.AgentStageError):
        main._policy_from_agent("把预算改为 100 元")
    with pytest.raises(main.AgentStageError):
        main._policy_from_agent(json.dumps({"policy": {
            "budget_cents": 100, "cover_days": 30, "safety_days": 7,
            "priority_categories": [], "excluded_skus": [], "order_qty": 999,
        }}))
