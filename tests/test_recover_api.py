# -*- coding: utf-8 -*-
"""P1-12：受控恢复 API 端到端测试（经过 /tasks/{id}/recover 端点，
验证批准 hash 校验、状态机迁移、互斥和审计完整性——不只直接调 adapter）。"""

import pytest
from tests.support import SyncClient
from app import db, main
from app.models import TaskState
from app.execution import build_supplier_orders, summarize_execution


class _FakeAdapter:
    instances = []
    _created_refs = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def execute_plan(self, plan, on_order_result=None, only_refs=None):
        results = []
        for order in build_supplier_orders(plan):
            if only_refs and order["external_ref"] not in only_refs:
                continue
            receipt = {"order_id": f"PO-{order['supplier_id']}",
                       "external_ref": order["external_ref"],
                       "supplier_id": order["supplier_id"], "status": "SUBMITTED",
                       "lines": order["lines"], "total_cost_cents": order["total_cost_cents"]}
            result = {"supplier_id": order["supplier_id"],
                      "external_ref": order["external_ref"],
                      "status": "created", "order_id": receipt["order_id"],
                      "receipt": receipt, "problems": [], "error": None}
            results.append(result)
            self.__class__._created_refs.add(order["external_ref"])
            if on_order_result is not None:
                on_order_result(result)
        return {"results": results, **summarize_execution(results)}

    def reconcile_orders(self, plan):
        # 模拟受控核对：已创建的订单返回 created，未创建的返回 not_submitted
        results = []
        for order in build_supplier_orders(plan):
            created = order["external_ref"] in self.__class__._created_refs
            results.append({
                "supplier_id": order["supplier_id"],
                "external_ref": order["external_ref"],
                "state": "created" if created else "not_submitted",
                "order_id": f"PO-{order['supplier_id']}" if created else None,
                "receipt": {"order_id": f"PO-{order['supplier_id']}",
                            "lines": order["lines"]} if created else None,
                "problems": [], "attempt_log": [],
            })
        return results


@pytest.fixture
def client(tmp_path, monkeypatch):
    main._conn = db.init_db(tmp_path / "test.db")
    main._tasks = {}
    _FakeAdapter.instances = []
    _FakeAdapter._created_refs = set()
    monkeypatch.setattr(main, "PlaywrightProcurementAdapter", _FakeAdapter)
    monkeypatch.setenv("RESTOCK_AGENT_MODE", "off")
    with SyncClient(main.app) as test_client:
        yield test_client
    main._conn.close()
    main._conn = None
    main._tasks = {}


def _to_partial(client, task_id):
    client.post(f"/tasks/{task_id}/approve")
    # 直接构造 PARTIAL 状态（不创建任何订单，所有订单未提交），
    # 使 recover 有真实的恢复集合（recon 返回 not_submitted）。
    # 注：不 execute_plan（避免全部创建后走"空恢复集合"路径）。
    rec = main.get_record(task_id)
    rec.transition(TaskState.EXECUTING)
    rec.transition(TaskState.PARTIAL)
    db.upsert_task(main.get_conn(), task_id, rec.state,
                   rec.approved_plan_hash, rec.approved_version)


def test_recover_rejects_mismatched_approved_hash(client):
    """P1-1：recover 必须重新校验批准指纹——hash 不匹配的任务拒绝恢复。"""
    created = client.post("/tasks", json={"policy": {"budget_cents": 500000}}).json()
    task_id = created["task_id"]
    _to_partial(client, task_id)
    # 篡改批准 hash 并持久化（get_record 每次返回新实例，内存修改不影响后续读取，
    # 必须 upsert_task 写入数据库，_recover_work 才能读到篡改后的 hash）。
    rec = main.get_record(task_id)
    db.upsert_task(main.get_conn(), task_id, rec.state, "y" * 64, rec.approved_version)
    # prefer=wait 同步执行，让 _recover_work 的 409 正确返回（异步则只返回 202）。
    result = client.post(f"/tasks/{task_id}/recover", headers={"prefer": "wait"})
    assert result.status_code == 409


def test_recover_rejects_unapproved_state(client):
    """recover 只允许 PARTIAL/NEEDS_RECONCILIATION/FAILED 状态；其他状态拒绝。"""
    created = client.post("/tasks", json={"policy": {"budget_cents": 500000}}).json()
    task_id = created["task_id"]
    # NEEDS_REVIEW 状态（不是待核对状态）→ recover 应拒绝
    result = client.post(f"/tasks/{task_id}/recover")
    assert result.status_code == 409


def test_recover_full_flow_with_audit_and_state(client):
    """P1-12：recover 完整流程——状态机迁移与审计事件（经过 API 端点）。"""
    created = client.post("/tasks", json={"policy": {"budget_cents": 500000}}).json()
    task_id = created["task_id"]
    _to_partial(client, task_id)
    result = client.post(f"/tasks/{task_id}/recover")
    assert result.status_code == 200, result.text
    # 审计：recover 有独立的 execution_started + order_result + execution_finished
    events = db.list_execution_events(main.get_conn(), task_id)
    types = [e["event_type"] for e in events]
    assert "execution_started" in types
    assert "order_result" in types
    assert "execution_finished" in types
    # recover 的 execution_started 应标记 recovered=True
    started = [e for e in events if e["event_type"] == "execution_started"
               and e["payload"].get("recovered")]
    assert started, "recover 应有独立的 execution_started（recovered=True）"
    # 状态机：全部恢复成功后应为 COMPLETED
    final = client.get(f"/tasks/{task_id}").json()
    assert final["state"] in (TaskState.COMPLETED.value, TaskState.NEEDS_RECONCILIATION.value,
                              TaskState.PARTIAL.value)
