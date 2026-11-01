"""Backend-owned, operation-scoped calculations. Tools never commit a plan.

The CLI bridge only forwards authorized calls; it imports no business modules
and opens no database. Only the API creates sessions, chooses their immutable
inputs, and commits accepted results. OS account isolation is out of scope.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import threading
import uuid

from app import db
from app.models import Policy, TaskState, canonical_json, plan_hash
from app.rules import build_plan
from app.validation import merged_rows, snapshot_hash, validate_policy

_current = ContextVar('restock_tool_session', default=None)


def active_session():
    return _current.get()


def compact_plan(plan):
    return {
        'currency': 'CNY', 'amount_unit': '人民币分；100 分 = 1 元，非美元或美分',
        'plan_id': plan.plan_id, 'version': plan.version, 'dataset_id': plan.dataset_id,
        'policy': plan.policy.model_dump(), 'total_cost_cents': plan.total_cost_cents,
        'budget_remaining_cents': plan.budget_remaining_cents, 'sku_count': len(plan.items),
        'ordered': [{'sku': i.sku, 'order_qty': i.order_qty, 'unit_cost_cents': i.unit_cost_cents,
                     'line_total_cents': i.line_total_cents, 'supplier_id': i.supplier_id,
                     'unmet_qty': i.unmet_qty} for i in plan.items if i.order_qty],
        'skipped': plan.skipped, 'excluded_skus': plan.excluded_skus,
        'low_confidence_skus': [i.sku for i in plan.items if i.confidence == 'low'],
        'stockout_skus': [i.sku for i in plan.items if i.stockout_before_arrival],
        'unmet_total_qty': sum(i.unmet_qty for i in plan.items),
    }


def planner_review(plan, advice):
    """Authoritative quantities are rendered by code; model supplies advice only."""
    import re
    advice = advice.strip()
    if not advice or len(advice) > 240 or re.search(r'[0-9]|[零一二三四五六七八九十百千万]+\s*(?:件|种|个|分|元|成|%)', advice):
        raise ValueError('Planner 只能提供简短非数值建议；金额、数量和商品编号由程序展示')
    def money(cents):
        whole, fraction = divmod(cents, 100)
        return f'{whole}.{fraction:02d} 元'
    risks = {i.sku for i in plan.items if i.stockout_before_arrival}
    skipped_risks = risks & set(plan.skipped)
    low = sum(i.confidence == 'low' for i in plan.items)
    ordered = sum(i.order_qty > 0 for i in plan.items)
    return (f'规则核对（程序生成）：全局预算 {money(plan.policy.budget_cents)}，'
        f'方案金额 {money(plan.total_cost_cents)}，余额 {money(plan.budget_remaining_cents or 0)}。'
        f'采购 {ordered} 种商品，预算跳过 {len(plan.skipped)} 种；低置信商品 {low} 种。'
        f'预计到货前缺货风险 {len(risks)} 种，其中被预算跳过 {len(skipped_risks)} 种。'
        '金额为人民币，申请尚需人工批准。\n模型建议：' + advice)


class ToolSession:
    def __init__(self, conn, task_id, stage, snapshot=None, current=None, fixed_policy=None):
        self.conn, self.task_id, self.stage = conn, task_id, stage
        self.snapshot = snapshot.model_copy(deep=True) if snapshot else None
        self.current = current.model_copy(deep=True) if current else None
        self.dataset_id = 'ds-' + snapshot_hash(snapshot) if snapshot else None
        self.fixed_policy = fixed_policy.model_copy(deep=True) if fixed_policy else None
        self.base_version = current.version if current else 0
        self.base_hash = plan_hash(current) if current else None
        self.operation_id = uuid.uuid4().hex
        self.draft = None
        self.closed = False
        self.inspected = False
        self._committed = False
        self._lock = threading.RLock()
        self.allowed = {
            'planner': {'inspect_dataset', 'build_plan'},
            'reviser': {'inspect_dataset', 'revise_plan'},
            'executor': {'get_execution_result'},
        }[stage]

    @contextmanager
    def activate(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            self.closed = True
            _current.reset(token)

    def _check(self):
        if self.closed:
            raise ValueError('工具授权已失效')
        row = db.get_task(self.conn, self.task_id)
        if not row:
            raise ValueError('找不到授权任务')
        if self.stage == 'executor':
            if (not self.base_version or row['approved_version'] != self.base_version
                    or row['state'] not in {'COMPLETED','PARTIAL','FAILED','NEEDS_RECONCILIATION'}):
                raise ValueError('执行版本已改变')
            return
        if row['state'] != TaskState.ANALYZING.value or not db.get_context(self.conn, self.task_id)['busy']:
            raise ValueError('当前任务不允许生成或修订方案')
        versions = db.list_plan_versions(self.conn, self.task_id)
        if (versions[-1] if versions else 0) != self.base_version:
            raise ValueError('方案版本已改变，请重新复核')
        if self.current:
            _, actual = db.get_plan(self.conn, self.task_id, self.base_version)
            if actual != self.base_hash:
                raise ValueError('方案内容已改变，请重新复核')

    def call(self, tool, arguments):
        with self._lock, db._LOCK:
            try:
                self._check()
                if tool not in self.allowed:
                    raise ValueError('此阶段不允许调用该工具')
                fields = {'task_id'} if tool in {'inspect_dataset', 'get_execution_result'} else {'task_id', 'dataset_id', 'policy'}
                if not isinstance(arguments, dict) or set(arguments) - fields:
                    raise ValueError('工具参数包含未支持字段')
                if arguments.get('task_id') != self.task_id:
                    raise ValueError('禁止跨任务调用')
                if arguments.get('dataset_id') not in (None, self.dataset_id):
                    raise ValueError('禁止替换授权数据版本')
                if tool == 'inspect_dataset':
                    self.inspected = True
                    result = {'dataset_id': self.dataset_id, 'dataset_hash': snapshot_hash(self.snapshot),
                              'currency': 'CNY', 'amount_unit': '人民币分；100 分 = 1 元',
                              'sku_count': len(self.snapshot.inventory), 'problems': [],
                              'window_start': self.snapshot.window_start, 'window_end': self.snapshot.window_end,
                              'contract': self.snapshot.contract.model_dump()}
                elif tool == 'get_execution_result':
                    result = {'records': [r for r in db.list_execution_records(self.conn, self.task_id)
                                          if r['version'] == self.base_version],
                              'events': [e for e in db.list_execution_events(self.conn, self.task_id)
                                         if e['payload'].get('version') == self.base_version
                                         or e['payload'].get('plan_hash') == self.base_hash]}
                else:
                    policy = Policy.model_validate(arguments.get('policy'))
                    if policy.model_fields_set != set(Policy.model_fields):
                        raise ValueError('必须完整提供五个 Policy 字段')
                    if self.fixed_policy is not None and policy != self.fixed_policy:
                        raise ValueError('禁止改变已确认的结构化要求')
                    problems = validate_policy(policy, self.snapshot)
                    if problems:
                        raise ValueError('; '.join(problems))
                    reuse = self.current is not None and self.current.policy == policy and self.current.dataset_hash == snapshot_hash(self.snapshot)
                    # M3：查询当前任务已创建申请覆盖的 SKU 数量，修订时扣除
                    # （恢复或修订不能再次采购已有申请覆盖的数量）。
                    covered: dict = {}
                    for record in db.list_execution_records(self.conn, self.task_id):
                        if record.get('status') == 'created':
                            receipt = (record.get('receipt') or {}).get('receipt')
                            if receipt:
                                for line in receipt.get('lines', []):
                                    covered[line['sku']] = covered.get(line['sku'], 0) + line['order_qty']
                    self.draft = (self.current.model_copy(deep=True) if reuse else
                        build_plan(self.task_id, self.base_version + 1, snapshot_hash(self.snapshot), policy,
                                   merged_rows(self.snapshot), self.snapshot, covered=covered))
                    result = {'plan': compact_plan(self.draft), 'plan_hash': plan_hash(self.draft),
                              'reused': reuse, 'candidate_only': True}
                self._audit(tool, arguments, result=result)
                return deepcopy(result)
            except Exception as exc:
                self._audit(tool, arguments, error=str(exc))
                raise

    def _audit(self, tool, arguments, **outcome):
        db.activity(self.conn, self.task_id, 'business_tool', {
            'operation_id': self.operation_id, 'stage': self.stage, 'tool': tool,
            'version': self.base_version, 'dataset_id': self.dataset_id,
            'arguments': arguments, **outcome})

    def commit(self, accepted_policy):
        """Called by the backend after model output validation, never exposed as a tool."""
        with self._lock, db._LOCK:
            if self.closed:
                raise ValueError('工具授权已失效')
            if self._committed:
                return self.draft, plan_hash(self.draft)
            self._check()
            if self.stage == 'planner' and not self.inspected:
                raise ValueError('Planner 未通过业务工具检查数据')
            if self.draft is None:
                raise ValueError('Agent 未通过业务工具生成候选方案')
            if self.draft.policy != accepted_policy:
                raise ValueError('最终决策与工具候选方案不一致')
            # One API process owns all commits. Keep the compare and insert atomic
            # across tool sessions; the CLI bridge has no SQLite access.
            h = db.commit_candidate(self.conn, self.draft, self.base_version, self.base_hash)
            self._committed = True
            return self.draft, h
