"""Strict CSV validation and deterministic, explicitly confirmed snapshots."""

import csv
import hashlib
import io
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.models import DataContract, DatasetSnapshot, canonical_json

SALES_COLS = {"date", "sku", "sold_qty"}
INVENTORY_COLS = {"sku", "name", "category", "available_qty", "inbound_qty", "inbound_eta"}
SUPPLIERS_COLS = {"sku", "supplier_id", "unit_cost_cents", "lead_days", "pack_size", "min_order_qty"}


def read_csv_rows(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("unsupported text encoding")
    reader = csv.DictReader(io.StringIO(text), strict=True)
    fields = reader.fieldnames
    if not fields or len(set(fields)) != len(fields) or any(not f.strip() for f in fields):
        raise ValueError("missing or duplicate column names")
    rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError("row width does not match header")
    return [{k: v.strip() for k, v in row.items()} for row in rows]


def parse_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expect YYYY-MM-DD")
    return date.fromisoformat(value)


def number(value, minimum=0, integer=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < minimum or result > 10**12:
            raise ValueError("outside supported range")
        if integer and (not isinstance(value, str) or not re.fullmatch(r"\d+", value)):
            raise ValueError("expect non-negative integer")
        return result
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("expect numeric value") from exc


def _validate(rows, label, cols, numeric, identity, date_field=None):
    problems, seen = [], set()
    if not rows:
        return [f"{label}: empty table"]
    for i, row in enumerate(rows, 1):
        prefix = f"{label} row {i}"
        if cols - set(row):
            problems.append(f"{prefix}: missing columns {sorted(cols - set(row))}")
            continue
        for col in cols - {"inbound_eta"}:
            if not isinstance(row[col], str) or not row[col].strip():
                problems.append(f"{prefix}: {col} is required")
        key = tuple(row.get(col) for col in identity)
        if key in seen:
            problems.append(f"{prefix}: duplicate {','.join(identity)} {key}")
        seen.add(key)
        for col, (minimum, integer) in numeric.items():
            try:
                number(row[col], minimum, integer)
            except ValueError:
                problems.append(f"{prefix}: invalid {col} (finite {'integer' if integer else 'number'}, {minimum}..1000000000000)")
        if date_field:
            try:
                parse_day(row[date_field])
            except (ValueError, TypeError):
                problems.append(f"{prefix}: invalid {date_field} (YYYY-MM-DD)")
        if label == "inventory":
            try:
                inbound = number(row["inbound_qty"], integer=True)
                if inbound > 0 or row["inbound_eta"] != "":
                    number(row["inbound_eta"], integer=True)
            except ValueError:
                problems.append(f"{prefix}: inbound quantity requires a valid ETA in days")
    return problems


def validate_sales(rows):
    return _validate(rows, "sales", SALES_COLS, {"sold_qty": (0, False)}, ("sku", "date"), "date")


def validate_inventory(rows):
    return _validate(rows, "inventory", INVENTORY_COLS,
                     {"available_qty": (0, True), "inbound_qty": (0, True)}, ("sku",))


def validate_suppliers(rows):
    return _validate(rows, "suppliers", SUPPLIERS_COLS,
                     {"unit_cost_cents": (1, True), "lead_days": (0, True),
                      "pack_size": (1, True), "min_order_qty": (0, True)}, ("sku",))


def cross_check(sales, inventory, suppliers):
    sales_ids, inventory_ids, supplier_ids = ({r.get("sku") for r in rows} for rows in (sales, inventory, suppliers))
    problems = []
    for label, diff in (("sales without inventory", sales_ids - inventory_ids),
                        ("inventory without supplier", inventory_ids - supplier_ids),
                        ("supplier without inventory", supplier_ids - inventory_ids)):
        if diff:
            problems.append(f"{label}: {sorted(str(v) for v in diff)}")
    return problems  # Inventory without sales is governed by missing_sales.


def load_dataset(data_dir):
    tables, problems = [], []
    for name in ("sales", "inventory", "suppliers"):
        try:
            rows = read_csv_rows(Path(data_dir) / (name + ".csv"))
        except (OSError, ValueError, csv.Error):
            rows = []
            problems.append(f"{name}.csv: unreadable, missing, or malformed CSV")
        tables.append(rows)
    sales, inv, sup = tables
    problems += validate_sales(sales) + validate_inventory(inv) + validate_suppliers(sup)
    problems += cross_check(sales, inv, sup)
    return sales, inv, sup, problems


def prepare_snapshot(sales, inventory, suppliers, contract):
    problems = validate_sales(sales) + validate_inventory(inventory) + validate_suppliers(suppliers)
    problems += cross_check(sales, inventory, suppliers)
    if contract.quantity_unit != "piece":
        problems.append("confirm quantity_unit=piece across sales, inventory and supplier packaging")
    for field in ("available_is_net", "cost_includes_tax_shipping", "inbound_eta_is_days"):
        if not getattr(contract, field):
            problems.append(f"confirm {field}=true")
    if problems:
        return None, problems
    try:
        end = parse_day(contract.window_end) if contract.window_end else max(parse_day(r["date"]) for r in sales)
        start = end - timedelta(days=27)
    except (ValueError, OverflowError):
        return None, ["invalid 28-day window_end"]
    days = {(start + timedelta(days=i)).isoformat() for i in range(28)}
    if any(parse_day(r["date"]) > end for r in sales):
        return None, ["sales dates exceed confirmed window_end"]
    present = {}
    for row in sales:
        present.setdefault(row["sku"], set()).add(row["date"])
    if contract.missing_sales == "reject":
        for row in inventory:
            missing = days - present.get(row["sku"], set())
            if missing:
                problems.append(f"sales {row['sku']}: {len(missing)} missing days in window; supply data or explicitly confirm missing_sales=zero")
    if problems:
        return None, problems
    normalized = contract.model_copy(update={"window_end": end.isoformat()})
    snapshot = DatasetSnapshot(contract=normalized, window_start=start.isoformat(), window_end=end.isoformat(),
        sales=sorted(sales, key=lambda r: (r["sku"], r["date"])),
        inventory=sorted(inventory, key=lambda r: r["sku"]), suppliers=sorted(suppliers, key=lambda r: r["sku"]))
    return snapshot, []


def load_snapshot(data_dir, contract=None):
    sales, inv, sup, problems = load_dataset(data_dir)
    if contract is None:
        path = Path(data_dir) / "contract.json"
        if path.exists():
            try:
                contract = DataContract.model_validate_json(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                problems.append("contract.json: invalid data contract")
        else:
            contract = DataContract()
    if problems:
        return None, problems
    return prepare_snapshot(sales, inv, sup, contract)


def snapshot_hash(snapshot):
    return hashlib.sha256(canonical_json(snapshot.model_dump()).encode("utf-8")).hexdigest()


def merged_rows(snapshot):
    sup = {r["sku"]: r for r in snapshot.suppliers}
    sales = {(r["sku"], r["date"]): r["sold_qty"] for r in snapshot.sales}
    start = parse_day(snapshot.window_start)
    days = [(start + timedelta(days=i)).isoformat() for i in range(28)]
    rows = []
    for inv in snapshot.inventory:
        sku, supplier = inv["sku"], sup[inv["sku"]]
        rows.append(dict(sku=sku, name=inv["name"], category=inv["category"],
            available_qty=int(inv["available_qty"]), inbound_qty=int(inv["inbound_qty"]),
            inbound_eta_days=int(inv["inbound_eta"]) if inv["inbound_eta"] else None,
            supplier_id=supplier["supplier_id"], unit_cost_cents=int(supplier["unit_cost_cents"]),
            lead_days=int(supplier["lead_days"]), pack_size=int(supplier["pack_size"]), min_order_qty=int(supplier["min_order_qty"]),
            daily_sales=[sales.get((sku, day), "0") for day in days],
            missing_sales_days=sum((sku, day) not in sales for day in days)))
    return rows


def validate_policy(policy, snapshot):
    skus = {r["sku"] for r in snapshot.inventory}
    categories = {r["category"] for r in snapshot.inventory}
    problems = []
    if set(policy.excluded_skus) - skus:
        problems.append("excluded_skus contains unknown SKU")
    if set(policy.priority_categories) - categories:
        problems.append("priority_categories contains unknown category")
    return problems
