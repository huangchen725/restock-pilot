import csv
from datetime import date, timedelta

import pytest

from app.models import DataContract, Policy
from app.rules import build_plan
from app.validation import (load_snapshot, load_dataset, prepare_snapshot, merged_rows,
                            snapshot_hash, validate_sales, validate_inventory,
                            validate_suppliers, cross_check)


def contract(**changes):
    return DataContract(quantity_unit="piece", available_is_net=True,
                        cost_includes_tax_shipping=True, inbound_eta_is_days=True, **changes)


def tables():
    sales = [{"date": (date(2026, 10, 1) + timedelta(days=i)).isoformat(),
              "sku": "A", "sold_qty": "4"} for i in range(28)]
    inventory = [dict(sku="A", name="商品", category="饮料", available_qty="0",
                      inbound_qty="0", inbound_eta="")]
    suppliers = [dict(sku="A", supplier_id="S", unit_cost_cents="100", lead_days="3",
                      pack_size="6", min_order_qty="12")]
    return sales, inventory, suppliers


def write_dataset(path, data=None, confirmed=True, encoding="utf-8"):
    path.mkdir(exist_ok=True)
    for name, rows in zip(("sales", "inventory", "suppliers"), data or tables()):
        with (path / (name + ".csv")).open("w", encoding=encoding, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    if confirmed:
        (path / "contract.json").write_text(contract().model_dump_json(), encoding="utf-8")


def test_window_is_calendar_based_and_order_invariant():
    sales, inv, sup = tables()
    sales.insert(0, dict(sku="A", date="2026-09-30", sold_qty="999"))
    a, errors = prepare_snapshot(sales, inv, sup, contract())
    b, errors_b = prepare_snapshot(list(reversed(sales)), inv, sup, contract())
    assert not errors and not errors_b
    assert a.window_start == "2026-10-01" and a.window_end == "2026-10-28"
    assert merged_rows(a)[0]["daily_sales"] == ["4"] * 28
    assert snapshot_hash(a) == snapshot_hash(b)
    args = ("p", 1, snapshot_hash(a), Policy(budget_cents=1800, cover_days=7, safety_days=2))
    assert build_plan(*args, merged_rows(a), a) == build_plan(*args, merged_rows(b), b)


def test_missing_days_require_explicit_zero_confirmation():
    sales, inv, sup = tables()
    sales.pop(10)
    snap, errors = prepare_snapshot(sales, inv, sup, contract())
    assert snap is None and any("1 missing days" in e for e in errors)
    snap, errors = prepare_snapshot(sales, inv, sup, contract(missing_sales="zero"))
    assert not errors
    row = merged_rows(snap)[0]
    assert row["daily_sales"][10] == "0" and row["missing_sales_days"] == 1
    plan = build_plan("p", 1, snapshot_hash(snap), Policy(budget_cents=10000), [row], snap)
    assert plan.items[0].confidence == "low"
    assert "missing_sales_filled" in plan.items[0].warnings


def test_all_missing_sku_is_not_silently_dropped():
    sales, inv, sup = tables()
    inv.append(dict(inv[0], sku="B")); sup.append(dict(sup[0], sku="B"))
    assert prepare_snapshot(sales, inv, sup, contract())[0] is None
    snap, errors = prepare_snapshot(sales, inv, sup, contract(missing_sales="zero"))
    assert not errors
    assert merged_rows(snap)[1]["missing_sales_days"] == 28


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-1", "abc", "", "1e1000"])
def test_invalid_sales_values(value):
    sales, _, _ = tables(); sales[0]["sold_qty"] = value
    assert validate_sales(sales)


@pytest.mark.parametrize("column", ["available_qty", "inbound_qty"])
@pytest.mark.parametrize("value", ["nan", "unknown", "1.5", "-2", ""])
def test_invalid_stock_is_reported(column, value):
    _, inv, _ = tables(); inv[0][column] = value
    assert validate_inventory(inv)


@pytest.mark.parametrize("column,value", [("unit_cost_cents", "0"), ("unit_cost_cents", "1.5"),
    ("pack_size", "0"), ("lead_days", "-1"), ("min_order_qty", "NaN")])
def test_supplier_numeric_validation(column, value):
    _, _, sup = tables(); sup[0][column] = value
    assert validate_suppliers(sup)


def test_duplicates_dates_and_cross_table_mismatches():
    sales, inv, sup = tables()
    assert validate_sales(sales + [sales[0]])
    assert validate_inventory(inv + inv)
    assert validate_suppliers(sup + sup)
    assert cross_check(sales, inv, [dict(sup[0], sku="B")])
    sales[0]["date"] = "2026-02-30"
    assert validate_sales(sales)
    sales[0]["date"] = "20261001"
    assert validate_sales(sales)


def test_inbound_requires_known_eta():
    _, inv, _ = tables(); inv[0]["inbound_qty"] = "10"
    assert validate_inventory(inv)
    inv[0]["inbound_eta"] = "-1"
    assert validate_inventory(inv)
    inv[0]["inbound_eta"] = "0"
    assert not validate_inventory(inv)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "gb18030"])
def test_supported_encodings_and_explicit_contract(tmp_path, encoding):
    write_dataset(tmp_path, encoding=encoding)
    snap, errors = load_snapshot(tmp_path)
    assert not errors and snap.inventory[0]["name"] == "商品"


def test_no_implicit_business_confirmation(tmp_path):
    write_dataset(tmp_path, confirmed=False)
    snap, errors = load_snapshot(tmp_path)
    assert snap is None and any("available_is_net" in e for e in errors)
    assert load_snapshot(tmp_path, contract())[0] is not None
    assert load_snapshot(tmp_path, DataContract())[0] is None


@pytest.mark.parametrize("csv_text", ["", "date,sku,sold_qty\n", "sku,sku\nA,A\n",
    "date,sku,sold_qty\n2026-10-01,A\n", "date,sku,sold_qty\n2026-10-01,A,1,extra\n"])
def test_empty_or_malformed_csv(tmp_path, csv_text):
    write_dataset(tmp_path)
    (tmp_path / "sales.csv").write_text(csv_text, encoding="utf-8")
    assert load_snapshot(tmp_path)[0] is None


def test_missing_file_and_bad_contract(tmp_path):
    assert load_dataset(tmp_path)[3]
    write_dataset(tmp_path)
    (tmp_path / "contract.json").write_text('{"available_is_net": "true"}')
    assert load_snapshot(tmp_path)[0] is None


@pytest.mark.parametrize("end", ["invalid", "0001-01-01", "2026-02-30", "2026-10-27"])
def test_invalid_or_future_dated_window(end):
    assert prepare_snapshot(*tables(), contract(window_end=end))[0] is None


def test_snapshot_hash_binds_confirmations_and_all_input():
    a, _ = prepare_snapshot(*tables(), contract())
    b, _ = prepare_snapshot(*tables(), contract(missing_sales="zero"))
    assert snapshot_hash(a) != snapshot_hash(b)
    b = a.model_copy(deep=True); b.inventory[0]["available_qty"] = "1"
    assert snapshot_hash(a) != snapshot_hash(b)
