# -*- coding: utf-8 -*-
"""领域模型：Policy / PlanItem / Plan / 任务状态。

金额一律整数分（int cents），禁止 float 参与计算。
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class TaskState(str, Enum):
    NEW = "NEW"
    ANALYZING = "ANALYZING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    # 异常态
    NEEDS_INPUT = "NEEDS_INPUT"           # 数据缺失/口径不明，等用户补充
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"  # 执行结果与批准内容不一致
    PARTIAL = "PARTIAL"                    # 部分申请单创建成功
    FAILED = "FAILED"


class Policy(BaseModel):
    """用户可调整的策略参数（Policy DSL 的 schema 层）。"""
    budget_cents: int = Field(..., gt=0, description="采购预算，整数分")
    cover_days: int = Field(30, ge=7, le=90, description="到货后期望覆盖天数 R")
    safety_days: int = Field(7, ge=0, le=30, description="安全库存天数 S")
    priority_categories: List[str] = Field(default_factory=list, description="优先补货类别")
    excluded_skus: List[str] = Field(default_factory=list, description="排除商品")


class PlanItem(BaseModel):
    sku: str
    name: str
    category: str
    avg_daily_sales: float          # d：近 N 天日均销量（含零销量日）
    lead_days: int                  # L：供应商交货天数
    available_qty: int              # 当前可用库存（已扣预占）
    inbound_qty: int                # 在途数量（本规划窗口内确认到货）
    inbound_eta_days: Optional[int] = None
    target_qty: int                 # 目标库存 ceil(d×(L+R+S))
    gap: int                        # 补货缺口
    order_qty: int                  # 建议采购量（起订量+包装倍数取整后）
    unit_cost_cents: int            # 含税含运到手单价（分）
    supplier_id: str
    pack_size: int
    min_order_qty: int
    line_total_cents: int           # order_qty × unit_cost_cents
    confidence: str = "high"        # high / medium / low（销量波动分层）
    reason: str = ""                # 跳过/降级等原因说明


class Plan(BaseModel):
    plan_id: str
    version: int
    dataset_hash: str
    policy: Policy
    items: List[PlanItem]
    total_cost_cents: int
    skipped: List[str] = Field(default_factory=list, description="预算不足等原因跳过的 SKU")


def canonical_json(obj) -> str:
    """规范化 JSON 序列化：排序 key、紧凑分隔符——plan_hash 的唯一输入。"""
    import json
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plan_hash(plan: Plan) -> str:
    import hashlib
    payload = canonical_json(plan.model_dump(include={
        "version", "dataset_hash", "policy", "items", "total_cost_cents"
    }))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
