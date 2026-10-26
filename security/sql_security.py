"""SQL 安全过滤器：sqlglot AST 静态检查 + 语句黑名单 + LIMIT 强制（设计文档 §9.4/§10.2）。

防线定位：这是 SQL 的**第一道**安检（生成时）；查询执行沙箱内部还会二次调用
本模块（双重防御，避免上层绕过）。规则全部确定性，不依赖 LLM 判断：

1. 单语句：sqlglot parse 出多条即拒（拦截 `;` 分割多语句）；
2. 根节点仅允许 SELECT（Union 的叶子必须全是 SELECT）；
3. 全树扫描禁写节点（DML/DDL/权限/USE/SET 等，settings.forbidden_statements）；
4. MySQL 专项：拦截 INTO OUTFILE/DUMPFILE（文件外写）、load_file 等函数黑名单；
5. 强制 LIMIT：无 LIMIT 追加默认上限；显式 LIMIT 超上限则封顶（不拒绝，保证可用）。
"""
from __future__ import annotations

from typing import Any, Dict

import sqlglot
from sqlglot import exp

from ..config.settings import Settings


class SqlSecurityChecker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(self, sql: str, *, max_limit: int | None = None) -> Dict[str, Any]:
        """校验一条 SQL。返回 {"passed": bool, "reason": str|None, "final_sql": str|None}。

        final_sql 为追加/封顶 LIMIT 后的可执行版本（仅 passed=True 时非空）。
        """
        limit = max_limit or self.settings.default_limit
        raw = (sql or "").strip().rstrip(";").strip()
        if not raw:
            return {"passed": False, "reason": "SQL 为空", "final_sql": None}

        # 文本级预检：MySQL 文件外写语法（sqlglot 可能不解析为独立节点，先按原文拦）
        lowered = raw.lower()
        for bad in ("into outfile", "into dumpfile"):
            if bad in lowered:
                return {"passed": False,
                        "reason": f"禁止将查询结果写入服务器文件（{bad.upper()}）",
                        "final_sql": None}

        try:
            trees = sqlglot.parse(raw, read="mysql")
        except Exception as e:  # noqa: BLE001  语法错误信息可能含片段，只回类型
            return {"passed": False, "reason": f"SQL 语法解析失败：{type(e).__name__}",
                    "final_sql": None}

        if len(trees) != 1 or trees[0] is None:
            return {"passed": False, "reason": "只允许单条 SQL 语句（检测到多条）",
                    "final_sql": None}
        expr = trees[0]

        # ① 根节点：SELECT 或纯 SELECT 组成的 UNION
        if not self._is_read_root(expr):
            return {"passed": False,
                    "reason": f"仅允许 SELECT 只读查询（根语句为 {type(expr).__name__}）",
                    "final_sql": None}

        # ② 全树扫描：禁写节点 + 危险函数
        for node in expr.walk():
            n = type(node).__name__
            if n in self.settings.forbidden_statements:
                return {"passed": False, "reason": f"禁止语句类型：{n}", "final_sql": None}
            # SELECT ... INTO @var / INTO OUTFILE：把结果落到会话变量或文件，
            # 不在 forbidden_statements 里（它是 Select 的一个子句），单独拦。
            if isinstance(node, exp.Into):
                return {"passed": False,
                        "reason": "禁止 SELECT INTO（不允许把结果写入变量或文件）",
                        "final_sql": None}
            if isinstance(node, exp.Func):
                fn = node.sql_name().lower()
                if fn in self.settings.forbidden_functions:
                    return {"passed": False, "reason": f"禁止函数：{fn}()", "final_sql": None}
            if isinstance(node, exp.Anonymous):
                fn = node.name.lower()
                if fn in self.settings.forbidden_functions:
                    return {"passed": False, "reason": f"禁止函数：{fn}()", "final_sql": None}

        # ③ LIMIT 强制：无则追加，超上限则封顶
        final_sql = self._enforce_limit(expr, limit)
        return {"passed": True, "reason": None, "final_sql": final_sql}

    @staticmethod
    def _is_read_root(expr: exp.Expression) -> bool:
        if isinstance(expr, exp.Select):
            return True
        if isinstance(expr, exp.Union):
            # Union 的左右子句在 args['this'] / args['expression']（selects 是投影列）
            left, right = expr.args.get("this"), expr.args.get("expression")
            return all(SqlSecurityChecker._is_read_root(s)
                       for s in (left, right) if s is not None)
        return False

    @staticmethod
    def _enforce_limit(expr: exp.Expression, limit: int) -> str:
        has_limit = expr.args.get("limit") is not None
        if has_limit:
            # 显式 LIMIT 超上限 → 封顶（保留可用性，防全表拉取）
            try:
                lim = expr.args["limit"]
                val = lim.expression.name if hasattr(lim, "expression") else ""
                if val.isdigit() and int(val) > limit:
                    expr = expr.limit(limit)
            except Exception:  # noqa: BLE001  非常携 LIMIT 表达式保持原样
                pass
            return expr.sql(dialect="mysql")
        return expr.limit(limit).sql(dialect="mysql")


def check_sql_safe(sql: str, *, max_limit: int = 1000,
                   settings: Settings | None = None) -> Dict[str, Any]:
    """模块级便捷入口（与类方法等价）。"""
    return SqlSecurityChecker(settings or Settings()).check(sql, max_limit=max_limit)


__all__ = ["SqlSecurityChecker", "check_sql_safe"]
