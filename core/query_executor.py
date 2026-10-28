"""查询执行沙箱（设计文档 §9.5）。

两条通道，互不共用安全门：
- execute()      只读：即使上层漏校验，执行前也重新过一遍 SELECT-only 门；
                 超时（MySQL 会话级 max_execution_time）+ 行数封顶 + 异常脱敏。
- prepare_write()/execute_write()  写入：过 write_security 出计划，
                 先估影响行数、再自动备份受影响行，然后才落刀；失败一律回滚（DML）。

写通道的每一层都可能单独救回一次误操作，所以顺序是硬约束：
档位/上限判 → 影响面预估 → 备份 → 执行。备份失败就中止，绝不"先删了再说"。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..config.settings import Settings
from ..core.data_source.base import build_adapter
from ..models.pydantic_schemas import QueryResult
from ..security.sql_security import SqlSecurityChecker
from ..security.write_security import plan_write_sql, tier_rank
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

    # ------------------------------------------------------------ 写通道
    def prepare_write(self, *, ds: Dict[str, Any], db: str, sql: str,
                      tier: str = "read") -> Dict[str, Any]:
        """只出计划与影响面预估，一个字节都不改。前端确认卡片用的就是这份。"""
        eff_tier = self._resolve_tier(ds, tier)
        adapter = build_adapter(ds)
        try:
            plan = plan_write_sql(sql, db=db, tier=eff_tier,
                                  settings=self.settings,
                                  qualify=adapter.qualified_name)
            if not plan.passed:
                raise PermissionError(f"写入安检未通过：{plan.reason}")
            estimated = self._estimate(adapter, plan)
        finally:
            adapter.close()
        return {**plan.to_dict(), "estimated_rows": estimated,
                "requested_tier": (tier or "read"), "effective_tier": eff_tier,
                "max_allowed_rows": self.settings.write_max_affected_rows}

    def execute_write(self, *, ds: Dict[str, Any], db: str, sql: str,
                      tier: str = "read", confirmed: bool = False,
                      confirm_phrase: str = "") -> Dict[str, Any]:
        """按「判档位 → 估影响 → 备份 → 执行」的顺序落刀。

        预估在这里重跑一次：从前端确认到真正执行之间可能已经过了一会儿，
        数据变了就得按新的影响面重新判上限，不能拿旧数字放行。
        """
        if not confirmed:
            raise PermissionError("写操作必须带 confirmed=true（双层确认不可绕过）")
        eff_tier = self._resolve_tier(ds, tier)
        adapter = build_adapter(ds)
        try:
            plan = plan_write_sql(sql, db=db, tier=eff_tier,
                                 settings=self.settings,
                                 qualify=adapter.qualified_name)
            if not plan.passed:
                raise PermissionError(f"写入安检未通过：{plan.reason}")
            # 破坏性操作要人原样输入表名，勾选框不足以承担"我知道这表没了"
            if plan.confirm_phrase and confirm_phrase.strip() != plan.confirm_phrase:
                raise PermissionError(
                    f"破坏性操作需要手动输入表名确认（应输入 {plan.confirm_phrase}）")

            estimated = self._estimate(adapter, plan)
            cap = self.settings.write_max_affected_rows
            if estimated is not None and estimated > cap:
                raise PermissionError(
                    f"预计影响 {estimated} 行，超过允许上限 {cap} 行，已拒绝；"
                    f"请缩小 WHERE 范围或调高 WRITE_MAX_AFFECTED_ROWS")

            # 备份失败 = 中止。宁可写不进去，也不能出现"改了但没后悔药"
            if plan.backup_sql:
                try:
                    adapter.execute_write(plan.backup_sql,
                                          timeout=self.settings.query_timeout_s)
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(
                        f"备份表创建失败，写入已中止：{sanitize_error(str(e))}") from e

            out = adapter.execute_write(plan.sql, timeout=self.settings.query_timeout_s)
            affected = int(out.get("affected_rows") or 0)
            return {"statement_kind": plan.kind,
                    "statement_label": plan.kind_label,
                    "targets": plan.targets,
                    "destructive": plan.destructive,
                    "estimated_rows": estimated,
                    "affected_rows": affected,
                    "cost_ms": int(out.get("cost_ms") or 0),
                    "backup_table": plan.backup_table,
                    "backup_rows": affected if plan.kind == "delete" else None,
                    "rollback_sql": self._rollback_sql(plan, db, adapter),
                    "final_sql": plan.sql,
                    # 审计要记"实际按哪个档位执行的"：请求档位被源上限压过之后，
                    # 只看请求值会把一次降级执行记成一次全权执行。
                    "effective_tier": eff_tier}
        except PermissionError:
            raise
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001  引擎错误脱敏后上抛
            raise RuntimeError(f"写操作执行失败：{sanitize_error(str(e))}") from e
        finally:
            adapter.close()

    def _resolve_tier(self, ds: Dict[str, Any], tier: str) -> str:
        """请求档位与数据源上限取低的；源没被标可写就直接回到 read。"""
        ceiling = (ds.get("allowed_ops") or "read").strip().lower()
        if not self.settings.allow_writes:
            return "read"
        if tier_rank(ceiling) <= 0:
            raise PermissionError(
                f"数据源「{ds.get('name') or ds.get('id')}」是只读源；"
                "写权限需要在数据源里显式设为可写（源级上限优先于聊天框的档位）")
        # 档位只能被源上限压下去，不能自己抬上去
        return tier if tier_rank(tier) <= tier_rank(ceiling) else ceiling

    def _estimate(self, adapter: Any, plan: Any) -> Optional[int]:
        """跑预估语句取一个标量。

        取不到数字就中止写入，而不是"放行但少个数字"：影响面是这次确认的唯一依据，
        预估失败还继续执行等于让人在盲签。
        """
        if not plan.estimate_sql:
            return None
        try:
            out = adapter.simple_query(plan.estimate_sql,
                                       timeout=self.settings.query_timeout_s,
                                       max_rows=1)
        except Exception as e:  # noqa: BLE001  预估失败 = 这条语句本身就跑不通
            raise PermissionError(
                f"影响行数预估失败，写入已中止：{sanitize_error(str(e))}") from e
        rows = out.get("rows") or []
        if not rows:
            raise PermissionError("影响行数预估无结果，写入已中止")
        first = list(rows[0].values())[0]
        try:
            return int(first)
        except (TypeError, ValueError):
            raise PermissionError("影响行数预估结果不是数字，写入已中止") from None

    @staticmethod
    def _rollback_sql(plan: Any, db: str, adapter: Any) -> Optional[str]:
        """回滚指引（给人看的 SQL 文本，系统不代跑）。

        为什么是"指引"而不是"一键回滚"：UPDATE 的还原要求表有主键才能按行匹配，
        DROP 的还原还要重建结构。把这些交给自动执行，等于再造一个更危险的写通道。
        """
        if not plan.backup_table or not plan.targets:
            return None
        main = next((t for t in plan.targets if t.get("is_main")), plan.targets[0])
        ref = adapter.qualified_name(db, main["table"])
        bak = adapter.qualified_name(db, plan.backup_table)
        if plan.kind == "update":
            # REPLACE INTO 按主键覆盖回去；没主键的表这句不成立，需人工比对备份表
            return f"REPLACE INTO {ref} SELECT * FROM {bak}"
        if plan.kind == "drop":
            return (f"CREATE TABLE {ref} LIKE {bak}; "
                    f"INSERT INTO {ref} SELECT * FROM {bak}")
        if plan.kind == "alter":
            return (f"备份表 {bak} 保留了改结构前的整表数据，"
                    f"请按它人工比对并回填被删的列")
        return f"INSERT INTO {ref} SELECT * FROM {bak}"


__all__ = ["QueryExecutor"]