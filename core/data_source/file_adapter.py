"""文件数据源适配器：一个 SQLite 文件 = 一个数据源（设计文档 §9.1）。

由 file_import 模块把 CSV/TSV/JSON/XLSX 解析落库，本适配器只负责"对着这个文件"
提供与 MySQL 完全相同的接口。只读三重落实：
1. 连接以 URI 只读模式打开（mode=ro），从根上杜绝写操作；
2. simple_query 的 SQL 已由上层安全门校验（SELECT-only）；
3. 行数封顶 + 多取一行判断截断，与 MySQL 适配器一致。

注意：SQLite 是单库，`db.table` 形式不存在——所以覆盖 qualified_name() 只返回表名，
MetaTool 拼接引用时经此方法适配（MySQL 走默认的 `db`.`table`）。
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from .base import DataSourceAdapter

_IDENT_RE = re.compile(r"[A-Za-z0-9_$\u4e00-\u9fff]+")


class FileAdapter(DataSourceAdapter):
    # SQLite 是单库：SQL 里只能写 `表名`，写 `库`.`表` 会被当成不存在的表
    qualifies_tables = False

    def __init__(self, file_path: str, db_name: str = "") -> None:
        p = Path(file_path or "")
        if not file_path or not p.exists():
            raise ValueError(f"文件数据源不存在或已被删除：{p.name or '(空路径)'}")
        self.path = str(p.resolve())
        self.db_name = db_name or p.stem
        # 只读打开：mode=ro 由 SQLite 在引擎层强制，误写直接报错
        def _creator():
            return sqlite3.connect(f"file:{quote(self.path)}?mode=ro", uri=True)
        self.engine = create_engine("sqlite://", creator=_creator, poolclass=StaticPool)

    # ------------------------------------------------------------ 连通性
    def test_connection(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1 FROM sqlite_master LIMIT 1"))
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------ 元数据
    def list_databases(self) -> List[str]:
        return [self.db_name]

    def list_tables(self, db_name: str) -> List[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")).fetchall()
        return [r[0] for r in rows]

    def get_table_schema(self, db_name: str, table_name: str) -> List[Dict[str, Any]]:
        self._ident(table_name)
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(f'PRAGMA table_info("{table_name}")')).fetchall()
        return [{"name": r[1], "type": r[2] or "TEXT", "comment": "",
                 "primary_key": bool(r[5]), "nullable": not r[3]} for r in rows]

    def sample_data(self, db_name: str, table_name: str, limit: int = 10) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        ref = self.qualified_name(db_name, table_name)
        with self.engine.connect() as conn:
            result = conn.execute(text(f"SELECT * FROM {ref} LIMIT {limit}"))
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result.fetchall()]

    # ------------------------------------------------------------ 优化建议元数据
    def table_indexes(self, db_name: str, table_name: str) -> List[Dict[str, Any]]:
        """SQLite 索引（PRAGMA index_list + index_info）。

        PRAGMA 不支持绑定参数，表名/索引名都必须过 _ident 白名单后再拼。
        """
        self._ident(table_name)
        out: List[Dict[str, Any]] = []
        with self.engine.connect() as conn:
            for r in conn.execute(text(f'PRAGMA index_list("{table_name}")')).fetchall():
                # r: (seq, name, unique, origin, partial)
                name, unique, origin = r[1], bool(r[2]), (r[3] or "")
                cols = [row[2] for row in conn.execute(
                    text(f'PRAGMA index_info("{self._ident(name)}")')).fetchall()]
                out.append({"name": name, "columns": cols, "unique": unique,
                            "primary": origin == "pk"})
        return out

    def table_row_estimate(self, db_name: str, table_name: str) -> Optional[int]:
        """文件源没有估算值可言，直接 COUNT(*)（导入文件已封顶 20 万行，扫得起）。"""
        ref = self.qualified_name(db_name, table_name)
        with self.engine.connect() as conn:
            row = conn.execute(text(f"SELECT COUNT(*) FROM {ref}")).fetchone()
        return int(row[0]) if row else None

    # ------------------------------------------------------------ 只读查询
    def simple_query(self, sql: str, timeout: int, max_rows: int) -> Dict[str, Any]:
        """SQLite 无会话级超时；安全门已限 SELECT + LIMIT，行数封顶兜底。"""
        max_rows = max(1, min(int(max_rows), 5000))
        t0 = time.perf_counter()
        with self.engine.connect() as conn:
            result = conn.execute(text(sql))
            cols = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [dict(zip(cols, row)) for row in fetched[:max_rows]]
        return {"columns": cols, "rows": rows, "row_count": len(rows),
                "cost_ms": int((time.perf_counter() - t0) * 1000),
                "truncated": truncated}

    # ------------------------------------------------------------ 单库来源的引用方式
    def qualified_name(self, db_name: str, table_name: str) -> str:
        # 反引号而非双引号：SQLite 兼容反引号；双引号在标识符不存在时会被当字符串字面量
        return f"`{self._ident(table_name)}`"

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _ident(name: str) -> str:
        if not _IDENT_RE.fullmatch(name or ""):
            raise ValueError(f"非法标识符: {name!r}")
        return name


__all__ = ["FileAdapter"]
