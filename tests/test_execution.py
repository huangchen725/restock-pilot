# -*- coding: utf-8 -*-
"""Step 3：采购后台幂等、固定适配器与回读一致性测试。"""

import copy

from fastapi.testclient import TestClient

from app.execution import (
    PlaywrightProcurementAdapter, build_supplier_orders,
    external_ref_for, reconcile_receipt, summarize_execution,
)
from app.models import Plan, PlanItem, Policy
from app.procurement_portal import create_portal_app


def _item(sku="SKU-1", supplier="SUP-A", qty=12, cost=250):
    return PlanItem(
        sku=sku, name=f"商品{sku}", category="日用", avg_daily_sales=2.0,
        lead_days=3, available_qty=0, inbound_qty=0, target_qty=qty,
        gap=qty, order_qty=qty, unit_cost_cents=cost, supplier_id=supplier,
        pack_size=1, min_order_qty=1, line_total_cents=qty * cost,
    )


def _plan():
    return Plan(
        plan_id="task123", version=2, dataset_hash="ds",
        policy=Policy(budget_cents=100000),
        items=[_item("SKU-2", "SUP-B", 4, 100),
               _item("SKU-1", "SUP-A", 12, 250),
               _item("SKU-0", "SUP-A", 0, 250)],
        total_cost_cents=3400,
    )


def _receipt(order):
    return {
        "order_id": "PO-TEST", "external_ref": order["external_ref"],
        "supplier_id": order["supplier_id"], "status": "SUBMITTED",
        "lines": copy.deepcopy(order["lines"]),
        "total_cost_cents": order["total_cost_cents"],
    }


def test_supplier_grouping_and_deterministic_ref():
    plan = _plan()
    orders = build_supplier_orders(plan)
    assert [order["supplier_id"] for order in orders] == ["SUP-A", "SUP-B"]
    assert external_ref_for(plan, "SUP-A") == "task123-v2-SUP-A"
    assert orders[0]["total_cost_cents"] == 3000
    assert len(orders[0]["lines"]) == 1  # order_qty=0 不录单


def test_reconciliation_detects_quantity_and_total_changes():
    order = build_supplier_orders(_plan())[0]
    receipt = _receipt(order)
    receipt["lines"][0]["order_qty"] += 1
    problems = reconcile_receipt(order, receipt)
    assert any("order_qty mismatch" in problem for problem in problems)
    receipt["total_cost_cents"] += 1
    assert any("total mismatch" in problem for problem in reconcile_receipt(order, receipt))


def test_mismatch_has_priority_over_partial_success():
    order = build_supplier_orders(_plan())[0]
    good = {"receipt": _receipt(order), "problems": []}
    bad = {"receipt": None, "problems": ["quantity mismatch"]}
    assert summarize_execution([good, bad])["state"] == "NEEDS_RECONCILIATION"
    failed = {"receipt": None, "problems": []}
    assert summarize_execution([good, failed])["state"] == "PARTIAL"


def test_portal_idempotency_and_three_pages(tmp_path):
    app = create_portal_app(tmp_path / "portal.db")
    client = TestClient(app)
    order = build_supplier_orders(_plan())[0]
    payload = {key: order[key] for key in ("external_ref", "supplier_id", "lines")}

    first = client.post("/api/purchase-orders", json=payload)
    assert first.status_code == 201 and first.json()["created"] is True
    order_id = first.json()["order"]["order_id"]
    second = client.post("/api/purchase-orders", json=payload)
    assert second.json()["created"] is False
    assert second.json()["order"]["order_id"] == order_id

    changed = copy.deepcopy(payload)
    changed["lines"][0]["order_qty"] += 1
    assert client.post("/api/purchase-orders", json=changed).status_code == 409
    assert client.get("/purchase-orders").status_code == 200
    assert client.get("/purchase-orders/new").status_code == 200
    detail = client.get(f"/purchase-orders/{order_id}")
    assert detail.status_code == 200
    assert "id='order-total-cents'" in detail.text
    assert len(app.state.store.list()) == 1


class _FakeLocator:
    def count(self):
        return 1

    def nth(self, index):
        return self

    def locator(self, selector):
        return self

    def fill(self, value):
        return None


class _TimeoutPage:
    def __init__(self):
        self.submit_calls = 0

    def goto(self, url):
        return None

    def fill(self, selector, value):
        return None

    def locator(self, selector):
        return _FakeLocator()

    def click(self, selector):
        if selector == "#submit-order":
            self.submit_calls += 1
            raise TimeoutError("simulated response timeout")

    def wait_for_url(self, pattern, timeout):
        return None


class _RecoveringAdapter(PlaywrightProcurementAdapter):
    def __init__(self, receipt):
        super().__init__()
        self.receipt = receipt
        self.lookup_calls = 0

    def _find_by_ref(self, page, external_ref, attempt_log=None):
        self.lookup_calls += 1
        if attempt_log is not None:
            attempt_log.append({"step": "duplicate_check", "external_ref": external_ref})
        return None if self.lookup_calls == 1 else self.receipt


def test_timeout_queries_before_any_retry():
    order = build_supplier_orders(_plan())[0]
    adapter = _RecoveringAdapter(_receipt(order))
    page = _TimeoutPage()
    result = adapter._execute_order(page, order)
    assert result["status"] == "recovered_after_error"
    assert result["problems"] == []
    assert adapter.lookup_calls == 2  # 提交前一次 + 异常后一次
    assert page.submit_calls == 1     # 绝不盲目重试
