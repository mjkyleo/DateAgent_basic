"""本地持久化：SQLite 保存数据源配置与会话状态（设计文档 §5 storage/local_store.py）。

MVP 定位：轻量 stdlib sqlite3，无 ORM、无迁移框架。
安全说明：MySQL 密码以明文存本地文件（MVP 取舍，已在 README 标注）；
生产应放系统密钥链。连接字符串只在内存中组装，不落日志。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.logging import get_logger

log = get_logger("basic.storage")

# 历史轮次列表只需要结果里的三个标量；走 SQLite JSON1 直接取，
# 避免把整段 query_result（可能上千行）反序列化进内存。
_HAS_JSON1 = True


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LocalStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # RLock：create/update 内部会调 get_*（同一线程重入），必须可重入
        self._lock = threading.RLock()
        # timeout= → SQLite busy_timeout：
        # uvicorn 多线程 + WAL 下偶发 "database is locked"，给它 10s 排队而不是立刻报错
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS datasources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'mysql',
                host TEXT DEFAULT '', port INTEGER DEFAULT 3306,
                user TEXT DEFAULT '', password TEXT DEFAULT '',
                database TEXT DEFAULT '',
                file_path TEXT DEFAULT '',
                allowed_ops TEXT NOT NULL DEFAULT 'read',
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                ds_id INTEGER DEFAULT 0,
                user_question TEXT DEFAULT '',
                intent TEXT DEFAULT '',
                target_db TEXT DEFAULT '',
                intent_confirmed INTEGER NOT NULL DEFAULT 0,
                sql TEXT DEFAULT NULL,
                sql_confirmed INTEGER NOT NULL DEFAULT 0,
                query_result TEXT DEFAULT NULL,
                analysis_result TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)
            # 索引：轮次列表按 updated_at 倒序 + 常按 ds_id 过滤；
            # 没有索引时轮次一多就是全表扫（历史列表是首屏必拉的）。
            for ddl in ("CREATE INDEX IF NOT EXISTS idx_sessions_updated "
                        "ON sessions(updated_at DESC)",
                        "CREATE INDEX IF NOT EXISTS idx_sessions_ds ON sessions(ds_id)",
                        "CREATE INDEX IF NOT EXISTS idx_ds_active ON datasources(is_active)"):
                self._conn.execute(ddl)
            # 旧库升级：SQLite 无 IF NOT EXISTS 语法的 ADD COLUMN，按列探测后补
            for table, col, ddl in (("sessions", "intent", "TEXT DEFAULT ''"),
                                    ("sessions", "target_db", "TEXT DEFAULT ''"),
                                    ("datasources", "file_path", "TEXT DEFAULT ''"),
                                    # 写权限上限：老库默认只读，绝不因为升级就突然能删表
                                    ("datasources", "allowed_ops",
                                     "TEXT NOT NULL DEFAULT 'read'")):
                if col not in self._columns_of(table):
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            self._probe_json1()

    def _probe_json1(self) -> None:
        """探测 JSON1 支持（老版 SQLite 可能没编进来），决定轮次列表走哪条路径。"""
        global _HAS_JSON1
        try:
            self._conn.execute("SELECT json_extract('{\"a\":1}','$.a')").fetchone()
            _HAS_JSON1 = True
        except sqlite3.OperationalError:
            _HAS_JSON1 = False
            log.warning("当前 SQLite 未启用 JSON1，历史轮次列表将退回 JSON 解析（较慢）")

    def _columns_of(self, table: str) -> set:
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _session_columns(self) -> set:  # 兼容旧名（测试/外部可能引用）
        return self._columns_of("sessions")

    # ------------------------------------------------------------ 数据源
    def create_datasource(self, *, name: str, type: str = "mysql", host: str = "",
                          port: int = 3306, user: str = "", password: str = "",
                          database: str = "", file_path: str = "",
                          allowed_ops: str = "read") -> Dict[str, Any]:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO datasources(name,type,host,port,user,password,database,"
                "file_path,allowed_ops,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, type, host, port, user, password, database, file_path,
                 allowed_ops or "read", _now()))
            return self.get_datasource(int(cur.lastrowid or 0)) or {}

    _DS_COLS = ("id,name,type,host,port,user,password,database,file_path,"
                "allowed_ops,is_active,created_at")

    def get_datasource(self, ds_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._DS_COLS} FROM datasources WHERE id=?", (ds_id,)).fetchone()
        return self._row_to_ds(row) if row else None

    def list_datasources(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._DS_COLS} FROM datasources ORDER BY id").fetchall()
        return [self._row_to_ds(r) for r in rows]

    def update_datasource(self, ds_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = ("name", "host", "port", "user", "password", "database", "file_path",
                   "allowed_ops")
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        # 约定：password 传空串 = 保持原密码（前端编辑表单未改密码时传空）
        if "password" in sets and sets["password"] == "":
            del sets["password"]
        if not sets:
            return self.get_datasource(ds_id)
        sql = "UPDATE datasources SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
        with self._lock, self._conn:
            self._conn.execute(sql, (*sets.values(), ds_id))
        return self.get_datasource(ds_id)

    def delete_datasource(self, ds_id: int) -> bool:
        """删除数据源，并级联清理它名下的历史轮次（否则历史列表里全是连不上的孤儿）。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM datasources WHERE id=?", (ds_id,))
            if cur.rowcount <= 0:
                return False
            removed = self._conn.execute(
                "DELETE FROM sessions WHERE ds_id=?", (ds_id,)).rowcount
        if removed:
            log.info("删除数据源 %s，同时清理 %s 条历史轮次", ds_id, removed)
        return True

    def set_active(self, ds_id: int) -> bool:
        with self._lock, self._conn:
            exists = self._conn.execute(
                "SELECT 1 FROM datasources WHERE id=?", (ds_id,)).fetchone()
            if not exists:
                return False
            self._conn.execute("UPDATE datasources SET is_active=0")
            self._conn.execute("UPDATE datasources SET is_active=1 WHERE id=?", (ds_id,))
            return True

    def get_active(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._DS_COLS} FROM datasources WHERE is_active=1").fetchone()
        return self._row_to_ds(row) if row else None

    @staticmethod
    def _row_to_ds(row: tuple) -> Dict[str, Any]:
        return {"id": row[0], "name": row[1], "type": row[2], "host": row[3],
                "port": row[4], "user": row[5], "password": row[6],
                "database": row[7], "file_path": row[8] or "",
                "allowed_ops": row[9] or "read",
                "is_active": bool(row[10]), "created_at": row[11]}

    # ------------------------------------------------------------ 会话
    def create_session(self, *, ds_id: int, user_question: str,
                       intent: str = "", target_db: str = "") -> str:
        sid = uuid.uuid4().hex[:12]
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions(session_id,ds_id,user_question,intent,target_db,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (sid, ds_id, user_question, intent, target_db, now, now))
        return sid

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id,ds_id,user_question,intent,target_db,intent_confirmed,sql,"
                "sql_confirmed,query_result,analysis_result,created_at,updated_at FROM sessions"
                " WHERE session_id=?", (session_id,)).fetchone()
        return self._row_to_session(row) if row else None

    def update_session(self, session_id: str, **fields: Any) -> None:
        allowed = {"ds_id", "user_question", "intent", "target_db", "intent_confirmed",
                   "sql", "sql_confirmed", "query_result", "analysis_result"}
        sets: Dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k in ("query_result", "analysis_result") and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False, default=str)
            if k in ("intent_confirmed", "sql_confirmed"):
                v = 1 if v else 0
            sets[k] = v
        if not sets:
            return
        sets["updated_at"] = _now()
        sql = "UPDATE sessions SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE session_id=?"
        with self._lock, self._conn:
            self._conn.execute(sql, (*sets.values(), session_id))

    @staticmethod
    def _row_to_session(row: tuple) -> Dict[str, Any]:
        def _load(v: Optional[str]) -> Any:
            if not v:
                return None
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return None
        return {"session_id": row[0], "ds_id": row[1], "user_question": row[2],
                "intent": row[3] or "", "target_db": row[4] or "",
                "intent_confirmed": bool(row[5]), "sql": row[6],
                "sql_confirmed": bool(row[7]), "query_result": _load(row[8]),
                "analysis_result": _load(row[9]), "created_at": row[10],
                "updated_at": row[11]}

    # ------------------------------------------------------------ 轮次历史
    def list_rounds(self, limit: int = 20, ds_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """历史轮次摘要（倒序）。只回轻量级字段，明细按需取 get_round。

        关键：row_count / cost_ms / suggest_chart / summary_text 由 SQLite 的
        json_extract 直接从 TEXT 列里取标量（有 JSON1 时），不把整段结果 JSON
        反序列化 —— 一轮结果上限 1000 行，拉 20 轮就是 2 万行白解析。
        """
        with self._lock:
            rows = self._conn.execute(self._rounds_sql(ds_id), self._rounds_args(ds_id, limit)).fetchall()
        out = []
        for r in rows:
            if _HAS_JSON1:
                row_count, cost_ms = r[6], r[7]
                has_result = r[10] is not None and r[10] != ""
            else:                                    # 无 JSON1：退回解析（兼容老构建）
                qr = self._json_or(r[10])
                ar = self._json_or(r[11])
                row_count = (qr or {}).get("row_count")
                cost_ms = (qr or {}).get("cost_ms")
                has_result = bool(qr)
            out.append({
                "session_id": r[0], "ds_id": r[1], "user_question": r[2],
                "intent": r[3] or "", "target_db": r[4] or "", "sql": r[5],
                "row_count": row_count,
                "cost_ms": cost_ms,
                "suggest_chart": r[8] or "table",
                "summary_text": r[9] or "",
                "has_result": has_result,
                "created_at": r[12], "updated_at": r[13],
            })
        return out

    def _rounds_sql(self, ds_id: Optional[int]) -> str:
        cols = "session_id,ds_id,user_question,intent,target_db,sql"
        if _HAS_JSON1:
            cols += (", json_extract(query_result,'$.row_count')"
                     ", json_extract(query_result,'$.cost_ms')"
                     ", json_extract(analysis_result,'$.suggest_chart')"
                     ", json_extract(analysis_result,'$.summary_text')")
        else:
            cols += ", NULL, NULL, NULL, NULL"
        sql = f"SELECT {cols},query_result,analysis_result,created_at,updated_at FROM sessions"
        if ds_id:
            sql += " WHERE ds_id=?"
        return sql + " ORDER BY updated_at DESC, rowid DESC LIMIT ?"

    @staticmethod
    def _rounds_args(ds_id: Optional[int], limit: int) -> tuple:
        args: List[Any] = []
        if ds_id:
            args.append(ds_id)
        args.append(max(1, min(int(limit), 200)))
        return tuple(args)

    def delete_round(self, session_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            return cur.rowcount > 0

    @staticmethod
    def _json_or(v: Optional[str]) -> Optional[Dict[str, Any]]:
        if not v:
            return None
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["LocalStore"]
