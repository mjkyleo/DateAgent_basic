"""写操作安检：把一条 SQL 变成「可展示的写入计划」，不通过就不给执行（P0）。

定位：只读通道走 security/sql_security.py（SELECT-only + 强制 LIMIT），本模块是写通道，
两者互不覆盖 —— SELECT 不该在这里被放行，写语句也不该在那里被放行。

为什么返回一个计划而不是一个布尔：写操作的影响面必须让人**看见**才能确认。
调用方拿到的是语句类型 / 目标表 / 预估行数的取数语句 / 备份表名 / 需要手输的确认词，
前端把这些摊开，人点头之后才轮到执行器动手。

四层权限里本模块负责的是"语句本身配不配"这一层：

    总闸(AGENT_ALLOW_WRITES) ∧ 数据源上限(datasources.allowed_ops)
      ∧ 本次请求档位 ∧ 人工确认位

前三层在这里判（总闸缺失时任何写语句一律拒），第四层由接口层的 confirmed 负责。

硬编码的边界（不随档位放开，也不由环境变量决定）：
- 单语句：分号拼多条永远拒；
- 导出文件：`INTO OUTFILE` / `INTO DUMPFILE` 永远拒（sqlglot 的 mysql 方言解析不了它，
  所以这条在解析之前按文本拦，免得给人一句"语法错误"当理由）；
- 系统库与元数据库：information_schema / mysql / performance_schema / sys 以及
  本项目的元数据库（默认 data_agent）不可写。业务库与元数据库常是同一台 MySQL、
  同一个账号（凭据复用），不挡掉就等于允许 Agent 删自己的会话表/任务表；
- 跨库写：只允许写当前选中的库，`UPDATE other_db.t` 一律拒；
- 多表写：P0 只支持单表 UPDATE/DELETE，带 JOIN 的写出备份与预估都不准，直接拒；
- UPDATE/DELETE 必须带 WHERE：想清空请显式用 TRUNCATE（走 DDL 档 + 输入表名确认）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from secrets import token_hex
from typing import Any, Callable, Dict, List, Optional

import sqlglot
from sqlglot import exp

from ..config.settings import Settings

#: 档位高低：请求档位只能等于或低于数据源上限，不能自己抬。
TIER_RANK = {"read": 0, "write_dml": 1, "write_ddl": 2}

DML_KINDS = {"insert", "update", "delete"}
DDL_KINDS = {"create", "alter", "drop", "truncate"}

#: 表名要拿去拼备份表名与限定名，先卡死字符集与长度（标识符不进参数绑定，只能白名单）
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_IDENT_LEN = 64

DEFAULT_QUALIFY = lambda db, t: f"`{db}`.`{t}`"  # noqa: E731  兜底限定名（无适配器时）


def tier_rank(tier: Optional[str]) -> int:
    return TIER_RANK.get((tier or "read").strip().lower(), 0)


@dataclass
class WritePlan:
    """一条写语句的安检结论。passed=False 时 reason 说明拦在哪一条规则。"""

    sql: str
    kind: str = "unknown"
    targets: List[Dict[str, Any]] = field(default_factory=list)
    #: 不可逆操作（DROP / TRUNCATE / ALTER DROP COLUMN）：需要手输表名确认 + 整表备份
    destructive: bool = False
    #: 需要人原样输入的确认词（破坏性操作 = 目标表名）；空串表示勾选确认即可
    confirm_phrase: str = ""
    #: 备份表名与建表语句（None = 这个语句不需要/没法备份）
    backup_table: Optional[str] = None
    backup_sql: Optional[str] = None
    #: 预估影响行数的 SELECT（执行器拿去跑，超 write_max_affected_rows 就拒）
    estimate_sql: Optional[str] = None
    #: 是否需要在执行后回捞实际影响行数
    needs_affected_rows: bool = False
    reason: Optional[str] = None
    #: 供前端展示的语句类型中文名片段
    kind_label: str = ""

    @property
    def passed(self) -> bool:
        return self.reason is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed, "reason": self.reason,
            "statement_kind": self.kind, "statement_label": self.kind_label,
            "targets": self.targets, "destructive": self.destructive,
            "confirm_phrase": self.confirm_phrase,
            "backup_table": self.backup_table, "backup_sql": self.backup_sql,
            "estimate_sql": self.estimate_sql,
            "needs_affected_rows": self.needs_affected_rows,
            "final_sql": self.sql if self.passed else None,
        }


_KIND_LABEL = {
    "select": "只读查询", "insert": "插入 INSERT", "update": "更新 UPDATE",
    "delete": "删除 DELETE", "create": "建表/建索引 CREATE", "alter": "改结构 ALTER",
    "drop": "删对象 DROP", "truncate": "清空表 TRUNCATE",
    "unknown": "无法识别的语句",
}


def classify_kind(sql: str) -> str:
    """语句根节点分类。解析失败返回 unknown，由调用方拒。"""
    try:
        trees = sqlglot.parse((sql or "").strip().rstrip(";").strip(), read="mysql")
    except Exception:  # noqa: BLE001  语法错误原文可能含片段，不外泄
        return "unknown"
    if len(trees) != 1 or trees[0] is None:
        return "unknown"
    name = type(trees[0]).__name__
    return {"Insert": "insert", "Update": "update", "Delete": "delete",
            "Create": "create", "Alter": "alter", "Drop": "drop",
            "TruncateTable": "truncate", "Select": "select",
            "Union": "select"}.get(name, "unknown")


def plan_write_sql(sql: str, *, db: str, tier: str = "read",
                   settings: Settings,
                   qualify: Optional[Callable[[str, str], str]] = None) -> WritePlan:
    """把写语句过一遍规则，返回执行计划。

    db     当前选中的库（作用域的唯一参照）；
    tier   本次请求档位；
    qualify 限定名拼法（来自适配器 qualified_name，保证和元数据查询同一套反引号规则）。
    """
    q = qualify or DEFAULT_QUALIFY
    raw = (sql or "").strip().rstrip(";").strip()
    plan = WritePlan(sql=raw)
    if not raw:
        plan.reason = "SQL 为空"
        return plan

    # ① 总闸：没显式打开环境变量，任何写语句都不该执行（代码进了公开仓库 ≠ 线上能写）
    if not settings.allow_writes:
        plan.reason = ("写操作总闸未打开（环境变量 AGENT_ALLOW_WRITES=true 才能开），"
                       "当前只允许只读查询")
        return plan
    if not db:
        plan.reason = "未指定目标库，无法判定写入作用域"
        return plan
    if db.strip().lower() in settings.protected_databases:
        plan.reason = f"库 {db} 是系统库或本项目的元数据库，禁止写入"
        return plan

    # ② 导出到服务器文件：`INTO OUTFILE` 在 sqlglot 的 mysql 方言里根本解析不动，
    #    等到解析后再判只能拿到一句"语法错误"，人看不出真正的风险在哪 —— 先按文本拦。
    #    （串匹配只认 `INTO OUTFILE/DUMPFILE` 这个固定搭配，不会误伤名叫 outfile_x 的表）
    lowered = raw.lower()
    for bad in ("into outfile", "into dumpfile"):
        if bad in lowered:
            plan.reason = f"禁止将数据写入服务器文件（{bad.upper()}）"
            return plan

    # ③ 单语句：sqlglot 解析出多条即拒（挡住 `;` 拼接）
    try:
        trees = sqlglot.parse(raw, read="mysql")
    except Exception as e:  # noqa: BLE001
        plan.reason = f"SQL 语法解析失败：{type(e).__name__}"
        return plan
    if len(trees) != 1 or trees[0] is None:
        plan.reason = f"只允许单条语句（解析出 {len(trees)} 条）"
        return plan
    expr = trees[0]

    # ④ 语句类型：认不出来的、SELECT 的，都不属于写通道
    plan.kind = classify_kind(raw)
    plan.kind_label = _KIND_LABEL.get(plan.kind, plan.kind)
    if plan.kind == "unknown":
        plan.reason = "无法识别的语句类型，一律不执行"
        return plan
    if plan.kind == "select":
        plan.reason = "只读查询请走查询通道（本接口只处理写语句）"
        return plan

    # ⑤ 档位：DML 要 write_dml 以上，DDL 要 write_ddl
    need = 1 if plan.kind in DML_KINDS else 2
    if tier_rank(tier) < need:
        want = "读写（write_dml）" if need == 1 else "结构变更（write_ddl）"
        plan.reason = (f"当前档位是 {tier or 'read'}，执行「{plan.kind_label}」"
                       f"需要 {want} 档位")
        return plan

    # ⑥ 函数黑名单：写语句里的 SELECT 子句同样能读服务器文件
    for node in expr.walk():
        # 顺序要紧：sqlglot 把不认识的函数（load_file 等）落成 Anonymous，
        # 而 Anonymous 是 Func 的子类，sql_name() 只会回 "ANONYMOUS"，
        # 单看 sql_name() 就漏掉了黑名单 —— 必须再按 Anonymous.name 判一次。
        if isinstance(node, exp.Anonymous):
            fn = (node.name or "").lower()
        elif isinstance(node, exp.Func):
            fn = (node.sql_name() or "").lower()
        else:
            continue
        if fn in settings.forbidden_functions:
            plan.reason = f"禁止函数：{fn}()"
            return plan

    # ⑦ 目标表：根节点的主目标 + 全树扫描出的每张表都要落在同一个库
    main = _main_target(expr)
    if main is None:
        plan.reason = "没能从语句里确定写入目标表"
        return plan
    tables = [(t.db or None, t.name) for t in expr.find_all(exp.Table)]
    tables = [(d, n) for d, n in tables if n]
    if not tables:
        plan.reason = "语句里没有可识别的表"
        return plan
    scoped: List[Dict[str, Any]] = []
    for d, n in tables:
        if d and d.strip().lower() in settings.protected_databases:
            plan.reason = f"禁止写入系统库或元数据库：{d}"
            return plan
        if d and d.strip().lower() != db.strip().lower():
            plan.reason = f"只允许写当前选中的库 {db}，语句涉及 {d}"
            return plan
        if not _IDENT_RE.match(n or "") or len(n) > _MAX_IDENT_LEN:
            plan.reason = f"表名不符合可执行的安全字符集：{n[:32]}"
            return plan
        scoped.append({"database": d or db, "table": n,
                       "is_main": (d or "") == (main[0] or "") and n == main[1]})
    plan.targets = scoped
    main_ref = q(main[0] or db, main[1])
    # 表别名要搬进重写的 SELECT：`DELETE FROM t a WHERE a.id=1` 的预估与备份语句
    # 如果不带 AS a，执行时是"unknown column a.id"，报错看起来像安检器坏了。
    alias = (main[2] or "").strip()
    if alias and not _IDENT_RE.match(alias):
        plan.reason = f"表别名不符合可执行的安全字符集：{alias[:32]}"
        return plan
    main_from = f"{main_ref} AS `{alias}`" if alias else main_ref

    # ⑧ 多表写：P0 只支持单表 UPDATE/DELETE（备份与行数预估只在单表上准确）
    if plan.kind in ("update", "delete"):
        # sqlglot 对无 USING 的 DELETE 给的是 args["using"]=False（不是 None），
        # 判 is not None 会把普通的单表 DELETE 误当成多表写。
        if expr.args.get("using") or _has_join(expr):
            plan.reason = "暂不支持多表 JOIN 的 UPDATE/DELETE，请拆成单表语句"
            return plan
        if len({t["table"] for t in scoped}) > 1:
            plan.reason = "暂不支持一次写多张表，请拆成单表语句"
            return plan

    # ⑨ WHERE：UPDATE/DELETE 无 WHERE 一律拒（清空表请显式 TRUNCATE，那条路有整表备份）
    where = expr.args.get("where")
    if plan.kind in ("update", "delete") and where is None:
        plan.reason = ("UPDATE/DELETE 必须带 WHERE，避免整表误伤；"
                       "确实要清空请显式用 TRUNCATE")
        return plan

    where_sql = where.sql(dialect="mysql") if where is not None else ""
    plan.needs_affected_rows = plan.kind in DML_KINDS
    plan.destructive = (plan.kind in ("drop", "truncate")
                        or (plan.kind == "alter" and _alters_drop_column(expr)))

    # ⑩ 预估与备份
    if plan.kind in ("update", "delete", "insert"):
        cond = f" {where_sql}" if where_sql else ""
        if plan.kind in ("update", "delete"):
            plan.estimate_sql = f"SELECT COUNT(*) FROM {main_from}{cond}"
        else:
            plan.estimate_sql = _insert_estimate(expr, main_ref, q, db)
        if settings.write_auto_backup and plan.kind in ("update", "delete"):
            plan.backup_table = _backup_name(main[1], settings.write_backup_prefix)
            plan.backup_sql = (f"CREATE TABLE {q(db, plan.backup_table)} AS "
                               f"SELECT * FROM {main_from}{cond}")
    elif plan.kind in ("drop", "truncate") or (plan.kind == "alter" and plan.destructive):
        # ALTER ... DROP COLUMN 会连带列上全部数据一起没了，和清空同级：
        # 同样整表备份 + 输入表名确认，否则这一条就没有后悔药了。
        plan.estimate_sql = f"SELECT COUNT(*) FROM {main_ref}"
        if settings.write_auto_backup:
            plan.backup_table = _backup_name(main[1], settings.write_backup_prefix)
            plan.backup_sql = (f"CREATE TABLE {q(db, plan.backup_table)} AS "
                               f"SELECT * FROM {main_ref}")

    # ⑪ 破坏性：要人原样输入表名
    if plan.destructive:
        plan.confirm_phrase = main[1]
        if not plan.backup_sql:
            plan.reason = ("破坏性操作必须先行备份，但自动备份已关闭"
                           "（WRITE_AUTO_BACKUP=false），拒绝执行")
            return plan
    return plan


def _main_target(expr: exp.Expression) -> Optional[tuple]:
    """写操作的主目标表：根节点 this 上的那张表（不是全树扫出来的任意表）。

    返回 (库, 表, 别名)；别名可能为空串 —— DELETE/UPDATE 允许给表起别名，
    WHERE 里就用它限定列，重写的预估与备份语句必须把别名一起带上。
    """
    node = expr.args.get("this")
    if isinstance(node, exp.Table):
        return (node.db or None, node.name, node.alias)
    if isinstance(node, exp.Schema):           # INSERT INTO t(a,b) / CREATE TABLE t(...)
        inner = node.this
        if isinstance(inner, exp.Table):
            return (inner.db or None, inner.name, inner.alias)
    if isinstance(node, exp.Anonymous):        # DROP/TRUNCATE 的少数形态
        return (None, node.name, "")
    for t in expr.find_all(exp.Table):         # 兜底：第一张表
        return (t.db or None, t.name, t.alias)
    return None


def _has_join(expr: exp.Expression) -> bool:
    return next(expr.find_all(exp.Join), None) is not None


def _alters_drop_column(expr: exp.Expression) -> bool:
    """ALTER ... DROP COLUMN 视为破坏性（列上的数据没了）。"""
    for action in (expr.args.get("actions") or []):
        if isinstance(action, exp.Drop) or "DROP" in action.sql(dialect="mysql").upper():
            return True
    return False


def _insert_estimate(expr: exp.Expression, main_ref: str,
                     qualify: Callable[[str, str], str], db: str) -> Optional[str]:
    """INSERT ... SELECT 的量能从子查询估；VALUES 的直接数行数。"""
    rows = expr.find(exp.Values)
    if rows is not None:
        n = len([t for t in rows.expressions if isinstance(t, exp.Tuple)]) or None
        return f"SELECT {int(n)} AS estimated" if n else None
    sel = expr.expression if isinstance(expr.expression, exp.Select) else None
    if sel is None:
        sel = next(iter([s for s in expr.find_all(exp.Select)
                         if s is not expr]), None)
    if sel is None:
        return None
    inner = sel.sql(dialect="mysql")
    return f"SELECT COUNT(*) FROM ({inner}) AS _src"


def _backup_name(table: str, prefix: str) -> str:
    """备份表名：<前缀>_<表名>_<UTC 秒>_<6位随机>。

    随机后缀不是讲究好看：同一张表连着改两次（很常见：先修数据再补条件），
    落在同一秒里就会算出同一个名字，第二条 CREATE TABLE 直接撞车。
    超 64 字符时砍表名、保留时间戳与随机位 —— 唯一性不能被截掉。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{stamp}_{token_hex(3)}"
    safe_prefix = re.sub(r"[^A-Za-z0-9_]", "", prefix or "_bak")[:12] or "_bak"
    head = f"{safe_prefix}_{table}"
    budget = _MAX_IDENT_LEN - len(suffix)
    return (head[:max(1, budget)] + suffix) if len(head) > budget else head + suffix


__all__ = ["WritePlan", "plan_write_sql", "classify_kind", "tier_rank",
           "TIER_RANK", "DML_KINDS", "DDL_KINDS"]
