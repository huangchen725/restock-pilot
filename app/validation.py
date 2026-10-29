# -*- coding: utf-8 -*-
"""数据导入与校验：编码自动探测（GBK 高频坑）、口径强制确认、结构校验。"""

import csv
import io
from pathlib import Path
from typing import Dict, List, Tuple

SALES_COLS = {"date", "sku", "sold_qty"}
INVENTORY_COLS = {"sku", "name", "category", "available_qty",
                  "inbound_qty", "inbound_eta"}
SUPPLIERS_COLS = {"sku", "supplier_id", "unit_cost_cents",
                  "lead_days", "pack_size", "min_order_qty"}


def read_csv_rows(path: str | Path) -> List[Dict[str, str]]:
    """依次尝试 utf-8 / utf-8-sig / gb18030，全部失败才报错。"""
    raw = Path(path).read_bytes()
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            return list(csv.DictReader(io.StringIO(text)))
        except UnicodeDecodeError as e:
            last_err = e
    raise ValueError(f"cannot decode {path} as utf-8/gb18030: {last_err}")


def validate_sales(rows: List[Dict[str, str]]) -> List[str]:
    problems = []
    for i, r in enumerate(rows):
        missing = SALES_COLS - set(r)
        if missing:
            problems.append(f"sales row {i}: missing columns {sorted(missing)}")
            continue
        try:
            q = float(r["sold_qty"])
            if q < 0:
                problems.append(f"sales row {i}: negative sold_qty {r['sold_qty']}")
        except ValueError:
            problems.append(f"sales row {i}: sold_qty not numeric: {r['sold_qty']!r}")
        try:
            from datetime import datetime
            datetime.strptime(r["date"], "%Y-%m-%d")
        except ValueError:
            problems.append(f"sales row {i}: invalid date {r['date']!r} (expect YYYY-MM-DD)")
    return problems


def validate_inventory(rows: List[Dict[str, str]]) -> List[str]:
    problems = []
    seen = set()
    for i, r in enumerate(rows):
        missing = INVENTORY_COLS - set(r)
        if missing:
            problems.append(f"inventory row {i}: missing {sorted(missing)}")
            continue
        if r["sku"] in seen:
            problems.append(f"inventory row {i}: duplicate sku {r['sku']}")
        seen.add(r["sku"])
        if int(r["available_qty"]) < 0:
            problems.append(f"inventory row {i}: negative stock {r['available_qty']}")
        if int(r["inbound_qty"]) < 0:
            problems.append(f"inventory row {i}: negative inbound {r['inbound_qty']}")
    return problems


def validate_suppliers(rows: List[Dict[str, str]]) -> List[str]:
    problems = []
    seen = set()
    for i, r in enumerate(rows):
        missing = SUPPLIERS_COLS - set(r)
        if missing:
            problems.append(f"suppliers row {i}: missing {sorted(missing)}")
            continue
        if r["sku"] in seen:
            problems.append(
                f"suppliers row {i}: sku {r['sku']} has multiple suppliers "
                "(v1 requires exactly one default supplier per sku)"
            )
        seen.add(r["sku"])
        if int(r["unit_cost_cents"]) <= 0:
            problems.append(f"suppliers row {i}: non-positive price {r['unit_cost_cents']}")
        if int(r["pack_size"]) < 1 or int(r["min_order_qty"]) < 0:
            problems.append(f"suppliers row {i}: invalid pack/moq for {r['sku']}")
    return problems


def cross_check(sales, inventory, suppliers) -> List[str]:
    problems = []
    s_skus = {r["sku"] for r in sales}
    i_skus = {r["sku"] for r in inventory}
    p_skus = {r["sku"] for r in suppliers}
    for label, a, b in (
        ("sales vs inventory", s_skus, i_skus),
        ("inventory vs suppliers", i_skus, p_skus),
    ):
        diff = a - b
        if diff:
            problems.append(f"{label}: sku mismatch {sorted(diff)}")
    return problems


def load_dataset(data_dir: str | Path) -> Tuple[List[Dict], List[Dict], List[Dict], List[str]]:
    """读入三份 CSV 并做全部校验；问题清单非空时调用方应转 NEEDS_INPUT。"""
    d = Path(data_dir)
    sales = read_csv_rows(d / "sales.csv")
    inv = read_csv_rows(d / "inventory.csv")
    sup = read_csv_rows(d / "suppliers.csv")
    problems = (
        validate_sales(sales) + validate_inventory(inv)
        + validate_suppliers(sup) + cross_check(sales, inv, sup)
    )
    return sales, inv, sup, problems
