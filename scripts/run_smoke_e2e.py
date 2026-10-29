# -*- coding: utf-8 -*-
"""端到端冒烟：create -> approve -> execute -> status -> 采购后台落单核对。

用法：
    python scripts/run_smoke_e2e.py [--budget-cents 500000] [--base http://127.0.0.1:8100]
依赖 RESTOCK_AGENT_MODE / PROCUREMENT_PORTAL_URL 等环境变量由服务端自行读取。
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--budget-cents", type=int, default=500000)
    args = parser.parse_args()

    # trust_env=False：本地服务直连，绝不走系统代理（沙箱 HTTP_PROXY 会劫持请求）
    client = httpx.Client(base_url=args.base, timeout=300, trust_env=False)

    # 1. create（规则引擎出 plan；required 模式下含真实 Planner）
    resp = client.post("/tasks", json={"policy": {"budget_cents": args.budget_cents}})
    if resp.status_code != 200:
        print(f"CREATE FAILED {resp.status_code}: {resp.text[:800]}")
        return 1
    created = resp.json()
    task_id = created["task_id"]
    plan_hash = created.get("plan_hash")
    agent = created.get("agent", {})
    print(f"[1/4] create  task={task_id} state={created['state']} "
          f"items={len(created['plan']['items'])} total={created['plan']['total_cost_cents']}c "
          f"agent={agent.get('status')}")

    # 2. approve
    resp = client.post(f"/tasks/{task_id}/approve")
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

    portal_base = args.base.replace(":8100", ":8200")
    portal = httpx.Client(base_url=portal_base, timeout=30, trust_env=False)
    orders = portal.get("/api/purchase-orders").json()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
