import pytest
from app.models import Policy
from app.rules import build_plan


def row(**changes):
    return dict(dict(sku="A", name="A", category="饮料", available_qty=0,
        inbound_qty=0, inbound_eta_days=None, supplier_id="S", unit_cost_cents=100,
        lead_days=3, pack_size=6, min_order_qty=12, daily_sales=["4"] * 28), **changes)


def plan(rows=None, budget=100000, **policy):
    return build_plan("p", 1, "ds", Policy(budget_cents=budget, cover_days=7, safety_days=2, **policy), rows or [row()])


def test_hand_calculated_partial_purchase_and_shortfall():
    p = plan(budget=1800); item = p.items[0]
    assert (item.target_qty, item.gap, item.original_order_qty) == (48, 48, 48)
    assert (item.order_qty, item.unmet_qty, item.line_total_cents) == (18, 30, 1800)
    assert p.total_cost_cents == 1800 and p.budget_remaining_cents == 0
    assert p.partial_skus == ["A"] and p.skipped == []


@pytest.mark.parametrize("budget,qty", [(1199, 0), (1200, 12), (1799, 12), (1800, 18), (4800, 48)])
def test_budget_moq_and_pack_boundaries(budget, qty):
    p = plan(budget=budget)
    assert p.items[0].order_qty == qty
    assert p.total_cost_cents == qty*100
    assert p.budget_remaining_cents == budget-qty*100


def test_late_inbound_is_not_deducted():
    p = plan([row(inbound_qty=100, inbound_eta_days=100)])
    it = p.items[0]
    assert (it.horizon_days, it.eligible_inbound_qty, it.gap, it.order_qty) == (12, 0, 48, 48)
    assert "inbound_not_deducted" in it.warnings


@pytest.mark.parametrize("eta,eligible", [(12, 20), (13, 0), (0, 20)])
def test_inbound_window_boundary(eta, eligible):
    it = plan([row(inbound_qty=20, inbound_eta_days=eta)]).items[0]
    assert it.eligible_inbound_qty == eligible
    assert it.gap == 48-eligible


def test_zero_gap_does_not_hide_prearrival_stockout():
    it = plan([row(inbound_qty=100, inbound_eta_days=10)]).items[0]
    assert it.gap == 0 and it.order_qty == 0
    assert it.stockout_before_arrival and "stockout_before_arrival" in it.warnings


@pytest.mark.parametrize("available,inbound,eta,risk", [(12,0,None,False), (11,0,None,True),
    (4,8,1,False), (4,7,1,True), (3,100,1,True), (0,12,0,False)])
def test_shortage_intervals_before_and_after_inbound(available,inbound,eta,risk):
    it = plan([row(available_qty=available,inbound_qty=inbound,inbound_eta_days=eta)]).items[0]
    assert it.stockout_before_arrival is risk


def test_low_confidence_and_budget_warning_do_not_hide_stockout():
    it = plan([row(daily_sales=[0]*21+[16]*7)], budget=100).items[0]
    assert {"low_confidence", "stockout_before_arrival", "budget_skipped"} <= set(it.warnings)


def test_priority_allocation_is_global_and_order_invariant():
    rows = [row(sku="B",category="日用"),row(sku="A", category="饮料")]
    a = plan(rows, budget=6000, priority_categories=["饮料"])
    assert [(x.sku,x.order_qty) for x in a.items] == [("A",48),("B",12)]
    assert a == plan(list(reversed(rows)), budget=6000, priority_categories=["饮料"])


def test_exact_fraction_prevents_extra_unit_at_integer_target():
    it = plan([row(daily_sales=["0.1"]*28,lead_days=1,pack_size=1,min_order_qty=0)]).items[0]
    assert it.target_qty == 1 and it.order_qty == 1
    it = plan([row(daily_sales=["2"]+["0"]*27,lead_days=19,pack_size=1,min_order_qty=0)]).items[0]
    assert it.target_qty == 2  # (2/28) * (19+7+2) is exactly 2.


def test_exclusions_and_zero_demand():
    p = plan([row(),row(sku="B",daily_sales=[0]*28)], excluded_skus=["A"])
    assert p.excluded_skus == ["A"] and [i.sku for i in p.items] == ["B"]
    assert p.total_cost_cents == 0 and p.items[0].order_qty == 0
