# -*- coding: utf-8 -*-
"""补货规则引擎——纯函数，零 IO，全部可单测。

规则（replenishment-v2）：
  d = 过去 28 天平均日销量（含零销量日）
  L = 交货天数；R = 到货后覆盖天数；S = 安全天数
  目标库存 = ceil(d × (L + R + S))
  缺口 = max(0, 目标库存 − 可用库存 − 窗口内确认到货的在途)
  采购量 = max(缺口, 起订量) 向上取整到整包装倍数（pack_size=1 不取整）
预算分配：优先类别 → 预计更早缺货 → SKU；可部分采购，仍需满足整包与起订量。
置信分层：CV < 0.3 高 / 0.3–0.7 中 / >0.7 低；多数零销量或确认补零也为低。
"""

import math
from fractions import Fraction
from typing import Dict, List, Tuple

from app.models import Plan, PlanItem, Policy, DatasetSnapshot

SALES_WINDOW_DAYS = 28


def avg_daily_sales(daily_sales: List[float]) -> float:
    """d：已确认的窗口日均；缺失日期不能未经确认补零。"""
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


def days_to_stockout(avg_daily: float, available: int) -> float:
    """预计多少天后断货（用于优先级排序与到货前断货检查）。"""
    if avg_daily <= 0:
        return math.inf  # 不缺货，不应排在任何有需求的 SKU 之前。
    return max(0, int(available // avg_daily))


def target_qty(avg_daily: float, lead_days: int, cover_days: int, safety_days: int) -> int:
    return math.ceil(Fraction(str(avg_daily)) * (lead_days + cover_days + safety_days))


def reorder_gap(target: int, available: int, inbound_confirmed: int) -> int:
    return max(0, target - available - inbound_confirmed)


def purchase_qty(gap: int, min_order_qty: int, pack_size: int) -> int:
    if gap <= 0:
        return 0
    base = max(gap, min_order_qty)
    if pack_size > 1:
        base = ((base + pack_size - 1) // pack_size) * pack_size
    return base


def stockout_before_arrival(
    avg_daily: float, available: int, lead_days: int, inbound_eta_days: int | None,
    inbound_qty: int = 0,
) -> bool:
    """检查在途到达前后两个区间，是否能撑到本次采购的供应商交货日。"""
    early = inbound_qty > 0 and inbound_eta_days is not None and 0 <= inbound_eta_days < lead_days
    return bool(available < avg_daily * (inbound_eta_days if early else lead_days) or
                (early and available + inbound_qty < avg_daily * lead_days))


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
    dataset_snapshot: DatasetSnapshot | None = None,
) -> Plan:
    """从校验后的合并数据行生成方案，daily_sales 为已确认的 28 天序列。
    """
    items: List[PlanItem] = []
    daily_by_sku = {}
    for row in sorted(sku_rows, key=lambda r: r["sku"]):
        if row["sku"] in policy.excluded_skus:
            continue
        d = sum(Fraction(str(v)) for v in row["daily_sales"]) / len(row["daily_sales"])
        daily_by_sku[row["sku"]] = d
        tier = confidence_tier([float(v) for v in row["daily_sales"]])
        if row.get("missing_sales_days", 0):
            tier = "low"
        horizon = row["lead_days"] + policy.cover_days + policy.safety_days
        eta = row.get("inbound_eta_days")
        eligible = row["inbound_qty"] if eta is not None and 0 <= eta <= horizon else 0
        target = target_qty(d, row["lead_days"], policy.cover_days, policy.safety_days)
        gap = reorder_gap(target, row["available_qty"], eligible)
        qty = purchase_qty(gap, row["min_order_qty"], row["pack_size"])
        # Check both intervals around an inbound arrival, independently of gap/confidence.
        risk = stockout_before_arrival(d, row["available_qty"], row["lead_days"], eta, row["inbound_qty"])
        warnings, reasons = [], []
        if gap == 0:
            reasons.append("缺口为 0，无需采购")
        if tier == "low":
            warnings.append("low_confidence")
            reasons.append("销量依据不足，建议人工复核")
        if row.get("missing_sales_days", 0):
            warnings.append("missing_sales_filled")
            reasons.append(f"按已确认口径补零 {row['missing_sales_days']} 天")
        if row["inbound_qty"] > eligible:
            warnings.append("inbound_not_deducted")
            reasons.append("在途未确认在规划窗口内到货，本次不抵扣")
        if risk:
            warnings.append("stockout_before_arrival")
            reasons.append("预计到货前缺货，建议加快补货或催单")

        items.append(PlanItem(
            sku=row["sku"], name=row["name"], category=row["category"],
            avg_daily_sales=round(float(d), 4), lead_days=row["lead_days"],
            available_qty=row["available_qty"], inbound_qty=row["inbound_qty"],
            inbound_eta_days=row.get("inbound_eta_days"),
            target_qty=target, gap=gap, order_qty=qty,
            unit_cost_cents=row["unit_cost_cents"], supplier_id=row["supplier_id"],
            pack_size=row["pack_size"], min_order_qty=row["min_order_qty"],
            line_total_cents=qty * row["unit_cost_cents"],
            confidence=tier, reason="；".join(reasons), warnings=warnings,
            horizon_days=horizon, eligible_inbound_qty=eligible,
            original_order_qty=qty, stockout_before_arrival=bool(risk),
            missing_sales_days=row.get("missing_sales_days", 0),
        ))

    # 预算分配：优先级排序后顺序分配，不足起订量的跳过
    need = [it for it in items if it.order_qty > 0]
    need.sort(key=lambda it: (0 if it.category in policy.priority_categories else 1,
                             days_to_stockout(daily_by_sku[it.sku], it.available_qty), it.sku))
    budget_left = policy.budget_cents
    skipped: List[str] = []
    partial: List[str] = []
    for it in need:
        affordable = (budget_left // it.unit_cost_cents // it.pack_size) * it.pack_size
        qty = min(it.original_order_qty, affordable)
        if qty < it.min_order_qty:
            qty = 0
        it.order_qty = qty
        it.line_total_cents = qty * it.unit_cost_cents
        it.unmet_qty = max(0, it.gap - qty)
        if qty < it.original_order_qty:
            code = "budget_partial" if qty else "budget_skipped"
            it.warnings.append(code)
            (partial if qty else skipped).append(it.sku)
            detail = (f"预算内部分采购 {qty} 件，未满足缺口 {it.unmet_qty} 件" if qty else
                      f"剩余预算 {budget_left} 分不足起订量或一整包，本轮不采购")
            it.reason = "；".join(filter(None, [it.reason, detail]))
        budget_left -= it.line_total_cents

    total = sum(it.line_total_cents for it in items)
    return Plan(
        plan_id=plan_id, version=version, dataset_hash=dataset_hash,
        policy=policy, items=items, total_cost_cents=total, skipped=skipped,
        schema_version=2, rules_version="replenishment-v2",
        dataset_id="ds-" + dataset_hash,
        dataset_snapshot=dataset_snapshot, budget_remaining_cents=budget_left,
        partial_skus=partial, excluded_skus=sorted(set(policy.excluded_skus)),
    )
