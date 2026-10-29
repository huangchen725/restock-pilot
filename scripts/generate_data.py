# -*- coding: utf-8 -*-
"""生成 30 SKU 样例数据：三份标准 CSV（含刻意的波动/零销量/在途差异）。"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)

DATA = Path(__file__).resolve().parents[1] / "data"
DATA.mkdir(exist_ok=True)

CATEGORIES = ["饮料", "零食", "日用", "个护"]
NAMES = ["商品{:02d}"] * 30

sku_rows = []
for i in range(1, 31):
    category = CATEGORIES[i % 4]
    pattern = random.choice(["stable", "volatile", "zerodays", "newitem"])
    sku = f"SKU-{i:03d}"
    base = random.uniform(3, 25)
    if pattern == "stable":
        daily = [max(0, random.gauss(base, base * 0.1)) for _ in range(28)]
        daily = [round(x, 1) for x in daily]
    elif pattern == "volatile":
        daily = [round(max(0, random.gauss(base, base * 0.8)), 1) for _ in range(28)]
    elif pattern == "zerodays":
        daily = [0.0 if random.random() < 0.4 else round(base, 1) for _ in range(28)]
    else:  # newitem：只有最近 7 天有销量
        daily = [0.0] * 21 + [round(random.uniform(1, 8), 1) for _ in range(7)]

    available = random.randint(0, 120)
    inbound = random.choice([0, 0, 0, random.randint(10, 60)])
    eta = random.randint(2, 10) if inbound else ""
    unit_cost = random.randint(300, 8000)  # 分
    lead = random.randint(1, 10)
    pack = random.choice([1, 1, 6, 12, 24])
    moq = random.choice([0, 10, 24, 48])
    sku_rows.append(dict(
        sku=sku, name=f"商品{i:02d}", category=category, pattern=pattern,
        daily=daily, available=available, inbound=inbound, eta=eta,
        unit_cost=unit_cost, lead=lead, pack=pack, moq=moq,
    ))

start = date(2026, 10, 1) - timedelta(days=28)
with open(DATA / "sales.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["date", "sku", "sold_qty"])
    for d in range(28):
        day = (start + timedelta(days=d)).isoformat()
        for r in sku_rows:
            w.writerow([day, r["sku"], r["daily"][d]])

with open(DATA / "inventory.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sku", "name", "category", "available_qty", "inbound_qty", "inbound_eta"])
    for r in sku_rows:
        w.writerow([r["sku"], r["name"], r["category"],
                    r["available"], r["inbound"], r["eta"]])

with open(DATA / "suppliers.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sku", "supplier_id", "unit_cost_cents", "lead_days",
                "pack_size", "min_order_qty"])
    for r in sku_rows:
        w.writerow([r["sku"], f"SUP-{r['lead']:02d}", r["unit_cost"],
                    r["lead"], r["pack"], r["moq"]])

patterns = {}
for r in sku_rows:
    patterns[r["pattern"]] = patterns.get(r["pattern"], 0) + 1
print("生成完成:", DATA)
print("SKU 模式分布:", patterns)
