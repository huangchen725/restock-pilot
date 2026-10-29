# -*- coding: utf-8 -*-
"""真实 Chromium 端到端验收；默认跳过，RUN_PLAYWRIGHT_E2E=1 时启用。"""

import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.execution import PlaywrightProcurementAdapter
from app.models import Plan, PlanItem, Policy
from app.procurement_portal import create_portal_app


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(os.getenv("RUN_PLAYWRIGHT_E2E") != "1",
                    reason="set RUN_PLAYWRIGHT_E2E=1 to run Chromium E2E")
def test_real_browser_submit_readback_and_idempotency(tmp_path):
    port = _free_port()
    app = create_portal_app(tmp_path / "portal.db")
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                # trust_env=False: local loopback must never go through system proxy
                if httpx.get(f"{base_url}/health", timeout=0.2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("procurement portal did not start")

        item = PlanItem(
            sku="SKU-E2E", name="浏览器验收商品", category="测试",
            avg_daily_sales=2.0, lead_days=3, available_qty=0, inbound_qty=0,
            target_qty=12, gap=12, order_qty=12, unit_cost_cents=250,
            supplier_id="SUP-E2E", pack_size=1, min_order_qty=1,
            line_total_cents=3000,
        )
        plan = Plan(plan_id="e2e-task", version=1, dataset_hash="ds",
                    policy=Policy(budget_cents=10000), items=[item],
                    total_cost_cents=3000)
        adapter = PlaywrightProcurementAdapter(base_url=base_url, headless=True)
        first = adapter.execute_plan(plan)
        assert first["state"] == "COMPLETED", first
        assert first["results"][0]["status"] == "created"
        second = adapter.execute_plan(plan)
        assert second["state"] == "COMPLETED", second
        assert second["results"][0]["status"] == "already_exists"
        assert first["results"][0]["order_id"] == second["results"][0]["order_id"]
        assert len(app.state.store.list()) == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
