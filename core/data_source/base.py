"""数据源适配器抽象层（设计文档 §9.1）。

统一接口契约：元数据五件套（库/表/结构/采样）+ 只读查询。
MySQL 之外的来源（CSV/Excel）实现同一接口即可接入，本 MVP 不实现。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class DataSourceAdapter(ABC):
    """数据源适配器统一接口。"""

    #: SQL 里是否需要写成 `库`.`表`（MySQL），还是只写 `表`（SQLite 单库）。
    #: SQL 生成器/LLM 提示词都读这个标志 —— 文件源写库限定名会被 SQLite 当成
    #: "no such table: demo.demo"。单库来源必须覆盖为 False。
    qualifies_tables = True

    @abstractmethod
    def test_connection(self) -> bool:
        """测试数据源连通性。"""
        ...

    @abstractmethod
    def list_databases(self) -> List[str]:
        """列出所有数据库名称。"""
        ...

    @abstractmethod
    def list_tables(self, db_name: str) -> List[str]:
        """列出指定数据库下全部数据表。"""
        ...

    @abstractmethod
    def get_table_schema(self, db_name: str, table_name: str) -> List[Dict[str, Any]]:
        """表结构：字段名、类型、注释、是否主键。"""
        ...

    @abstractmethod
    def sample_data(self, db_name: str, table_name: str, limit: int = 10) -> List[Dict[str, Any]]:
        """采样表数据（默认 10 行）。"""
        ...

    @abstractmethod
    def simple_query(self, sql: str, timeout: int, max_rows: int) -> Dict[str, Any]:
        """执行只读查询，返回 {columns, rows, row_count, cost_ms, truncated}。"""
        ...

    # -------------------------------------------------- 优化建议用的元数据
    # 这两个方法有默认实现（返回空），目的是让"拿不到索引信息"的数据源
    # 也能正常跑其余链路；优化建议在没有索引事实时会自动降级为只报 SQL 层面的问题。
    def table_indexes(self, db_name: str, table_name: str) -> List[Dict[str, Any]]:
        """表的索引：[{name, columns:[...], unique: bool, primary: bool}]。

        这是 SQL 优化建议的硬事实来源——"WHERE 的列有没有索引"不能靠猜。
        """
        return []

    def table_row_estimate(self, db_name: str, table_name: str) -> Optional[int]:
        """行数估计。MySQL 走 information_schema.TABLE_ROWS（不扫表），
        拿不到时实现方可退回 COUNT(*)（小表才划算）。"""
        return None

    def qualified_name(self, db_name: str, table_name: str) -> str:
        """SQL 里的表引用。默认 MySQL 风格 `db`.`table`；
        单库来源（SQLite 文件等）覆盖此方法，只返回表名。"""
        import re
        for n in (db_name, table_name):
            if not re.fullmatch(r"[A-Za-z0-9_$\u4e00-\u9fff]+", n or ""):
                raise ValueError(f"非法标识符: {n!r}")
        return f"`{db_name}`.`{table_name}`"

    def close(self) -> None:  # 可选实现
        pass


def build_adapter(ds: Dict[str, Any]) -> DataSourceAdapter:
    """按数据源类型构造适配器（连接参数在内存中组装，不落日志）。"""
    from .mysql_adapter import MysqlAdapter

    ds_type = (ds.get("type") or "mysql").lower()
    if ds_type == "mysql":
        return MysqlAdapter(host=ds.get("host") or "127.0.0.1",
                            port=int(ds.get("port") or 3306),
                            user=ds.get("user") or "",
                            password=ds.get("password") or "",
                            database=ds.get("database") or "")
    if ds_type == "file":
        from .file_adapter import FileAdapter
        return FileAdapter(file_path=ds.get("file_path") or "",
                           db_name=ds.get("database") or "")
    raise ValueError(f"不支持的数据源类型: {ds_type}")


__all__ = ["DataSourceAdapter", "build_adapter"]
