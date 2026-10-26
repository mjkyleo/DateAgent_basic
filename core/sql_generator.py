"""SQL 生成与安全校验模块（设计文档 §9.4）。

流程：LLM/模板原始 SQL → sqlglot AST 语法解析 → AST 安全校验 → LIMIT 强制 → final_sql。
Schema 约束：生成的 SQL 只能引用 schema_context 中登记的表（防幻觉表）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config.settings import Settings
from ..models.pydantic_schemas import SqlSecurityResult
from ..security.sql_security import SqlSecurityChecker


def generate_sql(llm_raw_sql: str, schema_context: Dict[str, Any], *,
                 max_limit: int = 1000,
                 settings: Optional[Settings] = None) -> SqlSecurityResult:
    """校验并规范化一条 SQL。schema_context 形如 {"tables": ["t_order", ...]}。"""
    st = settings or Settings()
    checker = SqlSecurityChecker(st)
    result = checker.check(llm_raw_sql, max_limit=max_limit)
    if not result["passed"]:
        return SqlSecurityResult(passed=False, reason=result["reason"], final_sql=None)

    final_sql = result["final_sql"] or ""
    # Schema 约束：引用表必须在上下文登记表内（防 LLM 幻觉表名）
    known = _known_tables(schema_context)
    if known:
        used = _referenced_tables(final_sql)
        unknown = [t for t in used if t.lower() not in known]
        if unknown:
            return SqlSecurityResult(
                passed=False,
                reason=f"SQL 引用了 Schema 之外的表：{', '.join(sorted(set(unknown)))}",
                final_sql=None)
    return SqlSecurityResult(passed=True, reason=None, final_sql=final_sql)


def _known_tables(schema_context: Dict[str, Any]) -> set[str]:
    tables = schema_context.get("tables") or []
    return {str(t).lower() for t in tables}


def _referenced_tables(sql: str) -> List[str]:
    import sqlglot
    try:
        trees = sqlglot.parse(sql, read="mysql")
    except Exception:  # noqa: BLE001  安全门已过语法，这里双保险
        return []
    out: List[str] = []
    for tree in trees:
        if tree is None:
            continue
        for node in tree.find_all(sqlglot.exp.Table):
            out.append(node.name)
    return out


__all__ = ["generate_sql"]
