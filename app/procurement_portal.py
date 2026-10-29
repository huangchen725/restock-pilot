# -*- coding: utf-8 -*-
"""自建采购后台：申请单列表 / 新建申请 / 申请单详情三页。

表单与 API 共用同一幂等存储；external_ref 全局唯一，同键同内容复用原单，
同键不同内容返回 409。仅用于本地演示，不包含真实供应商交易。
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.models import canonical_json

ROOT = Path(__file__).resolve().parents[1]


class OrderLineIn(BaseModel):
    sku: str = Field(min_length=1)
    name: str = Field(min_length=1)
    order_qty: int = Field(gt=0)
    unit_cost_cents: int = Field(ge=0)


class OrderIn(BaseModel):
    external_ref: str = Field(min_length=3, max_length=200)
    supplier_id: str = Field(min_length=1, max_length=100)
    lines: List[OrderLineIn] = Field(min_length=1)


class OrderConflict(Exception):
    pass


class ProcurementStore:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS purchase_orders (
                    order_id TEXT PRIMARY KEY,
                    external_ref TEXT NOT NULL UNIQUE,
                    supplier_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)

    @staticmethod
    def _materialize(order: OrderIn, order_id: str) -> dict:
        lines = []
        for line in order.lines:
            payload = line.model_dump()
            payload["line_total_cents"] = line.order_qty * line.unit_cost_cents
            lines.append(payload)
        return {
            "order_id": order_id,
            "external_ref": order.external_ref,
            "supplier_id": order.supplier_id,
            "status": "SUBMITTED",
            "lines": lines,
            "total_cost_cents": sum(line["line_total_cents"] for line in lines),
        }

    def create(self, order: OrderIn) -> tuple[dict, bool]:
        with self.lock:
            existing = self.get_by_ref(order.external_ref)
            if existing is not None:
                comparable = {
                    "external_ref": existing["external_ref"],
                    "supplier_id": existing["supplier_id"],
                    "lines": [{key: line[key] for key in
                               ("sku", "name", "order_qty", "unit_cost_cents")}
                              for line in existing["lines"]],
                }
                if canonical_json(comparable) != canonical_json(order.model_dump()):
                    raise OrderConflict("external_ref already exists with different content")
                return existing, False
            order_id = f"PO-{uuid.uuid4().hex[:12].upper()}"
            payload = self._materialize(order, order_id)
            with self.conn:
                self.conn.execute(
                    "INSERT INTO purchase_orders (order_id, external_ref, supplier_id, payload_json) "
                    "VALUES (?, ?, ?, ?)",
                    (order_id, order.external_ref, order.supplier_id,
                     json.dumps(payload, ensure_ascii=False)),
                )
            return payload, True

    def get_by_ref(self, external_ref: str) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute(
                "SELECT payload_json FROM purchase_orders WHERE external_ref = ?",
                (external_ref,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def get(self, order_id: str) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute(
                "SELECT payload_json FROM purchase_orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def list(self, external_ref: Optional[str] = None) -> List[dict]:
        with self.lock:
            if external_ref:
                rows = self.conn.execute(
                    "SELECT payload_json FROM purchase_orders WHERE external_ref = ?",
                    (external_ref,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT payload_json FROM purchase_orders ORDER BY created_at DESC, order_id DESC"
                ).fetchall()
            return [json.loads(row["payload_json"]) for row in rows]


STYLE = """
<style>
:root{color-scheme:dark;--bg:#0d1420;--panel:#152033;--line:#293852;--text:#edf3ff;--muted:#9fb0ca;--accent:#65d6b4}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,'Microsoft YaHei',sans-serif}
nav{padding:18px 6vw;background:#111b2b;border-bottom:1px solid var(--line);display:flex;gap:24px;align-items:center}
nav strong{color:var(--accent);font-size:20px;margin-right:auto}nav a{color:var(--text);text-decoration:none}main{max-width:1120px;margin:34px auto;padding:0 24px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:24px;margin-top:20px}h1{font-size:27px;margin:0}h2{font-size:18px}
.muted{color:var(--muted)}table{border-collapse:collapse;width:100%;margin-top:16px}th,td{text-align:left;padding:11px 10px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600}
input{background:#0d1625;color:var(--text);border:1px solid #3a4c6b;border-radius:7px;padding:10px;width:100%}label{display:block;color:var(--muted);margin:12px 0 6px}
button,.button{background:var(--accent);color:#09231c;border:0;border-radius:8px;padding:10px 18px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block;margin-top:16px}
button.secondary{background:#2a3b55;color:var(--text)}.grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}.badge{color:var(--accent);font-weight:700}.error{color:#ff9b9b}.actions{display:flex;gap:12px;align-items:end}
</style>
"""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)}</title>{STYLE}</head>
<body><nav><strong>RestockPilot 采购后台</strong><a href='/purchase-orders'>申请单列表</a>
<a href='/purchase-orders/new'>新建采购申请</a></nav><main>{body}</main></body></html>"""


def _list_page(orders: List[dict], external_ref: Optional[str]) -> str:
    rows = "".join(
        f"<tr class='order-row'><td><a class='order-link' href='/purchase-orders/{_e(o['order_id'])}'>{_e(o['order_id'])}</a></td>"
        f"<td>{_e(o['external_ref'])}</td><td>{_e(o['supplier_id'])}</td>"
        f"<td>{len(o['lines'])}</td><td>{o['total_cost_cents']}</td><td class='badge'>{_e(o['status'])}</td></tr>"
        for o in orders
    )
    if not rows:
        rows = "<tr><td colspan='6' id='no-orders' class='muted'>没有匹配的采购申请单</td></tr>"
    filter_value = _e(external_ref or "")
    return _layout("采购申请单", f"""<h1>采购申请单</h1><p class='muted'>所有金额均以整数分存储与展示。</p>
<div class='panel'><form method='get' action='/purchase-orders'><label for='ref-filter'>按幂等键精确查询</label>
<div class='actions'><input id='ref-filter' name='external_ref' value='{filter_value}' placeholder='plan-v1-SUPPLIER'>
<button type='submit'>查询</button></div></form></div>
<div class='panel'><table><thead><tr><th>单号</th><th>external_ref</th><th>供应商</th><th>行数</th><th>总额（分）</th><th>状态</th></tr></thead>
<tbody>{rows}</tbody></table></div>""")


NEW_PAGE_BODY = """<h1>新建采购申请</h1><p class='muted'>固定适配器仅填写已批准方案；服务端重新计算金额。</p>
<div class='panel'><form id='order-form' method='post' action='/purchase-orders'>
<div class='grid'><div><label for='external-ref'>幂等键 external_ref</label><input id='external-ref' name='external_ref' required></div>
<div><label for='supplier-id'>供应商</label><input id='supplier-id' name='supplier_id' required></div></div>
<h2>采购明细</h2><table><thead><tr><th>SKU</th><th>商品名称</th><th>采购量</th><th>单价（分）</th></tr></thead>
<tbody id='lines-body'><tr><td><input data-field='sku' required></td><td><input data-field='name' required></td>
<td><input data-field='order_qty' type='number' min='1' required></td><td><input data-field='unit_cost_cents' type='number' min='0' required></td></tr></tbody></table>
<input type='hidden' id='lines-json' name='lines_json'><button type='button' class='secondary' id='add-line'>添加一行</button>
<button type='submit' id='submit-order'>提交采购申请</button></form></div>
<script>
const body=document.querySelector('#lines-body');
document.querySelector('#add-line').addEventListener('click',()=>{const row=body.rows[0].cloneNode(true);row.querySelectorAll('input').forEach(i=>i.value='');body.appendChild(row);});
document.querySelector('#order-form').addEventListener('submit',()=>{const lines=[...body.rows].map(r=>{const value=f=>r.querySelector(`[data-field="${f}"]`).value;return {sku:value('sku'),name:value('name'),order_qty:Number(value('order_qty')),unit_cost_cents:Number(value('unit_cost_cents'))};});document.querySelector('#lines-json').value=JSON.stringify(lines);});
</script>"""


def _detail_page(order: dict) -> str:
    rows = "".join(
        f"<tr><td data-field='sku'>{_e(line['sku'])}</td><td data-field='name'>{_e(line['name'])}</td>"
        f"<td data-field='order_qty'>{line['order_qty']}</td><td data-field='unit_cost_cents'>{line['unit_cost_cents']}</td>"
        f"<td>{line['line_total_cents']}</td></tr>" for line in order["lines"]
    )
    return _layout(f"申请单 {order['order_id']}", f"""<h1>采购申请单 <span id='order-id'>{_e(order['order_id'])}</span></h1>
<p class='muted'>详情页是执行回读核对的唯一依据。</p><div class='panel'><div class='grid'>
<div><h2>幂等键</h2><p id='external-ref'>{_e(order['external_ref'])}</p><h2>供应商</h2><p id='supplier-id'>{_e(order['supplier_id'])}</p></div>
<div><h2>状态</h2><p class='badge' id='order-status'>{_e(order['status'])}</p><h2>总额（分）</h2><p id='order-total-cents'>{order['total_cost_cents']}</p></div></div></div>
<div class='panel'><table id='order-lines'><thead><tr><th>SKU</th><th>商品名称</th><th>采购量</th><th>单价（分）</th><th>行总额（分）</th></tr></thead><tbody>{rows}</tbody></table></div>""")


def create_portal_app(db_path: str | Path | None = None) -> FastAPI:
    store = ProcurementStore(db_path or os.getenv(
        "PROCUREMENT_DB_PATH", str(ROOT / "procurement.db")
    ))
    portal = FastAPI(title="RestockPilot Demo Procurement Portal", version="1.0.0")
    portal.state.store = store

    @portal.get("/health")
    def health():
        return {"status": "ok"}

    @portal.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/purchase-orders", status_code=303)

    @portal.get("/purchase-orders", response_class=HTMLResponse, include_in_schema=False)
    def list_orders(external_ref: Optional[str] = None):
        return _list_page(store.list(external_ref), external_ref)

    @portal.get("/purchase-orders/new", response_class=HTMLResponse, include_in_schema=False)
    def new_order():
        return _layout("新建采购申请", NEW_PAGE_BODY)

    @portal.post("/purchase-orders", include_in_schema=False)
    async def submit_form(request: Request):
        form = await request.form()
        try:
            lines = json.loads(str(form.get("lines_json", "")))
            order = OrderIn(
                external_ref=str(form.get("external_ref", "")),
                supplier_id=str(form.get("supplier_id", "")),
                lines=lines,
            )
            created, _ = store.create(order)
            return RedirectResponse(f"/purchase-orders/{created['order_id']}", status_code=303)
        except OrderConflict as exc:
            return HTMLResponse(_layout("幂等键冲突", f"<h1 class='error'>提交被拒绝</h1><p>{_e(exc)}</p>"), status_code=409)
        except (ValueError, json.JSONDecodeError) as exc:
            return HTMLResponse(_layout("表单错误", f"<h1 class='error'>表单无效</h1><p>{_e(exc)}</p>"), status_code=422)

    @portal.get("/purchase-orders/{order_id}", response_class=HTMLResponse, include_in_schema=False)
    def order_detail(order_id: str):
        order = store.get(order_id)
        if order is None:
            raise HTTPException(404, "purchase order not found")
        return _detail_page(order)

    @portal.get("/api/purchase-orders")
    def api_list(external_ref: Optional[str] = None):
        return store.list(external_ref)

    @portal.get("/api/purchase-orders/by-ref/{external_ref}")
    def api_by_ref(external_ref: str):
        order = store.get_by_ref(external_ref)
        if order is None:
            raise HTTPException(404, "purchase order not found")
        return order

    @portal.get("/api/purchase-orders/{order_id}")
    def api_detail(order_id: str):
        order = store.get(order_id)
        if order is None:
            raise HTTPException(404, "purchase order not found")
        return order

    @portal.post("/api/purchase-orders", status_code=201)
    def api_create(order: OrderIn):
        try:
            created, is_new = store.create(order)
            return {"order": created, "created": is_new}
        except OrderConflict as exc:
            raise HTTPException(409, str(exc))

    return portal


app = create_portal_app()
