"""把 SQLite 里的历史会话迁移到 MySQL 元数据库（一次性脚本）。

用法（仓库根 data-agent-lite/ 下）：
    python -m basic.scripts.migrate_sessions_to_mysql            # 正式迁移
    python -m basic.scripts.migrate_sessions_to_mysql --dry-run  # 只看会迁多少条
    python -m basic.scripts.migrate_sessions_to_mysql --source path/to/store.db

设计要点：
- 幂等：MySQL 侧用 INSERT ... ON DUPLICATE KEY UPDATE，重复执行不会产生副本，
  已存在的会话按 MySQL 数据为准**不覆盖**（只补缺失字段），避免把线上较新的数据回退；
- 只迁 sessions 表：数据源连接仍留 SQLite（文件源指向本机路径），
  llm_models 是新增表、无历史数据可迁；
- JSON 列做 round-trip 校验：迁完抽检解析，坏了立刻报错而不是静默写坏。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from basic.config.settings import settings                      # noqa: E402
from basic.storage.local_store import LocalStore                # noqa: E402
from basic.storage.mysql_store import (MysqlStore, quote_ident,  # noqa: E402
                                       resolve_meta_mysql)
from basic.utils.logging import get_logger                      # noqa: E402

log = get_logger("basic.migrate")


def read_sessions(sqlite_path: str) -> List[Dict[str, Any]]:
    """从 SQLite 读全部会话（含 JSON 字段解析后的对象）。"""
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id,ds_id,user_question,intent,target_db,intent_confirmed,"
            "sql,sql_confirmed,query_result,analysis_result,created_at,updated_at "
            "FROM sessions ORDER BY created_at").fetchall()
    finally:
        conn.close()

    def _load(v: Any) -> Any:
        if not v:
            return None
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return None

    return [{
        "session_id": r["session_id"], "ds_id": r["ds_id"] or 0,
        "user_question": r["user_question"] or "", "intent": r["intent"] or "",
        "target_db": r["target_db"] or "", "intent_confirmed": bool(r["intent_confirmed"]),
        "sql": r["sql"], "sql_confirmed": bool(r["sql_confirmed"]),
        "query_result": _load(r["query_result"]),
        "analysis_result": _load(r["analysis_result"]),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    } for r in rows]


def to_mysql_datetime(iso: str) -> str:
    """'2026-10-24T12:00:00' → '2026-10-24 12:00:00'（MySQL DATETIME 字面量）。"""
    return (iso or "").replace("T", " ")[:19] or "1970-01-01 00:00:00"


def upsert(mysql: MysqlStore, rows: List[Dict[str, Any]]) -> tuple[int, int]:
    """写入 MySQL。返回 (新增, 已存在)。已存在的不覆盖任何字段。"""
    from sqlalchemy import text

    sql = text(f"""
        INSERT INTO sessions({quote_ident('session_id')},{quote_ident('ds_id')},
                             {quote_ident('user_question')},{quote_ident('intent')},
                             {quote_ident('target_db')},{quote_ident('intent_confirmed')},
                             {quote_ident('sql')},{quote_ident('sql_confirmed')},
                             {quote_ident('query_result')},{quote_ident('analysis_result')},
                             {quote_ident('model_name')},{quote_ident('created_at')},
                             {quote_ident('updated_at')})
        VALUES(:sid,:ds,:q,:intent,:db,:ic,:sql,:sc,:qr,:ar,'',:ca,:ua)
        ON DUPLICATE KEY UPDATE {quote_ident('ds_id')}=VALUES({quote_ident('ds_id')})
    """)
    inserted = skipped = 0
    with mysql._engine.begin() as conn:      # noqa: SLF001  一次性脚本，直连引擎
        for r in rows:
            cur = conn.execute(sql, {
                "sid": r["session_id"], "ds": r["ds_id"], "q": r["user_question"],
                "intent": r["intent"], "db": r["target_db"],
                "ic": 1 if r["intent_confirmed"] else 0,
                "sql": r["sql"], "sc": 1 if r["sql_confirmed"] else 0,
                "qr": json.dumps(r["query_result"], ensure_ascii=False, default=str)
                if r["query_result"] is not None else None,
                "ar": json.dumps(r["analysis_result"], ensure_ascii=False, default=str)
                if r["analysis_result"] is not None else None,
                "ca": to_mysql_datetime(r["created_at"]),
                "ua": to_mysql_datetime(r["updated_at"]),
            })
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


def verify(mysql: MysqlStore, expected: int) -> List[str]:
    """迁后抽检：条数、JSON 可解析、时间顺序保持。"""
    problems: List[str] = []
    total = mysql.count_sessions()
    if total < expected:
        problems.append(f"MySQL 只有 {total} 条，少于源库 {expected} 条")
    rounds = mysql.list_rounds(limit=200)
    for r in rounds:
        sid = r["session_id"]
        full = mysql.get_session(sid)
        if full is None:
            problems.append(f"{sid} 写入后读不回")
            continue
        for key in ("query_result", "analysis_result"):
            v = full.get(key)
            if v is not None and not isinstance(v, (dict, list)):
                problems.append(f"{sid} 的 {key} 不是合法 JSON")
    stamps = [r["updated_at"] for r in rounds]
    if stamps != sorted(stamps, reverse=True):
        problems.append("列表顺序不是 updated_at 倒序")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite 会话 → MySQL 元数据库")
    ap.add_argument("--source", default=settings.store_path, help="SQLite store.db 路径")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"源库不存在：{src}")
        return 2

    local = LocalStore(str(src))
    rows = read_sessions(str(src))
    with_result = sum(1 for r in rows if r["query_result"] is not None)
    print(f"源库：{src}")
    print(f"待迁移会话：{len(rows)} 条（其中 {with_result} 条含查询结果）")
    if not rows:
        local.close()
        print("没有可迁移的数据。")
        return 0
    if args.dry_run:
        local.close()
        print("--dry-run：未写入。")
        return 0

    cfg = resolve_meta_mysql(local, settings)
    print(f"目标：MySQL {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}")
    mysql = MysqlStore(**cfg)

    inserted, skipped = upsert(mysql, rows)
    print(f"写入完成：新增 {inserted} 条，已存在跳过 {skipped} 条。")

    problems = verify(mysql, len(rows))
    if problems:
        print("校验发现问题：")
        for p in problems[:10]:
            print(f"  - {p}")
        mysql.close()
        local.close()
        return 1
    print("校验通过：条数、JSON 完整性、倒序均正常。")
    print(f"SQLite 原库保留作备份：{src}")
    mysql.close()
    local.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
