"""查询执行沙箱（设计文档 §9.5）。

双重防御：即使上层漏校验，执行前也重新过一遍安全门；
超时（MySQL 会话级 max_execution_time）+ 行数封顶 + 异常脱敏。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..config.settings import Settings
from ..core.data_source.base import build_adapter
from ..models.pydantic_schemas import QueryResult
from ..security.sql_security import SqlSecurityChecker
from ..utils.helpers import sanitize_error


class QueryExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._checker = SqlSecurityChecker(settings)

    def execute(self, *, ds: Dict[str, Any], sql: str,
                max_rows: Optional[int] = None) -> QueryResult:
        """执行只读查询。ds 为数据源配置 dict（含连接参数）。"""
        # ① 二次安全校验（双重防御）
        result = self._checker.check(sql, max_limit=self.settings.default_limit)
        if not result["passed"]:
            raise PermissionError(f"安全校验未通过：{result['reason']}")
        safe_sql = result["final_sql"] or sql

        # ② 经适配器执行（超时/行数在适配器内落实）
        adapter = build_adapter(ds)
        try:
            out = adapter.simple_query(safe_sql, timeout=self.settings.query_timeout_s,
                                       max_rows=max_rows or self.settings.default_limit)
        except Exception as e:  # noqa: BLE001  脱敏后抛友好错误
            raise RuntimeError(f"查询执行失败：{sanitize_error(str(e))}") from e
        finally:
            adapter.close()

        return QueryResult(columns=out["columns"], rows=out["rows"],
                           row_count=out["row_count"], cost_ms=out["cost_ms"],
                           truncated=bool(out.get("truncated")))


__all__ = ["QueryExecutor"]
