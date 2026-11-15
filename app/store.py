"""SQLite 访问：运行记录（含报告快照）、规则权重覆盖与责任归集版本。

表结构：
- ``runs``：每次抽检一行，报告整份 JSON 快照。
- ``attribution_versions``：责任归集版本。每次仲裁 / 重算产生一个候选版本，
  先对账、再在**同一个事务**里把旧版本标记 superseded、新版本置为 active；
  同一 run 只允许一个 active（部分唯一索引保证）。失败时旧 active 原样保留。
- ``allocations``：已认领问题的分配流水，``allocation_id`` 由「问题 + 供应商」
  决定，INSERT OR IGNORE 保证重试同一批不会新增分配。

只有对账通过（``reconcile.ok``）的归集结果才允许发布；发布动作要么整体生效
（版本 + 分配流水 + 可选的总分报告），要么完全不生效。
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import threading

from . import settings

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  stage_index INTEGER NOT NULL DEFAULT 0,
  rows INTEGER NOT NULL DEFAULT 0,
  total_score REAL,
  grade TEXT,
  weight_overrides TEXT,
  report_json TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attribution_versions (
  version_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  batch_key TEXT NOT NULL,
  content_key TEXT,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  ledger_json TEXT NOT NULL,
  reconcile_json TEXT NOT NULL,
  resolutions_json TEXT NOT NULL,
  error TEXT,
  UNIQUE (run_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attr_active
  ON attribution_versions (run_id) WHERE state = 'active';
CREATE TABLE IF NOT EXISTS allocations (
  allocation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  problem_id TEXT NOT NULL,
  supplier TEXT NOT NULL,
  version_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (run_id, problem_id, supplier)
);
"""


class StoreError(Exception):
    """归集版本无法发布（对账失败 / 事务冲突）→ 路由层转 409，上一版保留。"""



def connect() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript(SCHEMA)
            _CONN.commit()
        return _CONN


