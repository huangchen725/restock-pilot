# -*- coding: utf-8 -*-
"""端到端冒烟：create -> approve -> execute -> status -> 采购后台落单核对。

用法：
    python scripts/run_smoke_e2e.py [--budget-cents 500000] [--base http://127.0.0.1:8100]
依赖 RESTOCK_AGENT_MODE / PROCUREMENT_PORTAL_URL 等环境变量由服务端自行读取。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--portal", default="http://127.0.0.1:8200")
    parser.add_argument("--budget-cents", type=int, default=500000)
    args = parser.parse_args()

    load_environment()
    token = os.getenv("RESTOCK_APPROVAL_TOKEN")
    if not token:
        print("独立冒烟需要与服务相同的 RESTOCK_APPROVAL_TOKEN；请在本地 .env 配置后重启服务。日常使用工作台无需此项配置。")
        return 1
    # The compatibility header waits for jobs; the product UI uses polling.
    client = httpx.Client(base_url=args.base, timeout=900, trust_env=False,
                         headers={"Prefer":"wait"})

    # 1. create（规则引擎出 plan；required 模式下含真实 Planner）
    resp = client.post("/tasks", json={"policy": {"budget_cents": args.budget_cents}})
    if resp.status_code != 200:
        print(f"CREATE FAILED {resp.status_code}: {resp.text[:800]}")
        return 1
    created = resp.json()
    if created.get("state") != "NEEDS_REVIEW" or not created.get("plan"):
        print("CREATE INCOMPLETE: " + json.dumps(created, ensure_ascii=False)[:800])
        return 1
    task_id = created["task_id"]
    plan_hash = created.get("plan_hash")
    agent = created.get("agent", {})
    print(f"[1/4] create  task={task_id} state={created['state']} "
          f"items={len(created['plan']['items'])} total={created['plan']['total_cost_cents']}c "
          f"agent={agent.get('status')}")

    # 2. approve
    resp = client.post(f"/tasks/{task_id}/approve",
        headers={"X-Restock-Approval":token},
        json={"version":created["plan"]["version"],"plan_hash":plan_hash})
    if resp.status_code != 200:
        print(f"APPROVE FAILED {resp.status_code}: {resp.text[:800]}")
        return 1
    print(f"[2/4] approve state={resp.json().get('state')}")

    # 3. execute（真实 Playwright 录单）
    resp = client.post(f"/tasks/{task_id}/execute")
    if resp.status_code != 200:
        print(f"EXECUTE FAILED {resp.status_code}: {resp.text[:800]}")
        return 1
    execution = resp.json()
    report = execution["execution"]
    print(f"[3/4] execute state={execution['state']} "
          f"succeeded={report.get('succeeded')}/{report.get('total')}")
    for result in report.get("results", []):
        print(f"      - {result['external_ref']} status={result['status']} "
              f"order={result.get('order_id')} problems={result.get('problems') or '-'} "
              f"error={result.get('error') or '-'}")

    # 4. status + 采购后台核对
    resp = client.get(f"/tasks/{task_id}")
    status = resp.json()
    print(f"[4/4] status  state={status['state']} approved_hash={str(status.get('approved_plan_hash'))[:16]}...")

    portal = httpx.Client(base_url=args.portal, timeout=30, trust_env=False)
    orders = [order for order in portal.get("/api/purchase-orders").json()
              if order["external_ref"].startswith(task_id+"-")]
    print(f"PORTAL  orders={len(orders)}")
    for order in orders:
        print(f"      - {order['order_id']} ref={order['external_ref']} "
              f"supplier={order['supplier_id']} lines={len(order['lines'])} "
              f"total={order['total_cost_cents']}c status={order['status']}")

    summary = {"task_id": task_id, "plan_hash": plan_hash,
               "final_state": status["state"],
               "execution_state": execution["state"],
               "portal_orders": len(orders)}
    print("SMOKE_SUMMARY " + json.dumps(summary, ensure_ascii=False))
    expected = {}
    for item in created["plan"]["items"]:
        if item["order_qty"]:
            expected.setdefault(item["supplier_id"], []).append(
                (item["sku"],item["order_qty"],item["unit_cost_cents"]))
    actual = {o["supplier_id"]:sorted((i["sku"],i["order_qty"],i["unit_cost_cents"])
              for i in o["lines"]) for o in orders}
    valid = (status["state"] == execution["state"] == "COMPLETED" and len(orders)==len(expected)
        and actual == {supplier:sorted(lines) for supplier,lines in expected.items()}
        and all(o["status"]=="SUBMITTED" and o["external_ref"]==f"{task_id}-v{created['plan']['version']}-{o['supplier_id']}"
                and o["total_cost_cents"]==sum(qty*cost for _,qty,cost in actual[o["supplier_id"]]) for o in orders))
    client.close(); portal.close()
    if not valid:
        print("SMOKE FAILED: 申请单、批准内容或最终状态不一致。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
