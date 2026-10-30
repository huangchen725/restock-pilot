"""Upload boundary: fixed field names, bounded bytes, immutable validated snapshot."""
import csv
from app.validation import parse_csv_bytes, prepare_snapshot, snapshot_hash

MAX_FILE_BYTES = 5 * 1024 * 1024


def ingest(files, contract):
    tables, problems = [], []
    for name in ("sales", "inventory", "suppliers"):
        data = files[name]
        if len(data) > MAX_FILE_BYTES:
            problems.append(f"{name}.csv 超过 5 MB，请缩小数据范围。")
            tables.append([])
            continue
        try:
            tables.append(parse_csv_bytes(data))
        except (ValueError, csv.Error):
            tables.append([])
            problems.append(f"{name}.csv 无法解析，请检查编码、表头和列数。")
    if problems:
        return None, problems
    return prepare_snapshot(*tables, contract)


def summary(snapshot):
    return {"dataset_id":"ds-"+snapshot_hash(snapshot),"dataset_hash":snapshot_hash(snapshot),
            "window_start":snapshot.window_start,"window_end":snapshot.window_end,
            "sku_count":len(snapshot.inventory),"contract":snapshot.contract.model_dump(),
            "categories":sorted({r["category"] for r in snapshot.inventory}),
            "skus":[r["sku"] for r in snapshot.inventory]}
