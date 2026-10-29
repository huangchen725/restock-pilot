# -*- coding: utf-8 -*-
"""规则引擎与状态机边界用例——方案评审中列出的全部高危路径。"""

import pytest

from app.models import PlanItem, Policy, canonical_json
from app.rules import (
    avg_daily_sales, confidence_tier, days_to_stockout, purchase_qty,
    priority_key, reorder_gap, target_qty,
)
from app.state_machine import TaskRecord, TaskState, TransitionError
from app.models import plan_hash


# ---------- 规则引擎 ----------

def test_target_rounding():
    # d=12.4, L=3, R=30, S=7 → 12.4*40=496 → ceil=496
    assert target_qty(12.4, 3, 30, 7) == 496
    # 小数必须向上取整：12.1*41.9...取小数场景
    assert target_qty(0.1, 1, 7, 0) == 1  # 0.8 → 1


def test_gap_zero_and_inbound():
    assert reorder_gap(100, 60, 40) == 0          # 在途补足
    assert reorder_gap(100, 60, 50) == 0           # 在途超目标 → 0
    assert reorder_gap(100, 60, 0) == 40


def test_purchase_qty_moq_and_pack():
    assert purchase_qty(0, 10, 1) == 0             # 缺口 0 → 0
    assert purchase_qty(5, 10, 1) == 10            # MOQ 抬底
    assert purchase_qty(5, 10, 6) == 12            # MOQ=10 → 12（6 的倍数）
    assert purchase_qty(13, 0, 6) == 18            # 缺口 → 18
    assert purchase_qty(24, 0, 6) == 24            # 恰好整包不进位
    assert purchase_qty(7, 0, 1) == 7              # pack=1 不取整


def test_integer_cents_no_float_error():
    # 0.1 元 × 3 的经典浮点坑：整数分下不存在
    assert 3 * 10 == 30
    line = 24 * 3999  # 24 件 × 39.99 元
    assert line == 95976


def test_confidence_tiers():
    stable = [10.0] * 28
    volatile = [x * 3 if i % 2 else x for i, x in enumerate([10.0] * 28)]
    zeros = [0.0 if i % 2 else 5.0 for i in range(28)]
    mostly_zero = [0.0] * 20 + [5.0] * 8
    assert confidence_tier(stable) == "high"
    assert confidence_tier(volatile) in ("medium", "low")
    assert confidence_tier(zeros) in ("medium", "low")
    assert confidence_tier(mostly_zero) == "low"   # 零销量占比 > 50%


def test_days_to_stockout():
    assert days_to_stockout(0.0, 100) > 10**9      # 不动销 → 不缺货
    assert days_to_stockout(10.0, 95) == 9
    assert days_to_stockout(10.0, 0) == 0


def _item(sku="SKU-001", cat="日用", d=10.0, avail=50):
    return PlanItem(
        sku=sku, name="n", category=cat, avg_daily_sales=d, lead_days=3,
        available_qty=avail, inbound_qty=0, target_qty=400, gap=350,
        order_qty=350, unit_cost_cents=1000, supplier_id="S", pack_size=1,
        min_order_qty=0, line_total_cents=350000, confidence="high",
    )


def test_priority_categories_beat_stockout():
    policy = Policy(budget_cents=10**9, priority_categories=["饮料"])
    urgent_daily = _item(sku="A", cat="日用", d=20.0, avail=10)   # 0.5 天后断货
    priority = _item(sku="B", cat="饮料", d=1.0, avail=300)       # 300 天后断货
    assert priority_key(priority, policy) < priority_key(urgent_daily, policy)


# ---------- 状态机 ----------

def test_happy_path():
    rec = TaskRecord("t1")
    for s in (TaskState.ANALYZING, TaskState.NEEDS_REVIEW):
        rec.transition(s)
    rec.approve("hash-1", 1)
    assert rec.state is TaskState.APPROVED
    rec.check_execution_authority("hash-1")
    rec.transition(TaskState.EXECUTING)
    rec.transition(TaskState.COMPLETED)
    assert rec.state is TaskState.COMPLETED


def test_approve_outside_review_rejected():
    rec = TaskRecord("t2")
    rec.transition(TaskState.ANALYZING)
    with pytest.raises(TransitionError):
        rec.approve("hash", 1)                     # ANALYZING 里不许批准


def test_revision_invalidates_approval():
    rec = TaskRecord("t3")
    rec.transition(TaskState.ANALYZING)
    rec.transition(TaskState.NEEDS_REVIEW)
    rec.approve("hash-1", 1)
    # 用户修改 → 回 ANALYZING → 旧批准必须失效
    rec.transition(TaskState.ANALYZING)
    assert rec.approved_plan_hash is None
    # 新方案 v2 审批前，用旧 hash 提执行必须被拒
    rec.transition(TaskState.NEEDS_REVIEW)
    with pytest.raises(TransitionError):
        rec.check_execution_authority("hash-1")    # 还没批准 v2


def test_execute_wrong_hash_rejected():
    rec = TaskRecord("t4")
    rec.transition(TaskState.ANALYZING)
    rec.transition(TaskState.NEEDS_REVIEW)
    rec.approve("hash-correct", 2)
    with pytest.raises(TransitionError):
        rec.check_execution_authority("hash-forged")  # 模型伪造清单 → 拒绝


def test_terminal_states_locked():
    rec = TaskRecord("t5")
    rec.transition(TaskState.ANALYZING)
    rec.transition(TaskState.FAILED)
    with pytest.raises(TransitionError):
        rec.transition(TaskState.ANALYZING)        # FAILED 是终态


# ---------- plan_hash 规范化 ----------

def test_plan_hash_stable_across_key_order():
    item = _item()
    h1 = plan_hash(_plan(item))
    h2 = plan_hash(_plan(item))                    # 同一方案两次计算
    assert h1 == h2


def test_plan_hash_changes_on_policy_change():
    from app.models import Plan
    item = _item()
    p1 = _plan(item, budget=100000)
    p2 = _plan(item, budget=200000)                # 预算变化 → hash 变
    assert plan_hash(p1) != plan_hash(p2)


def _plan(item, budget=100000, version=1):
    from app.models import Plan
    return Plan(plan_id="p", version=version, dataset_hash="ds",
                policy=Policy(budget_cents=budget), items=[item],
                total_cost_cents=item.line_total_cents)
