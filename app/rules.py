# -*- coding: utf-8 -*-
"""补货规则引擎——纯函数，零 IO，全部可单测。

规则（与方案 v1 一致）：
  d = 过去 28 天平均日销量（含零销量日）
  L = 交货天数；R = 到货后覆盖天数；S = 安全天数
  目标库存 = ceil(d × (L + R + S))
  缺口 = max(0, 目标库存 − 可用库存 − 在途)
  采购量 = max(缺口, 起订量) 向上取整到整包装倍数（pack_size=1 不取整）
预算分配：优先类别 → 预计更早缺货 → 其他；剩余预算不足起订量的跳过并说明。
置信分层：日销量变异系数 CV < 0.3 高 / < 0.7 中 / 其余低（低置信走保守说明）。
"""

import math
from typing import Dict, List, Tuple

from app.models import Plan, PlanItem, Policy

SALES_WINDOW_DAYS = 28


def avg_daily_sales(daily_sales: List[float]) -> float:
    """d：窗口内日均，零销量日必须计入（缺失日由数据层补零）。"""
    if not daily_sales:
        raise ValueError("sales window is empty")
    return sum(daily_sales) / len(daily_sales)


def sales_cv(daily_sales: List[float]) -> float:
    """变异系数：销量波动分层的依据。"""
    n = len(daily_sales)
    if n < 2:
        return 0.0
    mean = sum(daily_sales) / n
    if mean == 0:
        return float("inf")  # 全零销量 → 无规则依据
    var = sum((x - mean) ** 2 for x in daily_sales) / n
    return math.sqrt(var) / mean


def confidence_tier(daily_sales: List[float]) -> str:
    cv = sales_cv(daily_sales)
    zero_ratio = sum(1 for x in daily_sales if x == 0) / len(daily_sales)
    if zero_ratio > 0.5 or cv > 0.7:
        return "low"
    if cv >= 0.3:
        return "medium"
    return "high"


def days_to_stockout(avg_daily: float, available: int) -> int:
    """预计多少天后断货（用于优先级排序与到货前断货检查）。"""
    if avg_daily <= 0:
        return 1 << 30  # 不缺货
    return max(0, int(available // avg_daily))


def target_qty(avg_daily: float, lead_days: int, cover_days: int, safety_days: int) -> int:
    return math.ceil(avg_daily * (lead_days + cover_days + safety_days))


def reorder_gap(target: int, available: int, inbound_confirmed: int) -> int:
    return max(0, target - available - inbound_confirmed)


def purchase_qty(gap: int, min_order_qty: int, pack_size: int) -> int:
    if gap <= 0:
        return 0
    base = max(gap, min_order_qty)
    if pack_size > 1:
        base = math.ceil(base / pack_size) * pack_size
    return base


def stockout_before_arrival(
    avg_daily: float, available: int, lead_days: int, inbound_eta_days: int
) -> bool:
    """到货前是否可能断货：可用撑不到 eta。"""
    days = days_to_stockout(avg_daily, available)
    return days < min(lead_days, inbound_eta_days)


def priority_key(item: PlanItem, policy: Policy) -> Tuple:
    """排序键：用户指定优先类别 → 更早缺货 → 其他（稳定排序）。"""
    in_priority = 0 if item.category in policy.priority_categories else 1
    return (in_priority, days_to_stockout(item.avg_daily_sales, item.available_qty), item.sku)


def build_plan(
    plan_id: str,
    version: int,
    dataset_hash: str,
    policy: Policy,
    sku_rows: List[Dict],
) -> Plan:
    """从合并数据行生成方案。sku_rows 字段与 PlanItem 输入一致，
    且必须已含 daily_sales: List[float]（28 天序列）。
    """
    items: List[PlanItem] = []
    for row in sku_rows:
        if row["sku"] in policy.excluded_skus:
            continue
        d = avg_daily_sales(row["daily_sales"])
        tier = confidence_tier(row["daily_sales"])
        target = target_qty(d, row["lead_days"], policy.cover_days, policy.safety_days)
        gap = reorder_gap(target, row["available_qty"], row["inbound_qty"])
        qty = purchase_qty(gap, row["min_order_qty"], row["pack_size"])
        reason = ""
        if gap == 0:
            reason = "缺口为 0，无需采购"
        elif tier == "low":
            reason = "销量依据不足（高波动或长期零销量），建议人工复核"
        elif row["inbound_eta_days"] is not None and stockout_before_arrival(
            d, row["available_qty"], row["lead_days"], row["inbound_eta_days"]
        ):
            reason = "预计到货前缺货，建议加快补货或催单"

        items.append(PlanItem(
            sku=row["sku"], name=row["name"], category=row["category"],
            avg_daily_sales=round(d, 4), lead_days=row["lead_days"],
            available_qty=row["available_qty"], inbound_qty=row["inbound_qty"],
            inbound_eta_days=row.get("inbound_eta_days"),
            target_qty=target, gap=gap, order_qty=qty,
            unit_cost_cents=row["unit_cost_cents"], supplier_id=row["supplier_id"],
            pack_size=row["pack_size"], min_order_qty=row["min_order_qty"],
            line_total_cents=qty * row["unit_cost_cents"],
            confidence=tier, reason=reason,
        ))

    # 预算分配：优先级排序后顺序分配，不足起订量的跳过
    need = [it for it in items if it.order_qty > 0]
    need.sort(key=lambda it: priority_key(it, policy))
    budget_left = policy.budget_cents
    skipped: List[str] = []
    for it in need:
        cost = it.line_total_cents
        if cost <= budget_left:
            budget_left -= cost
        else:
            skipped.append(it.sku)
            it.order_qty = 0
            it.line_total_cents = 0
            it.reason = (
                f"剩余预算 {budget_left} 分不足起订采购量 "
                f"（需 {cost} 分），本轮暂不采购"
            )

    total = sum(it.line_total_cents for it in items)
    return Plan(
        plan_id=plan_id, version=version, dataset_hash=dataset_hash,
        policy=policy, items=items, total_cost_cents=total, skipped=skipped,
    )
