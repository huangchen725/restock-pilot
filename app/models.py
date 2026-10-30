# -*- coding: utf-8 -*-
"""领域模型：Policy / PlanItem / Plan / 任务状态。

金额一律整数分（int cents），禁止 float 参与计算。
"""

from enum import Enum
from typing import Dict, List, Optional, Literal

from pydantic import BaseModel, Field, ConfigDict


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
    model_config = ConfigDict(extra="forbid", strict=True)
    budget_cents: int = Field(..., gt=0, description="采购预算，整数分")
    cover_days: int = Field(30, ge=7, le=90, description="到货后期望覆盖天数 R")
    safety_days: int = Field(7, ge=0, le=30, description="安全库存天数 S")
    priority_categories: List[str] = Field(default_factory=list, description="优先补货类别")
    excluded_skus: List[str] = Field(default_factory=list, description="排除商品")


class DataContract(BaseModel):
    """数据提供方确认的口径；单位为件，ETA 为规划日起天数。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    quantity_unit: Optional[Literal["piece"]] = None
    available_is_net: bool = False
    cost_includes_tax_shipping: bool = False
    inbound_eta_is_days: bool = False
    missing_sales: Literal["reject", "zero"] = "reject"
    window_end: Optional[str] = None


class DatasetSnapshot(BaseModel):
    """规范排序后的完整输入；随方案保存，用于重算和追溯。"""
    model_config = ConfigDict(extra="forbid")
    contract: DataContract
    window_start: str
    window_end: str
    sales: List[Dict[str, str]]
    inventory: List[Dict[str, str]]
    suppliers: List[Dict[str, str]]


class RevisionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy: Optional[Policy]
    explanation: str
    unsupported_requests: List[str]


class PlanItem(BaseModel):
    sku: str
    name: str
    category: str
    avg_daily_sales: float          # d：近 N 天日均销量（含零销量日）
    lead_days: int                  # L：供应商交货天数
    available_qty: int              # 当前可用库存（已扣预占）
    inbound_qty: int                # 原始在途数量；实际抵扣见 eligible_inbound_qty
    inbound_eta_days: Optional[int] = None
    target_qty: int                 # 目标库存 ceil(d×(L+R+S))
    gap: int                        # 补货缺口
    order_qty: int                  # 最终采购量（预算分配后，仍满足起订量和整包）
    unit_cost_cents: int            # 含税含运到手单价（分）
    supplier_id: str
    pack_size: int
    min_order_qty: int
    line_total_cents: int           # order_qty × unit_cost_cents
    confidence: str = "high"        # high / medium / low（销量波动分层）
    reason: str = ""                # 跳过/降级等原因说明
    horizon_days: int = 0
    eligible_inbound_qty: int = 0
    original_order_qty: int = 0
    unmet_qty: int = 0
    stockout_before_arrival: bool = False
    missing_sales_days: int = 0
    warnings: List[str] = Field(default_factory=list)


class Plan(BaseModel):
    plan_id: str
    version: int
    dataset_hash: str
    policy: Policy
    items: List[PlanItem]
    total_cost_cents: int
    skipped: List[str] = Field(default_factory=list, description="预算不足等原因跳过的 SKU")
    schema_version: Literal[1, 2] = 1  # 缺省为旧方案，保护既有批准 hash。
    rules_version: str = "legacy"
    dataset_snapshot: Optional[DatasetSnapshot] = None
    dataset_id: Optional[str] = None
    budget_remaining_cents: Optional[int] = None
    partial_skus: List[str] = Field(default_factory=list)
    excluded_skus: List[str] = Field(default_factory=list)


def canonical_json(obj) -> str:
    """规范化 JSON 序列化：排序 key、紧凑分隔符——plan_hash 的唯一输入。"""
    import json
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plan_hash(plan: Plan) -> str:
    import hashlib
    if plan.schema_version == 1:
        data = plan.model_dump(include={"version", "dataset_hash", "policy", "items", "total_cost_cents"})
        added = {"horizon_days", "eligible_inbound_qty", "original_order_qty", "unmet_qty",
                 "stockout_before_arrival", "missing_sales_days", "warnings"}
        data["items"] = [{k: v for k, v in item.items() if k not in added} for item in data["items"]]
    else:
        data = plan.model_dump()
    payload = canonical_json(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
