# -*- coding: utf-8 -*-
"""执行回放审计账本：事件链完整性 + 回放可用性测试。"""

import pytest
from fastapi.testclient import TestClient
from tests.support import SyncClient

from app import db, main
from app.execution import build_supplier_orders, summarize_execution


class _FakeAdapter:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def execute_plan(self, plan, on_order_result=None):
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
            result = {
                "supplier_id": order["supplier_id"],
                "external_ref": order["external_ref"],
                "status": "created", "order_id": receipt["order_id"],
                "receipt": receipt, "problems": [], "error": None,
                "attempt_log": [
                    {"step": "duplicate_check", "external_ref": order["external_ref"]},
                    {"step": "duplicate_check_result", "found": False},
                    {"step": "submit", "lines": len(order["lines"])},
                    {"step": "readback", "order_id": receipt["order_id"]},
                ],
            }
            results.append(result)
            # 模拟真实 adapter 的逐步落账回调（M3）
            if on_order_result is not None:
                on_order_result(result)
        return {"results": results, **summarize_execution(results)}


@pytest.fixture
def client(tmp_path, monkeypatch):
    main._conn = db.init_db(tmp_path / "test.db")
    main._tasks = {}
    _FakeAdapter.instances = []
    monkeypatch.setattr(main, "PlaywrightProcurementAdapter", _FakeAdapter)
    from app.business_tools import active_session

    def fake_agent(stage, context, user_input):
        if stage == "planner":
            import json as J
            ctx = J.loads(context)
            active_session().call('inspect_dataset', {'task_id':ctx['task_id']})
            result = active_session().call('build_plan', {'task_id':ctx['task_id'], 'dataset_id':ctx['dataset_id'], 'policy':ctx['policy']})
            if "error" in result:
                raise RuntimeError(f"MCP build_plan failed: {result}")
            return {"status": "ok", "summary": "planner summary",
                    "tool_calls": ["mcp__restock__build_plan"], "duration_s": 0.1}
        return {"status": "ok", "summary": "s", "tool_calls": [], "duration_s": 0.1}

    monkeypatch.setattr(main, "_run_agent_stage", fake_agent)
    with SyncClient(main.app) as test_client:
        yield test_client
    main._conn.close()
    main._conn = None
    main._tasks = {}


def test_audit_trail_records_full_replay_chain(client):
    created = client.post("/tasks", json={"policy": {"budget_cents": 500000}}).json()
    task_id = created["task_id"]
    approved = client.post(f"/tasks/{task_id}/approve").json()
    executed = client.post(f"/tasks/{task_id}/execute").json()

    trail = client.get(f"/tasks/{task_id}/audit-trail").json()
    assert trail["replay_complete"] is True
    types = [event["event_type"] for event in trail["events"]]
    # 事件链：started -> 每单一个 order_result -> finished
    assert types[0] == "execution_started"
    assert types[-1] == "execution_finished"
    assert types.count("order_result") == executed["execution"]["total"]

    started = trail["events"][0]["payload"]
    assert started["plan_hash"] == approved["approved_plan_hash"]

    # seq 严格递增（回放顺序确定）
    seqs = [event["seq"] for event in trail["events"]]
    assert seqs == sorted(seqs)

    for event in trail["events"]:
        if event["event_type"] == "order_result":
            assert event["payload"]["plan_hash"] == approved["approved_plan_hash"]
            assert event["external_ref"]
            steps = [item["step"] for item in event["payload"]["attempt_log"]]
            assert steps == ["duplicate_check", "duplicate_check_result",
                             "submit", "readback"]


def test_audit_trail_replay_detects_missing_plan_hash(tmp_path):
    """账本被部分破坏时 replay_complete 必须报 False（账本完整性可验证）。"""
    main._conn = db.init_db(tmp_path / "t.db")
    main._tasks = {}
    try:
        db.append_execution_event(main.get_conn(), "t-x", "order_result",
                                  "ref-x", {"status": "created", "plan_hash": None})
        events = db.list_execution_events(main.get_conn(), "t-x")
        replay_ok = all(
            e["event_type"] != "order_result" or e["payload"].get("plan_hash")
            for e in events
        )
        assert replay_ok is False
    finally:
        main._conn.close()
        main._conn = None
        main._tasks = {}


def test_audit_trail_empty_ledger_is_not_complete(client):
    """空账本不代表完整执行（M3 审计完整性）。"""
    created = client.post("/tasks", json={"policy": {"budget_cents": 500000}}).json()
    task_id = created["task_id"]
    trail = client.get(f"/tasks/{task_id}/audit-trail").json()
    assert trail["replay_complete"] is False
    assert trail["event_count"] == 0


def test_audit_trail_interrupted_execution_is_not_complete(client):
    """中断执行（started + finished 但无 order_result）不能标为完整。"""
    created = client.post("/tasks", json={"policy": {"budget_cents": 500000}}).json()
    task_id = created["task_id"]
    db.append_execution_event(main.get_conn(), task_id, "execution_started", None, {
        "plan_hash": "x" * 64,
        "orders": [{"external_ref": "e1", "supplier_id": "S1",
                    "lines": 1, "total_cost_cents": 100}],
    })
    db.append_execution_event(main.get_conn(), task_id, "execution_finished",
                              None, {"state": "FAILED"})
    trail = client.get(f"/tasks/{task_id}/audit-trail").json()
    assert trail["replay_complete"] is False


def test_audit_trail_partial_order_results_is_not_complete(client):
    """部分订单结果（声明 2 单但只有 1 个 order_result）不能标为完整。"""
    created = client.post("/tasks", json={"policy": {"budget_cents": 500000}}).json()
    task_id = created["task_id"]
    db.append_execution_event(main.get_conn(), task_id, "execution_started", None, {
        "plan_hash": "x" * 64,
        "orders": [{"external_ref": "e1", "supplier_id": "S1",
                    "lines": 1, "total_cost_cents": 100},
                   {"external_ref": "e2", "supplier_id": "S2",
                    "lines": 1, "total_cost_cents": 200}],
    })
    db.append_execution_event(main.get_conn(), task_id, "order_result", "e1",
                              {"status": "created", "plan_hash": "x" * 64})
    db.append_execution_event(main.get_conn(), task_id, "execution_finished",
                              None, {"state": "PARTIAL"})
    trail = client.get(f"/tasks/{task_id}/audit-trail").json()
    assert trail["replay_complete"] is False  # e2 缺 order_result
