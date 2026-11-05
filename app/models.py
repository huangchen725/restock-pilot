# -*- coding: utf-8 -*-
"""领域模型：Policy / PlanItem / Plan / 任务状态。

金额一律整数分（int cents），禁止 float 参与计算。
"""

from enum import Enum
from typing import Dict, List, Optional, Literal

from pydantic import BaseModel, Field, ConfigDict, model_serializer, model_validator


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


class PurchaseLine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: str = Field(min_length=1, max_length=160)
    sku: str = Field(min_length=1, max_length=100)
    supplier_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0)
    status: Literal['requested', 'ordered', 'in_transit', 'received']
    created_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    expected_date: str | None = None
    amount_cents: int | None = Field(default=None, gt=0)
    origin_system_id: str | None = Field(default=None,min_length=1,max_length=160)
    origin_order_id: str | None = Field(default=None,min_length=1,max_length=160)

    @model_serializer(mode='wrap')
    def serialize_link(self,handler):
        data=handler(self)
        for key in ('origin_system_id','origin_order_id'):
            if data[key] is None:data.pop(key)
        return data


    @model_validator(mode='after')
    def valid_identity_dates(self):
        from datetime import date
        if any(not getattr(self,k).strip() or getattr(self,k)!=getattr(self,k).strip()
               for k in ('order_id','sku','supplier_id')):
            raise ValueError('采购单据身份不能空白或带首尾空格')
        for name in ('origin_system_id','origin_order_id'):
            value=getattr(self,name)
            if value is not None and (not value.strip() or value!=value.strip()):raise ValueError('采购对应标识不能空白或带首尾空格')
        if bool(self.origin_system_id)!=bool(self.origin_order_id):raise ValueError('采购对应关系须同时提供系统和原单号')
        date.fromisoformat(self.created_date)
        if self.expected_date is not None:
            value=date.fromisoformat(self.expected_date)
            if value.isoformat()!=self.expected_date:raise ValueError('预计到货日期格式无效')
        if self.status in ('ordered','in_transit') and self.expected_date is None:
            raise ValueError('已订货/在途必须提供可核验的预计到货日期')
        return self


class PurchaseContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    system_id: str = Field(min_length=1,max_length=160)
    timezone_offset: str = Field(pattern=r'^[+-](?:0\d|1[0-4]):[0-5]\d$')
    complete: bool
    row_count: int = Field(ge=0)
    orders: list[PurchaseLine] = Field(max_length=10000)

    @model_validator(mode='after')
    def complete_identity(self):
        if not self.complete or self.row_count!=len(self.orders):
            raise ValueError('采购上下文尚未完整或行数声明不符')
        if self.system_id!=self.system_id.strip() or not self.system_id:
            raise ValueError('采购系统身份无效')
        if self.timezone_offset[1:3]=='14' and self.timezone_offset[4:]!='00':
            raise ValueError('时区偏移不能超过14小时')
        keys=[(x.order_id,x.sku) for x in self.orders]
        if len(set(keys))!=len(keys):raise ValueError('同一采购单据/SKU 重复出现')
        self.orders.sort(key=lambda x:(x.order_id,x.sku))
        return self


class DatasetSnapshot(BaseModel):
    """规范排序后的完整输入；随方案保存，用于重算和追溯。"""
    model_config = ConfigDict(extra="forbid")
    contract: DataContract
    window_start: str
    window_end: str
    sales: List[Dict[str, str]]
    inventory: List[Dict[str, str]]
    suppliers: List[Dict[str, str]]
    purchase_context: PurchaseContext | None = None

    @model_serializer(mode='wrap')
    def serialize_legacy_compatible(self, handler):
        data=handler(self)
        # Existing snapshot/plan identities must remain byte-for-byte compatible.
        if self.purchase_context is None:data.pop('purchase_context',None)
        return data


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
    schema_version: Literal[1, 2, 3] = 1  # 缺省为旧方案，保护既有批准 hash。
    rules_version: str = "legacy"
    dataset_snapshot: Optional[DatasetSnapshot] = None
    dataset_id: Optional[str] = None
    budget_remaining_cents: Optional[int] = None
    partial_skus: List[str] = Field(default_factory=list)
    excluded_skus: List[str] = Field(default_factory=list)
    automation_context: dict | None = None

    @model_serializer(mode='wrap')
    def compatible_context(self,handler):
        value=handler(self)
        if self.automation_context is None:value.pop('automation_context',None)
        return value

    @model_validator(mode='after')
    def versioned_automation(self):
        if (self.schema_version==3)!=(self.automation_context is not None):
            raise ValueError('自动补货计算上下文必须使用 schema 3')
        return self


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