def reset_connection() -> None:
    """测试用：切换 DB 路径后重建连接。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_run(run_id: str, source: str, weight_overrides: dict | None) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO runs (run_id, created_at, source, status, stage, stage_index, weight_overrides)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, now(), source, "running", "排队中", 0,
             json.dumps(weight_overrides or {}, ensure_ascii=False)),
        )
        conn.commit()


def set_progress(run_id: str, stage_index: int, stage: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET stage_index=?, stage=? WHERE run_id=?",
            (stage_index, stage, run_id),
        )
        conn.commit()


def finish_run(run_id: str, report: dict) -> None:
    """写入完成状态；若归集结果对账通过，同时原子发布首个归集版本。

    发布失败不影响本次运行可用性：报告快照照常落库，归集结果标记 rejected 留痕，
    结果页继续展示报告内的账本，工单与总分均可用。
    """
    conn = connect()
    score = report["score"]
    with _LOCK:
        conn.execute(
            "UPDATE runs SET status='done', stage='已完成', stage_index=99, rows=?,"
            " total_score=?, grade=?, report_json=? WHERE run_id=?",
            (report["dataset"]["rows"], score["total"], score["grade"],
             json.dumps(report, ensure_ascii=False), run_id),
        )
        conn.commit()

    ledger = report.get("attribution") or {}
    reconcile = report.get("reconcile") or {}
    if not ledger.get("items"):
        return
    try:
        publish_attribution(run_id, ledger, reconcile, report.get("resolutions"), None)
    except StoreError as exc:
        record_failed_attribution(run_id, ledger, reconcile,
                                  report.get("resolutions"), str(exc))


def fail_run(run_id: str, message: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET status='failed', stage='失败', error=? WHERE run_id=?",
            (message, run_id),
        )
        conn.commit()


def get_run(run_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_runs(limit: int = 20) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT run_id, created_at, source, status, stage, rows, total_score, grade, error"
        " FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_report(run_id: str, report: dict, weights: dict | None) -> None:
    conn = connect()
    score = report["score"]
    with _LOCK:
        conn.execute(
            "UPDATE runs SET total_score=?, grade=?, report_json=?, weight_overrides=? WHERE run_id=?",
            (score["total"], score["grade"], json.dumps(report, ensure_ascii=False),
             json.dumps(weights or {}, ensure_ascii=False), run_id),
        )
        conn.commit()


def count_runs() -> int:
    conn = connect()
    row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


# ---------------------------------------------------------------- 责任归集版本

def publish_attribution(run_id: str, ledger: dict, reconcile: dict,
                        resolutions: dict | None = None,
                        report: dict | None = None) -> dict:
    """对账通过后原子切换生效版本。

    一个事务内完成：旧 active → superseded、新版本 → active、分配流水补齐；
    可选地同时更新 runs 的报告快照（重算总分）。任一语句失败整体回滚，
    上一版总分 / 供应商结果 / 工单保持可用。
    """
    if not reconcile.get("ok"):
        raise StoreError("对账未通过，拒绝切换：" + "；".join(reconcile.get("errors") or ["未知原因"]))
    conn = connect()
    now_text = now()
    content_key = ledger.get("content_key") or ledger.get("batch_key") or ""
    with _LOCK:
        existing = conn.execute(
            "SELECT * FROM attribution_versions WHERE run_id=? AND state='active'",
            (run_id,),
        ).fetchone()
        if existing is not None and existing["content_key"] == content_key:
            run_row = conn.execute(
                "SELECT total_score, grade FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            same_score = (
                report is None
                or (run_row is not None
                    and run_row["total_score"] == report["score"]["total"]
                    and run_row["grade"] == report["score"]["grade"])
            )
            if same_score:
                # 重试同一批：内容完全一致 → 不新增版本、不新增分配，直接返回生效版本
                return _version_to_dict(existing)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM attribution_versions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            seq = int(row["seq"]) + 1
            version_id = f"{run_id}-v{seq}"
            conn.execute(
                "UPDATE attribution_versions SET state='superseded'"
                " WHERE run_id=? AND state='active'",
                (run_id,),
            )
            conn.execute(
                "INSERT INTO attribution_versions"
                " (version_id, run_id, seq, batch_key, content_key, state, created_at,"
                "  ledger_json, reconcile_json, resolutions_json, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (version_id, run_id, seq, ledger["batch_key"], content_key, "active", now_text,
                 json.dumps(ledger, ensure_ascii=False),
                 json.dumps(reconcile, ensure_ascii=False),
                 json.dumps(resolutions or {}, ensure_ascii=False)),
            )
            for item in ledger.get("items") or []:
                if item.get("status") != "claimed" or not item.get("allocation_id"):
                    continue
                # 分配号由「问题 + 供应商」决定，跨 run 稳定；入库主键再带上 run_id，
                # 否则同一批数据跑第二个 run 时会被 INSERT OR IGNORE 静默吞掉。
                # UNIQUE(run_id, problem_id, supplier) 保证同一 run 重试不新增流水。
                conn.execute(
                    "INSERT OR IGNORE INTO allocations"
                    " (allocation_id, run_id, problem_id, supplier, version_id, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (f"{run_id}:{item['allocation_id']}", run_id, item["problem_id"],
                     item["owner"], version_id, now_text),
                )
            if report is not None:
                score = report["score"]
                conn.execute(
                    "UPDATE runs SET total_score=?, grade=?, report_json=?, weight_overrides=?"
                    " WHERE run_id=?",
                    (score["total"], score["grade"],
                     json.dumps(report, ensure_ascii=False),
                     json.dumps(report.get("weights_used") or {}, ensure_ascii=False),
                     run_id),
                )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StoreError(f"归集版本写入失败，已保留上一版：{exc}") from exc
    return get_attribution(run_id) or {}


def record_failed_attribution(run_id: str, ledger: dict | None, reconcile: dict,
                              resolutions: dict | None, message: str) -> str:
    """把失败尝试留痕（state='rejected'），不影响 active 版本。"""
    conn = connect()
    now_text = now()
    with _LOCK:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM attribution_versions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        seq = int(row["seq"]) + 1
        version_id = f"{run_id}-v{seq}"
        conn.execute(
            "INSERT INTO attribution_versions"
            " (version_id, run_id, seq, batch_key, state, created_at,"
            "  ledger_json, reconcile_json, resolutions_json, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (version_id, run_id, seq, (ledger or {}).get("batch_key") or "", "rejected",
             now_text,
             json.dumps(ledger or {}, ensure_ascii=False),
             json.dumps(reconcile or {}, ensure_ascii=False),
             json.dumps(resolutions or {}, ensure_ascii=False), message),
        )
        conn.commit()
    return version_id


def get_attribution(run_id: str, version_id: str | None = None) -> dict | None:
    """返回指定版本；默认 active 版本。找不到返回 None。"""
    conn = connect()
    if version_id:
        row = conn.execute(
            "SELECT * FROM attribution_versions WHERE run_id=? AND version_id=?",
            (run_id, version_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM attribution_versions WHERE run_id=? AND state='active'",
            (run_id,),
        ).fetchone()
    return _version_to_dict(row) if row else None


def list_attributions(run_id: str, limit: int = 20) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT version_id, run_id, seq, batch_key, state, created_at, error"
        " FROM attribution_versions WHERE run_id=? ORDER BY seq DESC LIMIT ?",
        (run_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def allocations_for(run_id: str) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM allocations WHERE run_id=? ORDER BY created_at, allocation_id",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def allocation_count(run_id: str) -> int:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM allocations WHERE run_id=?", (run_id,)
    ).fetchone()
    return int(row["n"])


def _version_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key in ("ledger_json", "reconcile_json", "resolutions_json"):
        raw = data.pop(key, None)
        try:
            data[key.replace("_json", "")] = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data[key.replace("_json", "")] = None
    return data


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = data.pop("report_json", None)
    data["report"] = json.loads(raw) if raw else None
    try:
        data["weight_overrides"] = json.loads(data.get("weight_overrides") or "{}")
    except json.JSONDecodeError:
        data["weight_overrides"] = {}
    return data


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def weight_overrides() -> dict:
    raw = get_setting("weight_overrides", "{}") or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {
        "columns": {k: float(v) for k, v in (data.get("columns") or {}).items()},
        "rule_weights": {k: float(v) for k, v in (data.get("rule_weights") or {}).items()},
    }


def save_weight_overrides(columns: dict, rule_weights: dict) -> dict:
    payload = {
        "columns": {k: float(v) for k, v in columns.items()},
        "rule_weights": {k: float(v) for k, v in rule_weights.items()},
    }
    set_setting("weight_overrides", json.dumps(payload, ensure_ascii=False))
    return payload
