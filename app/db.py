# -*- coding: utf-8 -*-
"""SQLite 持久层：WAL 模式 + 单写者纪律（所有写操作经由本模块）。"""

import sqlite3
import threading
from pathlib import Path

from app.models import TaskState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    approved_plan_hash TEXT,
    approved_version   INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    task_id     TEXT NOT NULL,
    dataset_hash TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    plan_json   TEXT NOT NULL,
    plan_hash   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (plan_id, version)
);
CREATE TABLE IF NOT EXISTS execution_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    external_ref TEXT NOT NULL,
    status      TEXT NOT NULL,
    receipt_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_execution_task ON execution_records(task_id, id);
CREATE TABLE IF NOT EXISTS execution_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    external_ref TEXT,
    payload_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_task ON execution_events(task_id, seq);
"""

_LOCK = threading.Lock()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    with _LOCK, conn:
        conn.executescript(_SCHEMA)
    return conn


def upsert_task(conn, task_id: str, state: TaskState,
                approved_plan_hash=None, approved_version=None):
    with _LOCK, conn:
        conn.execute(
            """INSERT INTO tasks (task_id, state, approved_plan_hash, approved_version)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 state=excluded.state,
                 approved_plan_hash=excluded.approved_plan_hash,
                 approved_version=excluded.approved_version,
                 updated_at=datetime('now')""",
            (task_id, state.value, approved_plan_hash, approved_version),
        )


def get_task(conn, task_id: str):
    with _LOCK:
        return conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()


def save_plan(conn, plan) -> str:
    from app.models import plan_hash
    h = plan_hash(plan)
    with _LOCK, conn:
        conn.execute(
            """INSERT OR REPLACE INTO plans
               (plan_id, version, task_id, dataset_hash, policy_json, plan_json, plan_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (plan.plan_id, plan.version, plan.plan_id, plan.dataset_hash,
             plan.policy.model_dump_json(), plan.model_dump_json(), h),
        )
    return h


def get_plan(conn, plan_id: str, version: int):
    with _LOCK:
        row = conn.execute(
            "SELECT * FROM plans WHERE plan_id = ? AND version = ?",
            (plan_id, version),
        ).fetchone()
    if row is None:
        return None, None
    from app.models import Plan, plan_hash
    plan = Plan.model_validate_json(row["plan_json"])
    # 不信任冗余存储的 hash 字段：审批与执行必须绑定当前方案内容。
    return plan, plan_hash(plan)


def save_execution_record(conn, task_id: str, plan_id: str, version: int,
                          external_ref: str, status: str, receipt: dict) -> None:
    """每次执行尝试均追加审计记录；重复尝试不去重，便于回放。"""
    import json
    with _LOCK, conn:
        conn.execute(
            """INSERT INTO execution_records
               (task_id, plan_id, version, external_ref, status, receipt_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, plan_id, version, external_ref, status,
             json.dumps(receipt, ensure_ascii=False, sort_keys=True)),
        )


def list_execution_records(conn, task_id: str):
    import json
    with _LOCK:
        rows = conn.execute(
            """SELECT id, plan_id, version, external_ref, status, receipt_json, created_at
               FROM execution_records WHERE task_id = ? ORDER BY id""",
            (task_id,),
        ).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["receipt"] = json.loads(record.pop("receipt_json"))
        records.append(record)
    return records


def append_execution_event(conn, task_id: str, event_type: str,
                           external_ref: str | None, payload: dict) -> int:
    """append-only 审计账本：只增不改不删，seq 即回放顺序。

    供故障回放与审计演示使用；写入失败向上抛（账本不完整比静默丢事件更危险）。
    """
    import json
    with _LOCK, conn:
        cursor = conn.execute(
            """INSERT INTO execution_events
               (task_id, event_type, external_ref, payload_json)
               VALUES (?, ?, ?, ?)""",
            (task_id, event_type, external_ref,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
    return cursor.lastrowid


def list_execution_events(conn, task_id: str):
    import json
    with _LOCK:
        rows = conn.execute(
            """SELECT seq, event_type, external_ref, payload_json, created_at
               FROM execution_events WHERE task_id = ? ORDER BY seq""",
            (task_id,),
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["payload"] = json.loads(event.pop("payload_json"))
        events.append(event)
    return events
