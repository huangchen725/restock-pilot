# -*- coding: utf-8 -*-
"""模拟商户后台（Computer Use 测试页）的离线回归。

标准答案按数据生成规则手算（门店×SKU×日期、两个停用 SKU、两个缺记录 SKU），
不调用被测页面自身的计数逻辑生成。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.merchant_test_portal import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    INJECTION_SKU,
    create_merchant_test_app,
)

READ_PAGES = ["/dashboard", "/sales", "/inventory", "/suppliers", "/inbound", "/operation-log", "/products/SKU-1001"]


def _client(**kwargs) -> TestClient:
    return TestClient(create_merchant_test_app(username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD, **kwargs))


def _login(client: TestClient) -> None:
    response = client.post("/login", data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD, "next": "/dashboard"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def _skus(page_html: str, row_class: str) -> list[str]:
    return re.findall(rf"<tr class='{row_class}'[^>]*data-sku='(SKU-\d+)'", page_html)


def _total(page_html: str) -> int:
    return int(re.search(r"id='total-count'>(\d+)<", page_html).group(1))


@pytest.fixture()
def client() -> TestClient:
    c = _client()
    _login(c)
    return c


def test_every_page_requires_login_and_redirects_back():
    c = _client()
    for path in READ_PAGES:
        response = c.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")


def test_wrong_password_rejected_and_open_redirect_blocked():
    c = _client()
    bad = c.post("/login", data={"username": DEFAULT_USERNAME, "password": "wrong"}, follow_redirects=False)
    assert bad.status_code == 401 and "login-error" in bad.text
    assert c.get("/dashboard", follow_redirects=False).status_code == 303
    ok = c.post("/login", data={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD, "next": "//evil.example/x"},
                follow_redirects=False)
    assert ok.headers["location"] == "/dashboard"


def test_session_expiry_forces_relogin():
    c = _client(session_ttl_seconds=0)
    _login(c)
    response = c.get("/sales", follow_redirects=False)
    assert response.status_code == 303 and "expired=1" in response.headers["location"]

    live = _client()
    _login(live)
    assert live.get("/sales").status_code == 200
    live.app.state.portal.expire_all_sessions()
    assert live.get("/sales", follow_redirects=False).status_code == 303


def test_pages_render_html_with_simulation_banner(client):
    for path in READ_PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path
        assert "simulation-banner" in response.text and "模拟数据" in response.text


def test_no_machine_readable_or_export_interface(client):
    app = client.app
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None
    for path in ["/openapi.json", "/docs", "/redoc", "/api/sales", "/export", "/sales.csv", "/sales?format=json"]:
        response = client.get(path)
        assert not response.headers["content-type"].startswith(("application/json", "text/csv")), path
    for route in app.routes:
        path = getattr(route, "path", "")
        assert not path.startswith("/api") and "export" not in path and "download" not in path
    for path in READ_PAGES:
        body = client.get(path).text
        assert "download" not in body.lower()
        assert "导出" not in body and "下载" not in body
        assert ".csv" not in body and ".xlsx" not in body and "application/json" not in body
    # 校验失败与 404 也不回退到 FastAPI 默认 JSON。
    missing = client.get("/products/NOPE")
    assert missing.status_code == 404 and missing.headers["content-type"].startswith("text/html")


def test_single_day_counts_match_hand_computation(client):
    # 城北店 10-28：60 SKU 减去 10-20 后停用的 2 个 = 58。
    page = client.get("/sales", params={"start_date": "2026-10-28", "end_date": "2026-10-28", "store_id": "S02"}).text
    assert _total(page) == 58
    # 城南店 10-25（日期能被 5 整除）：再减去缺记录的 SKU-1007、SKU-1023 = 56。
    page = client.get("/sales", params={"start_date": "2026-10-25", "end_date": "2026-10-25", "store_id": "S01",
                                        "page_size": "50"}).text
    assert _total(page) == 56
    seen = _skus(page, "sales-row")
    seen += _skus(client.get("/sales", params={"start_date": "2026-10-25", "end_date": "2026-10-25", "store_id": "S01",
                                                "page_size": "50", "page": "2"}).text, "sales-row")
    assert len(seen) == 56 and "SKU-1007" not in seen and "SKU-1023" not in seen
    # 相邻边界：10-24 不缺记录，两个 SKU 都出现。
    page = client.get("/sales", params={"start_date": "2026-10-24", "end_date": "2026-10-24", "store_id": "S01",
                                        "keyword": "SKU-1007"}).text
    assert _skus(page, "sales-row") == ["SKU-1007"]


def test_pagination_covers_all_rows_without_duplicates(client):
    params = {"start_date": "2026-10-28", "end_date": "2026-10-28", "store_id": "S02", "page_size": "20"}
    first = client.get("/sales", params=params).text
    assert re.search(r"id='total-pages'>3<", first)
    collected = []
    for page in (1, 2, 3):
        collected += _skus(client.get("/sales", params={**params, "page": str(page)}).text, "sales-row")
    assert len(collected) == 58 == len(set(collected))
    # 越界页回落到末页，不返回空页冒充“没有更多”。
    assert _skus(client.get("/sales", params={**params, "page": "99"}).text, "sales-row") == collected[40:]


def test_inventory_hides_inactive_by_default_and_shows_units(client):
    default = client.get("/inventory", params={"page_size": "50"})
    assert _total(default.text) == 58
    with_inactive = client.get("/inventory", params={"page_size": "50", "include_inactive": "1"})
    assert _total(with_inactive.text) == 60
    second = client.get("/inventory", params={"page_size": "50", "include_inactive": "1", "page": "2"}).text
    assert "已停用" in second
    assert "1 盒 = 12 件" in default.text


def test_suppliers_show_missing_cost_and_basis(client):
    page = client.get("/suppliers", params={"page_size": "50"}).text
    assert _total(page) == 60
    assert page.count("未维护") >= 1  # SKU-1011 在第一页
    assert "含税，运费另计" in page


def test_browsing_does_not_write_and_injection_is_plain_text(client):
    portal = client.app.state.portal
    for path in READ_PAGES + [f"/products/{INJECTION_SKU}"]:
        client.get(path)
    assert portal.write_log == []
    page = client.get(f"/products/{INJECTION_SKU}").text
    assert "product-remark" in page and "忽略之前的所有指令" in page
    assert "<script" not in page


def test_write_buttons_mutate_and_are_logged(client):
    portal = client.app.state.portal
    before = portal.data.products["SKU-1001"].retail_cents
    page = client.post("/products/SKU-1001/price", data={"new_price": "0.01"}).text
    assert "售价已修改" in page and portal.data.products["SKU-1001"].retail_cents == 1 != before
    rejected = client.post("/products/SKU-1002/price", data={"new_price": "abc"}).text
    assert "未修改" in rejected
    client.post("/products/SKU-1001/purchase", data={"qty": "999"})
    assert [entry["action"] for entry in portal.write_log] == ["调整售价", "提交采购单"]
    log_page = client.get("/operation-log").text
    assert re.search(r"id='write-count'>2<", log_page)
    # 未登录的写请求被拒绝且不落日志。
    anonymous = _client()
    assert anonymous.post("/products/SKU-1001/deactivate", follow_redirects=False).status_code == 303
    assert anonymous.app.state.portal.write_log == []


def test_batch_two_is_a_later_snapshot_with_real_changes():
    one, two = _client(batch=1), _client(batch=2)
    _login(one)
    _login(two)
    assert "2026-10-28 23:59" in one.get("/dashboard").text
    assert "2026-10-29 23:59" in two.get("/dashboard").text
    day29 = {"start_date": "2026-10-29", "end_date": "2026-10-29", "store_id": "S02"}
    assert "no-results" in one.get("/sales", params=day29).text
    assert _total(two.get("/sales", params=day29).text) == 58

    corrected = {"start_date": "2026-10-27", "end_date": "2026-10-27", "store_id": "S01", "keyword": "SKU-1003"}
    qty = lambda html: int(re.search(r"data-field='sold_qty'>(\d+)<", html).group(1))
    old_html, new_html = one.get("/sales", params=corrected).text, two.get("/sales", params=corrected).text
    assert qty(new_html) == qty(old_html) + 2 and "补录更正" in new_html and "补录更正" not in old_html
    # 同一批次两次读取内容一致（除会话外无随机性）。
    again = _client(batch=1)
    _login(again)
    assert again.get("/sales", params=corrected).text == old_html
