# -*- coding: utf-8 -*-
"""SQLite 持久层：WAL 模式 + 单写者纪律（所有写操作经由本模块）。"""

import sqlite3
import json
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
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS task_context (
    task_id TEXT PRIMARY KEY, busy INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT '', error TEXT,
    result_json TEXT, request_json TEXT
);
CREATE TABLE IF NOT EXISTS task_activity (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    kind TEXT NOT NULL, payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_task ON task_activity(task_id, seq);
"""

_LOCK = threading.RLock()


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


def save_dataset(conn, snapshot):
    from app.validation import snapshot_hash
    identifier = "ds-" + snapshot_hash(snapshot)
    with _LOCK, conn:
        conn.execute("INSERT OR IGNORE INTO datasets(dataset_id,snapshot_json) VALUES (?,?)",
                     (identifier, snapshot.model_dump_json()))
    return identifier


def get_dataset(conn, identifier):
    from app.models import DatasetSnapshot
    from app.validation import snapshot_hash
    with _LOCK:
        row = conn.execute("SELECT snapshot_json FROM datasets WHERE dataset_id=?", (identifier,)).fetchone()
    if not row:
        return None
    snapshot = DatasetSnapshot.model_validate_json(row[0])
    if identifier != "ds-" + snapshot_hash(snapshot):
        raise ValueError("数据快照指纹不一致，请重新上传。")
    return snapshot


def update_context(conn, task_id, **fields):
    allowed = {"busy", "phase", "error", "result_json", "request_json"}
    if not fields or not set(fields) <= allowed:
        raise ValueError("invalid context update")
    with _LOCK, conn:
        conn.execute("INSERT OR IGNORE INTO task_context(task_id) VALUES (?)", (task_id,))
        conn.execute("UPDATE task_context SET " + ",".join(k+"=?" for k in fields) + " WHERE task_id=?",
                     (*fields.values(), task_id))


def claim_task(conn, task_id, phase):
    with _LOCK, conn:
        conn.execute("INSERT OR IGNORE INTO task_context(task_id) VALUES (?)", (task_id,))
        cursor = conn.execute("UPDATE task_context SET busy=1,phase=?,error=NULL WHERE task_id=? AND busy=0",
                              (phase, task_id))
        return cursor.rowcount == 1


def get_context(conn, task_id):
    with _LOCK:
        row = conn.execute("SELECT * FROM task_context WHERE task_id=?", (task_id,)).fetchone()
    result = dict(row) if row else {"busy":0,"phase":"","error":None}
    for field in ("result", "request"):
        result[field] = json.loads(result.pop(field+"_json", None) or "null")
    return result


def activity(conn, task_id, kind, payload):
    with _LOCK, conn:
        conn.execute("INSERT INTO task_activity(task_id,kind,payload_json) VALUES (?,?,?)",
                     (task_id, kind, json.dumps(payload, ensure_ascii=False)))


def list_activity(conn, task_id):
    with _LOCK:
        rows = conn.execute("SELECT * FROM task_activity WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
    return [{"seq":r["seq"],"kind":r["kind"],"payload":json.loads(r["payload_json"]),
             "created_at":r["created_at"]} for r in rows]


def list_tasks(conn):
    with _LOCK:
        return [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC,rowid DESC LIMIT 100")]


def list_plan_versions(conn, task_id):
    with _LOCK:
        return [r[0] for r in conn.execute("SELECT version FROM plans WHERE task_id=? ORDER BY version", (task_id,))]


def recover_interrupted(conn):
    # Single API process only; never replay a possibly submitted purchase automatically.
    with _LOCK, conn:
        rows = conn.execute("SELECT t.task_id,t.state,c.phase FROM tasks t LEFT JOIN task_context c USING(task_id) WHERE c.busy=1 OR t.state='EXECUTING'").fetchall()
        for row in rows:
            state = row["state"]
            if state == "EXECUTING":
                state = "NEEDS_RECONCILIATION"
            elif state in ("NEW", "ANALYZING"):
                exists = conn.execute("SELECT 1 FROM plans WHERE task_id=?", (row["task_id"],)).fetchone()
                state = "NEEDS_REVIEW" if exists else "FAILED"
            conn.execute("UPDATE tasks SET state=?,approved_plan_hash=CASE WHEN ? IN ('FAILED','NEEDS_REVIEW') THEN NULL ELSE approved_plan_hash END,approved_version=CASE WHEN ? IN ('FAILED','NEEDS_REVIEW') THEN NULL ELSE approved_version END WHERE task_id=?",
                         (state,state,state,row["task_id"]))
            update_context(conn,row["task_id"],busy=0,phase="服务重启后待复核",error="上次操作因服务重启中断，请核对结果后再继续。")
            activity(conn,row["task_id"],"interrupted",{"state":state})
