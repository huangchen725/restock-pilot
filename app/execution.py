# -*- coding: utf-8 -*-
"""固定 Playwright 采购录单适配器。

安全边界：只接收后端已批准的 Plan；按 supplier 分组生成 deterministic
external_ref。任何提交异常都先按 external_ref 回查，再决定结果，绝不盲目重试。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional
from urllib.parse import quote

from app.models import Plan, TaskState


def external_ref_for(plan: Plan, supplier_id: str) -> str:
    return f"{plan.plan_id}-v{plan.version}-{supplier_id}"


def build_supplier_orders(plan: Plan) -> List[dict]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for item in plan.items:
        if item.order_qty <= 0:
            continue
        grouped[item.supplier_id].append({
            "sku": item.sku,
            "name": item.name,
            "order_qty": item.order_qty,
            "unit_cost_cents": item.unit_cost_cents,
        })
    orders = []
    for supplier_id in sorted(grouped):
        lines = sorted(grouped[supplier_id], key=lambda line: line["sku"])
        orders.append({
            "external_ref": external_ref_for(plan, supplier_id),
            "supplier_id": supplier_id,
            "lines": lines,
            "total_cost_cents": sum(
                line["order_qty"] * line["unit_cost_cents"] for line in lines
            ),
        })
    return orders


def reconcile_receipt(order: dict, receipt: dict) -> List[str]:
    """双通道一致性校验：批准方案 vs 采购后台回读详情。"""
    problems = []
    for field in ("external_ref", "supplier_id"):
        if receipt.get(field) != order[field]:
            problems.append(
                f"{field} mismatch: expected {order[field]!r}, got {receipt.get(field)!r}"
            )
    expected_lines = sorted(order["lines"], key=lambda line: line["sku"])
    actual_lines = sorted(receipt.get("lines") or [], key=lambda line: line.get("sku", ""))
    if len(actual_lines) != len(expected_lines):
        problems.append(f"line count mismatch: expected {len(expected_lines)}, got {len(actual_lines)}")
    for index, expected in enumerate(expected_lines):
        if index >= len(actual_lines):
            break
        actual = actual_lines[index]
        for field in ("sku", "order_qty", "unit_cost_cents"):
            if actual.get(field) != expected[field]:
                problems.append(
                    f"line {index + 1} {field} mismatch: expected {expected[field]!r}, "
                    f"got {actual.get(field)!r}"
                )
    expected_total = order["total_cost_cents"]
    if receipt.get("total_cost_cents") != expected_total:
        problems.append(
            f"total mismatch: expected {expected_total}, got {receipt.get('total_cost_cents')}"
        )
    return problems


def summarize_execution(results: List[dict]) -> dict:
    if not results:
        return {"state": TaskState.COMPLETED.value, "succeeded": 0, "total": 0}
    succeeded = sum(
        1 for result in results
        if result.get("receipt") is not None and not result.get("problems")
    )
    has_mismatch = any(result.get("problems") for result in results)
    if has_mismatch:
        state = TaskState.NEEDS_RECONCILIATION
    elif succeeded == len(results):
        state = TaskState.COMPLETED
    elif succeeded:
        state = TaskState.PARTIAL
    else:
        state = TaskState.FAILED
    return {"state": state.value, "succeeded": succeeded, "total": len(results)}


class PlaywrightProcurementAdapter:
    """通过采购后台真实 UI 创建申请单，并从详情页回读核对。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8200",
                 headless: bool = True, timeout_ms: int = 15000):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.timeout_ms = timeout_ms

    def execute_plan(self, plan: Plan, on_order_result=None, only_refs=None) -> dict:
        orders = build_supplier_orders(plan)
        # M3 受控恢复：only_refs 限定只恢复指定订单（其余不触碰，不生成新版本）。
        if only_refs is not None:
            orders = [o for o in orders if o["external_ref"] in only_refs]
        if not orders:
            return {"results": [], **summarize_execution([])}
        results = []
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    # 采购后台在本地回环；显式禁用代理，避免系统 HTTP_PROXY 劫持录单请求。
                    args=["--no-proxy-server"],
                )
                try:
                    page = browser.new_page()
                    page.set_default_timeout(self.timeout_ms)
                    for order in orders:
                        result = self._execute_order(page, order)
                        results.append(result)
                        # M3 逐步落账：每个订单完成时立即回调持久化，
                        # 中断时已完成订单的过程记录不丢失（不再整批结束才落账）。
                        if on_order_result is not None:
                            on_order_result(result)
                finally:
                    browser.close()
        except Exception as exc:
            completed_refs = {result["external_ref"] for result in results}
            for order in orders:
                if order["external_ref"] not in completed_refs:
                    failure = self._failure(order, exc)
                    results.append(failure)
                    if on_order_result is not None:
                        on_order_result(failure)
        return {"results": results, **summarize_execution(results)}

    def reconcile_orders(self, plan: Plan) -> List[dict]:
        """M3 受控核对：按 external_ref 只读查询每个供应商订单状态，不创建新单。

        返回每个供应商的确定状态：
        - created：订单已存在（保留，不重复提交），附回读一致性问题
        - not_submitted：确定未提交（连接正常但无此单，可恢复）
        - unknown：回查失败（后台异常，无法确认，需人工核对）
        """
        orders = build_supplier_orders(plan)
        results: List[dict] = []
        if not orders:
            return results
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=self.headless, args=["--no-proxy-server"])
            try:
                page = browser.new_page()
                page.set_default_timeout(self.timeout_ms)
                for order in orders:
                    log: List[dict] = []
                    try:
                        existing = self._find_by_ref(page, order["external_ref"], log)
                        if existing is not None:
                            results.append({
                                "supplier_id": order["supplier_id"],
                                "external_ref": order["external_ref"],
                                "state": "created",
                                "order_id": existing.get("order_id"),
                                "problems": reconcile_receipt(order, existing),
                                "attempt_log": log,
                            })
                        else:
                            results.append({
                                "supplier_id": order["supplier_id"],
                                "external_ref": order["external_ref"],
                                "state": "not_submitted",
                                "order_id": None,
                                "problems": [],
                                "attempt_log": log,
                            })
                    except Exception as exc:
                        results.append({
                            "supplier_id": order["supplier_id"],
                            "external_ref": order["external_ref"],
                            "state": "unknown",
                            "order_id": None,
                            "problems": [f"回查失败：{type(exc).__name__}: {exc}"],
                            "attempt_log": log,
                        })
            finally:
                browser.close()
        return results

    def _execute_order(self, page, order: dict) -> dict:
        attempt_log: List[dict] = []
        try:
            existing = self._find_by_ref(page, order["external_ref"], attempt_log)
        except Exception as exc:
            # No submit was attempted. Preserve the actual failed lookup instead
            # of inventing a negative lookup result or dropping the entire trace.
            attempt_log.append({'step':'initial_lookup_error','error':f'{type(exc).__name__}: {exc}'[:300]})
            result = self._failure(order, exc, attempt_log)
            result['submission_state'] = 'not_attempted'
            return result
        if existing is not None:
            return self._receipt_result(order, existing, "already_exists", attempt_log)
        try:
            page.goto(f"{self.base_url}/purchase-orders/new")
            page.fill("#external-ref", order["external_ref"])
            page.fill("#supplier-id", order["supplier_id"])
            rows = page.locator("#lines-body tr")
            while rows.count() < len(order["lines"]):
                page.click("#add-line")
            for index, line in enumerate(order["lines"]):
                row = rows.nth(index)
                row.locator('[data-field="sku"]').fill(line["sku"])
                row.locator('[data-field="name"]').fill(line["name"])
                row.locator('[data-field="order_qty"]').fill(str(line["order_qty"]))
                row.locator('[data-field="unit_cost_cents"]').fill(str(line["unit_cost_cents"]))
            attempt_log.append({"step": "submit", "lines": len(order["lines"])})
            page.click("#submit-order")
            page.wait_for_url(re.compile(r"/purchase-orders/(?!new$)[^/]+$"),
                              timeout=self.timeout_ms)
            receipt = self._read_detail(page)
            attempt_log.append({"step": "readback", "order_id": receipt.get("order_id")})
            return self._receipt_result(order, receipt, "created", attempt_log)
        except Exception as exc:
            attempt_log.append({"step": "submit_error",
                                "error": f"{type(exc).__name__}: {exc}"[:300]})
            # 请求可能已在服务端成功：先按幂等键回查，不得直接再次提交。
            try:
                recovered = self._find_by_ref(page, order["external_ref"], attempt_log)
            except Exception as lookup_exc:
                # 回查本身失败（如后台整体不可达）：提交结果未知，
                # 可能已落库但无法确认——进入待人工核对，不得盲目重试或重复购买。
                attempt_log.append({"step": "recover_lookup_error",
                                    "error": f"{type(lookup_exc).__name__}: {lookup_exc}"[:300]})
                return self._unknown(order, exc, attempt_log)
            if recovered is not None:
                attempt_log.append({"step": "recovered",
                                    "order_id": recovered.get("order_id")})
                return self._receipt_result(order, recovered,
                                            "recovered_after_error", attempt_log)
            # 回查未找到：区分"确定未提交"与"结果未知"（M3）。
            # 连接拒绝（后台明确未收到请求）→ 确定未提交，可安全重试；
            # 超时/响应丢失（后台可能已落库但回查时延迟未出现）→ 结果未知，
            # 进入待人工核对，不得盲目重试或重复购买。
            msg = str(exc).lower()
            if ("connection_refused" in msg or "connection refused" in msg
                    or "econnrefused" in msg):
                attempt_log.append({"step": "confirmed_not_submitted",
                                    "reason": "connection_refused"})
                return self._failure(order, exc, attempt_log)
            attempt_log.append({"step": "unknown_not_confirmed",
                                "reason": "submit_error_but_no_order_found"})
            return self._unknown(order, exc, attempt_log)

    def _find_by_ref(self, page, external_ref: str,
                     attempt_log: Optional[List[dict]] = None) -> Optional[dict]:
        if attempt_log is not None:
            attempt_log.append({"step": "duplicate_check", "external_ref": external_ref})
        response = page.goto(f"{self.base_url}/purchase-orders?external_ref={quote(external_ref)}")
        # 区分"回查失败（结果未知）"与"正常无此单（确定未提交）"：
        # 5xx 是后台异常，无法确认订单状态，必须抛异常进入 recover_lookup_error，
        # 不能当作"无此单"误判为确定未提交。
        if response is not None and response.status >= 500:
            raise RuntimeError(f"回查后台返回 {response.status}，无法确认订单状态")
        if page.locator("a.order-link").count() == 0:
            if attempt_log is not None:
                attempt_log.append({"step": "duplicate_check_result", "found": False})
            return None
        page.locator("a.order-link").first.click()
        page.wait_for_load_state("domcontentloaded")
        if attempt_log is not None:
            attempt_log.append({"step": "duplicate_check_result", "found": True})
        return self._read_detail(page)

    def _read_detail(self, page) -> dict:
        lines = []
        rows = page.locator("#order-lines tbody tr")
        for index in range(rows.count()):
            row = rows.nth(index)
            lines.append({
                "sku": row.locator('[data-field="sku"]').inner_text(),
                "name": row.locator('[data-field="name"]').inner_text(),
                "order_qty": int(row.locator('[data-field="order_qty"]').inner_text()),
                "unit_cost_cents": int(row.locator('[data-field="unit_cost_cents"]').inner_text()),
            })
        return {
            "order_id": page.locator("#order-id").inner_text(),
            "external_ref": page.locator("#external-ref").inner_text(),
            "supplier_id": page.locator("#supplier-id").inner_text(),
            "status": page.locator("#order-status").inner_text(),
            "total_cost_cents": int(page.locator("#order-total-cents").inner_text()),
            "lines": lines,
        }

    @staticmethod
    def _receipt_result(order: dict, receipt: dict, status: str,
                        attempt_log: Optional[List[dict]] = None) -> dict:
        return {
            "supplier_id": order["supplier_id"],
            "external_ref": order["external_ref"],
            "status": status,
            "order_id": receipt.get("order_id"),
            "receipt": receipt,
            "problems": reconcile_receipt(order, receipt),
            "error": None,
            "attempt_log": attempt_log or [],
        }

    @staticmethod
    def _failure(order: dict, exc: Exception,
                 attempt_log: Optional[List[dict]] = None) -> dict:
        # 确定未提交（连接失败或回查确认无此单）：可安全重试。
        return {
            "supplier_id": order["supplier_id"],
            "external_ref": order["external_ref"],
            "status": "failed",
            "order_id": None,
            "receipt": None,
            "problems": [],
            "error": f"{type(exc).__name__}: {exc}",
            "submission_state": "not_attempted",
            "attempt_log": attempt_log or [],
        }

    @staticmethod
    def _unknown(order: dict, exc: Exception,
                 attempt_log: Optional[List[dict]] = None) -> dict:
        # 结果未知：提交可能已落库但回查失败，无法确认——需人工核对，
        # problems 非空使 summarize_execution 进入 NEEDS_RECONCILIATION。
        return {
            "supplier_id": order["supplier_id"],
            "external_ref": order["external_ref"],
            "status": "unknown",
            "order_id": None,
            "receipt": None,
            "problems": ["提交结果未知：回查失败，无法确认是否已创建申请，需人工核对"],
            "error": f"{type(exc).__name__}: {exc}",
            "submission_state": "unknown",
            "attempt_log": attempt_log or [],
        }
