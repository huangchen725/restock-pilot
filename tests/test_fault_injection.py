# -*- coding: utf-8 -*-
"""故障注入评测：真实 Chromium + 采购后台 + ASGI 响应层故障注入。

门控 RUN_PLAYWRIGHT_E2E=1（同 Chromium E2E）。场景：
  F1 提交响应丢失（订单已落库）→ 先查后交 recovered_after_error
  F2 幂等重放 → already_exists，订单数不变
  F3 回读数据篡改 → reconcile 捕获 → NEEDS_RECONCILIATION
  F4 后台完全不可用 → failed，绝不盲目重试
  F5 提交挂起超时 → 先查后交恢复
"""

import asyncio
import os
import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.execution import PlaywrightProcurementAdapter
from app.models import Plan, PlanItem, Policy, TaskState
from app.procurement_portal import create_portal_app

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT_E2E") != "1",
    reason="set RUN_PLAYWRIGHT_E2E=1 to run fault injection",
)


class FaultInjector:
    """纯 ASGI 中间件：业务真实执行后注入响应层故障（订单可能已落库）。"""

    def __init__(self, app):
        self.app = app
        self.fail_next_submit = 0       # F1：接下来 N 次 POST 真实执行后替换为 502
        self.tamper_total_delta = 0     # F3：详情页 total 加上该 delta（分）
        self.hang_seconds = 0.0         # F5：真实落库后延迟响应
        self.injected = []
        self.submit_requests = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]

        if method == "POST" and path == "/purchase-orders":
            self.submit_requests += 1
            if self.hang_seconds > 0:
                bucket = []
                async def capture_hang(message):
                    bucket.append(message)
                await self.app(scope, receive, capture_hang)
                self.injected.append("hang_after_commit")
                await asyncio.sleep(self.hang_seconds)
                for message in bucket:
                    await send(message)
                return
            if self.fail_next_submit > 0:
                self.fail_next_submit -= 1
                self.injected.append("fake_502")
                bucket = []

                async def capture(message):
                    bucket.append(message)

                await self.app(scope, receive, capture)  # 订单真实落库
                await send({"type": "http.response.start", "status": 502,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"injected 502"})
                return

        if (method == "GET" and path.startswith("/purchase-orders/")
                and not path.endswith("/new") and self.tamper_total_delta):
            bucket = []

            async def capture(message):
                bucket.append(message)

            await self.app(scope, receive, capture)
            start = next(m for m in bucket if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in bucket
                            if m["type"] == "http.response.body")
            tampered, count = re.subn(
                br'''(id=["']order-total-cents["']>)(\d+)(<)''',
                lambda m: m.group(1) + str(int(m.group(2)) + self.tamper_total_delta).encode() + m.group(3),
                body,
            )
            if count:
                self.injected.append(f"tamper_total(+{self.tamper_total_delta})")
            headers = [(k, v) for k, v in start.get("headers", [])
                       if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(tampered)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": tampered})
            return

        await self.app(scope, receive, send)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _plan(plan_id: str) -> Plan:
    item = PlanItem(
        sku="SKU-FI", name="故障注入商品", category="测试",
        avg_daily_sales=2.0, lead_days=3, available_qty=0, inbound_qty=0,
        target_qty=10, gap=10, order_qty=10, unit_cost_cents=250,
        supplier_id="SUP-FI", pack_size=1, min_order_qty=1,
        line_total_cents=2500,
    )
    return Plan(plan_id=plan_id, version=1, dataset_hash="ds",
                policy=Policy(budget_cents=10000), items=[item],
                total_cost_cents=2500)


def _start_portal(tmp_path, injector):
    app = injector.app  # Assertions and requests must use this very same store.
    server = uvicorn.Server(uvicorn.Config(
        injector, host="127.0.0.1", port=_free_port(), log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.config.port or _free_port()}"
    # uvicorn.Server 在 run() 前已赋值 config.port
    for _ in range(50):
        try:
            if httpx.get(f"{base_url}/health", timeout=0.3, trust_env=False).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        pytest.fail("fault-injected portal did not start")
    return base_url, app, server, thread


def test_f1_submit_response_lost_recovers_by_lookup(tmp_path):
    injector = FaultInjector(create_portal_app(tmp_path / "p.db"))
    base_url, store_app, server, thread = _start_portal(tmp_path, injector)
    try:
        adapter = PlaywrightProcurementAdapter(base_url=base_url, headless=True,
                                               timeout_ms=8000)
        injector.fail_next_submit = 1
        report = adapter.execute_plan(_plan("f1"))
        result = report["results"][0]
        assert result["status"] == "recovered_after_error", result
        assert report["state"] == TaskState.COMPLETED.value
        assert "submit_error" in [s["step"] for s in result["attempt_log"]]
        assert "recovered" in [s["step"] for s in result["attempt_log"]]
        # 服务端只有一单：响应丢了但订单落库，先查后交没有二次提交
        assert len(store_app.state.store.list()) == 1
        assert injector.injected == ['fake_502'] and injector.submit_requests == 1
        actual = httpx.get(base_url+'/api/purchase-orders',trust_env=False).json()
        assert len(actual)==1 and actual[0]['total_cost_cents']==2500 and actual[0]['status']=='SUBMITTED'
        # 注入解除后重放：幂等命中，不新建
        replay = adapter.execute_plan(_plan("f1"))
        assert replay["results"][0]["status"] == "already_exists"
        assert len(store_app.state.store.list()) == 1
        assert injector.submit_requests == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_f2_idempotent_replay_no_duplicates(tmp_path):
    injector = FaultInjector(create_portal_app(tmp_path / "p.db"))
    base_url, store_app, server, thread = _start_portal(tmp_path, injector)
    try:
        adapter = PlaywrightProcurementAdapter(base_url=base_url, headless=True)
        first = adapter.execute_plan(_plan("f2"))
        second = adapter.execute_plan(_plan("f2"))
        assert first["results"][0]["status"] == "created"
        assert second["results"][0]["status"] == "already_exists"
        assert (first["results"][0]["order_id"]
                == second["results"][0]["order_id"])
        assert len(store_app.state.store.list()) == 1
        assert injector.submit_requests == 1
        assert len(httpx.get(base_url+'/api/purchase-orders',trust_env=False).json()) == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_f3_tampered_readback_marks_reconciliation(tmp_path):
    injector = FaultInjector(create_portal_app(tmp_path / "p.db"))
    base_url, store_app, server, thread = _start_portal(tmp_path, injector)
    try:
        adapter = PlaywrightProcurementAdapter(base_url=base_url, headless=True)
        first = adapter.execute_plan(_plan("f3"))
        assert first["state"] == TaskState.COMPLETED.value
        injector.tamper_total_delta = 1  # 回读时总额被 +1 分篡改
        second = adapter.execute_plan(_plan("f3"))
        result = second["results"][0]
        assert result["status"] == "already_exists"
        assert any("total mismatch" in p for p in result["problems"]), result["problems"]
        assert second["state"] == TaskState.NEEDS_RECONCILIATION.value
        assert 'tamper_total(+1)' in injector.injected and injector.submit_requests == 1
        actual = httpx.get(base_url+'/api/purchase-orders',trust_env=False).json()
        assert len(actual)==1 and actual[0]['total_cost_cents']==2500
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_f4_portal_down_fails_without_blind_retry(tmp_path):
    # Hold an unlistened loopback port: actual connection refusal, not Chromium's
    # unsafe-port rejection (port 1) from the old fixture.
    with socket.socket() as unavailable:
        unavailable.bind(('127.0.0.1',0))
        adapter = PlaywrightProcurementAdapter(base_url=f"http://127.0.0.1:{unavailable.getsockname()[1]}",
                                               headless=True, timeout_ms=4000)
        report = adapter.execute_plan(_plan("f4"))
    result = report["results"][0]
    assert result["status"] == "failed"
    assert report["state"] == TaskState.FAILED.value
    steps = [s["step"] for s in result["attempt_log"]]
    # A failed initial read cannot claim a completed negative lookup or submit.
    assert steps == ['duplicate_check','initial_lookup_error'], steps
    assert result['submission_state']=='not_attempted'
    assert 'ERR_CONNECTION_REFUSED' in result['error']


def test_f5_submit_hang_times_out_then_recovers(tmp_path):
    injector = FaultInjector(create_portal_app(tmp_path / "p.db"))
    base_url, store_app, server, thread = _start_portal(tmp_path, injector)
    try:
        adapter = PlaywrightProcurementAdapter(base_url=base_url, headless=True,
                                               timeout_ms=5000)
        injector.hang_seconds = 9.0  # 已落库，响应超过 adapter 5s 超时
        report = adapter.execute_plan(_plan("f5"))
        result = report["results"][0]
        assert result["status"] == "recovered_after_error", result
        assert report["state"] == TaskState.COMPLETED.value
        assert len(store_app.state.store.list()) == 1
        assert injector.injected == ['hang_after_commit'] and injector.submit_requests == 1
        assert 'submit_error' in [e['step'] for e in result['attempt_log']]
        assert 'recovered' in [e['step'] for e in result['attempt_log']]
        actual=httpx.get(base_url+'/api/purchase-orders',trust_env=False).json()
        assert len(actual)==1 and actual[0]['total_cost_cents']==2500 and actual[0]['status']=='SUBMITTED'
    finally:
        server.should_exit = True
        thread.join(timeout=5)
