"""MySQL 适配器：SQLAlchemy + PyMySQL，只读查询执行（设计文档 §9.1/§11）。

只读三重落实：
1. 连接账号最小权限（运维建议，见 README）；
2. simple_query 入口的 SQL 已由上层安全门校验（SELECT-only）；
3. 会话级 max_execution_time 超时 + 行数封顶，防慢查询/大结果集。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .base import DataSourceAdapter


class MysqlAdapter(DataSourceAdapter):
    def __init__(self, *, host: str, port: int, user: str, password: str,
                 database: str) -> None:
        self.host, self.port, self.user, self.database = host, port, user, database
        url = (f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
               f"@{host}:{port}/{database or ''}?charset=utf8mb4")
        # pool_pre_ping：长连接失效自动重连；future 模式统一 2.0 用法
        self.engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800,
                                    connect_args={"connect_timeout": 5})

    # ------------------------------------------------------------ 连通性
    def test_connection(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    # ------------------------------------------------------------ 元数据
    def list_databases(self) -> List[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                "ORDER BY SCHEMA_NAME")).fetchall()
        return [r[0] for r in rows]

    def list_tables(self, db_name: str) -> List[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME"), {"db": db_name}).fetchall()
        return [r[0] for r in rows]

    def get_table_schema(self, db_name: str, table_name: str) -> List[Dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, COLUMN_KEY, IS_NULLABLE "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t "
                "ORDER BY ORDINAL_POSITION"),
                {"db": db_name, "t": table_name}).fetchall()
        return [{"name": r[0], "type": r[1], "comment": r[2] or "",
                 "primary_key": (r[3] or "").upper() == "PRI",
                 "nullable": (r[4] or "YES").upper() == "YES"} for r in rows]

    def sample_data(self, db_name: str, table_name: str, limit: int = 10) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        # 标识符用反引号包裹 + 白名单校验，防注入
        ref = f"`{self._ident(db_name)}`.`{self._ident(table_name)}`"
        with self.engine.connect() as conn:
            result = conn.execute(text(f"SELECT * FROM {ref} LIMIT {limit}"))
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result.fetchall()]

    # ------------------------------------------------------------ 优化建议元数据
    def table_indexes(self, db_name: str, table_name: str) -> List[Dict[str, Any]]:
        """索引清单（information_schema.STATISTICS，按索引名+序列聚合出复合索引列序）。"""
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t "
                "ORDER BY INDEX_NAME, SEQ_IN_INDEX"),
                {"db": db_name, "t": table_name}).fetchall()
        grouped: Dict[str, Dict[str, Any]] = {}
        for name, non_unique, _seq, col in rows:
            entry = grouped.get(name)
            if entry is None:
                entry = {"name": name, "columns": [],
                         "unique": not int(non_unique or 0),
                         "primary": (name or "").upper() == "PRIMARY"}
                grouped[name] = entry
            entry["columns"].append(col)
        return list(grouped.values())

    def table_row_estimate(self, db_name: str, table_name: str) -> Optional[int]:
        """行数估计（InnoDB 的 TABLE_ROWS 是估算值，够用来判断"要不要管索引"）。"""
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t"),
                {"db": db_name, "t": table_name}).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------ 只读查询
    def simple_query(self, sql: str, timeout: int, max_rows: int) -> Dict[str, Any]:
        """执行只读查询（调用方已做安全校验，此处二次防御见 QueryExecutor）。"""
        max_rows = max(1, min(int(max_rows), 5000))
        t0 = time.perf_counter()
        with self.engine.connect() as conn:
            # 会话级超时（毫秒）；MySQL 5.7+ 支持，失败不影响查询本身
            try:
                conn.execute(text(f"SET SESSION max_execution_time = {int(timeout) * 1000}"))
            except SQLAlchemyError:
                pass
            result = conn.execute(text(sql))
            cols = list(result.keys())
            # 多取一行判断是否触顶（截断提示）
            fetched = result.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [dict(zip(cols, row)) for row in fetched[:max_rows]]
        return {"columns": cols, "rows": rows, "row_count": len(rows),
                "cost_ms": int((time.perf_counter() - t0) * 1000),
                "truncated": truncated}

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _ident(name: str) -> str:
        """标识符白名单：字母/数字/下划线/中文，其余直接拒（反引号内也不信任输入）。"""
        import re
        if not re.fullmatch(r"[A-Za-z0-9_$\u4e00-\u9fff]+", name or ""):
            raise ValueError(f"非法标识符: {name!r}")
        return name


__all__ = ["MysqlAdapter"]
