"""写安检（security/write_security.py）的规则测试。

这一层的每条规则都是"少一条就可能出事故"的，所以每条都单独钉住：

- 总闸没开，任何写语句都不该被放行（代码进公开仓库 ≠ 线上能写）；
- 档位不够就拒，且 DROP/TRUNCATE/ALTER 需要最高的 write_ddl；
- 只允许写当前选中的库，系统库与本项目元数据库永远拒 —— 业务库和元数据库
  常常是同一台 MySQL、同一个账号（凭据复用），不挡掉就等于允许 Agent 删自己的会话表；
- UPDATE/DELETE 必须带 WHERE，多表 JOIN 的写不支持；
- 函数黑名单要能拦住 `SET x = load_file(...)`：sqlglot 把未知函数落成 Anonymous，
  而 Anonymous 是 Func 的子类，只按 sql_name() 判会漏（sql_name() 回的是 "ANONYMOUS"）；
- 破坏性操作给出确认词（表名）与整表备份；自动备份被关掉时破坏性操作直接拒。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from basic.config.settings import Settings  # noqa: E402
from basic.security.write_security import (plan_write_sql, classify_kind,  # noqa: E402
                                           tier_rank)

DB = "kaggle_demo"


def _settings(**over) -> Settings:
    s = Settings()
    s.allow_writes = True
    for k, v in over.items():
        setattr(s, k, v)
    return s


S = _settings()


def plan(sql: str, tier: str = "write_dml", *, settings=None, db: str = DB):
    return plan_write_sql(sql, db=db, tier=tier, settings=settings or S)


# ------------------------------------------------------------------ 总闸
def test_master_gate_closed_rejects_every_write():
    off = _settings()
    off.allow_writes = False
    for sql in ("DELETE FROM t WHERE id=1", "UPDATE t SET a=1 WHERE id=1",
                "INSERT INTO t(a) VALUES(1)", "DROP TABLE t"):
        p = plan(sql, "write_ddl", settings=off)
        assert not p.passed and "AGENT_ALLOW_WRITES" in p.reason, sql


def test_no_target_db_rejected():
    assert not plan("DELETE FROM t WHERE id=1", db="").passed


# ------------------------------------------------------------------ 档位
@pytest.mark.parametrize("sql,kind", [
    ("INSERT INTO t(a) VALUES(1)", "insert"),
    ("UPDATE t SET a=1 WHERE id=1", "update"),
    ("DELETE FROM t WHERE id=1", "delete"),
])
def test_read_tier_rejects_all_writes(sql, kind):
    """反向验证的正面：把档位调回 read，写语句必须一条条被拒。"""
    p = plan(sql, "read")
    assert not p.passed and p.kind == kind
    assert "write_dml" in p.reason


@pytest.mark.parametrize("sql", [
    "DROP TABLE t", "TRUNCATE TABLE t", "ALTER TABLE t ADD COLUMN c TEXT",
    "CREATE TABLE t2 (id INT)",
])
def test_dml_tier_rejects_ddl(sql):
    p = plan(sql, "write_dml")
    assert not p.passed and "write_ddl" in p.reason


@pytest.mark.parametrize("sql,kind", [
    ("DROP TABLE t", "drop"),
    ("TRUNCATE TABLE t", "truncate"),
    ("ALTER TABLE t ADD COLUMN c TEXT", "alter"),
    ("CREATE TABLE t2 (id INT)", "create"),
])
def test_ddl_tier_allows_ddl(sql, kind):
    p = plan(sql, "write_ddl")
    assert p.passed, p.reason
    assert p.kind == kind


def test_select_is_not_the_write_channels_job():
    """SELECT 走只读通道；写通道放行它等于开了个绕过 LIMIT 封顶的后门。"""
    p = plan("SELECT * FROM t", "write_ddl")
    assert not p.passed and "查询通道" in p.reason


def test_unknown_or_unparsable_rejected():
    for sql in ("", "THIS IS NOT SQL", "EXPLAIN SELECT 1", "KILL 123"):
        assert not plan(sql, "write_ddl").passed, sql


# ------------------------------------------------------------------ 作用域
@pytest.mark.parametrize("sql,why", [
    ("UPDATE other_db.t SET a=1 WHERE id=1", "跨库"),
    ("DELETE FROM mysql.user WHERE 1=1", "系统库"),   # 会被带 WHERE 检查前的库判定先拦
    ("DROP TABLE information_schema.tables", "系统库"),
    ("UPDATE data_agent.sessions SET x=1 WHERE 1=1", "元数据库"),
])
def test_out_of_scope_targets_rejected(sql, why):
    p = plan(sql, "write_ddl")
    assert not p.passed, f"{sql} 本该被拦（{why}）"
    assert ("库" in p.reason), p.reason


def test_meta_database_is_protected_even_when_custom_named():
    """元数据库名可以改，黑名单必须跟着改 —— 写死 data_agent 的话换个库名就绕过了。"""
    s = _settings()
    s.protected_databases = set(s.protected_databases) | {"company_meta"}
    p = plan("DELETE FROM company_meta.sessions WHERE id=1", "write_dml", settings=s)
    assert not p.passed and "元数据库" in p.reason


def test_unqualified_target_is_scoped_to_current_db():
    p = plan("DELETE FROM sales_test WHERE id=1")
    assert p.passed
    assert p.targets[0]["database"] == DB
    assert p.targets[0]["is_main"] is True


# ------------------------------------------------------------------ WHERE / 多表
def test_update_without_where_rejected():
    p = plan("UPDATE t SET a=1")
    assert not p.passed and "WHERE" in p.reason


def test_delete_without_where_rejected_and_truncate_is_the_explicit_way():
    assert not plan("DELETE FROM t").passed
    assert plan("TRUNCATE TABLE t", "write_ddl").passed


def test_multi_table_delete_rejected():
    p = plan("DELETE a FROM t a JOIN t2 b ON a.id=b.id WHERE b.x=1")
    assert not p.passed and "JOIN" in p.reason


def test_plain_single_table_delete_passes():
    """上一条的反向验证：不能为了拦 JOIN 把所有 DELETE 都拦掉。"""
    assert plan("DELETE FROM t WHERE id=1").passed


def test_subquery_read_from_other_db_rejected():
    p = plan("INSERT INTO t(a) SELECT x FROM other_db.u", "write_dml")
    assert not p.passed and "other_db" in p.reason


# ------------------------------------------------------------------ 函数黑名单
def test_load_file_in_set_clause_rejected():
    """钉住 Anonymous/Func 那个坑：只按 sql_name() 判会看成 ANONYMOUS 而放行。"""
    p = plan("UPDATE t SET x = load_file('/etc/passwd') WHERE id=1")
    assert not p.passed and "load_file" in p.reason


def test_benchmark_in_where_rejected():
    p = plan("DELETE FROM t WHERE sleep(10) IS NULL")
    assert not p.passed and "sleep" in p.reason


def test_into_outfile_rejected():
    """`INTO OUTFILE` 在 sqlglot 的 mysql 方言里根本解析不动；拦它的 reason 必须是
    "写服务器文件"，不是"语法解析失败" —— 前者人看得懂，后者会被当成 SQL 写错了。"""
    p = plan("INSERT INTO t SELECT * FROM u INTO OUTFILE '/tmp/x'", "write_ddl")
    assert not p.passed and "OUTFILE" in p.reason, p.reason


def test_into_dumpfile_rejected():
    p = plan("INSERT INTO t SELECT * FROM u INTO DUMPFILE '/tmp/y'", "write_ddl")
    assert not p.passed and "DUMPFILE" in p.reason, p.reason


def test_table_named_like_export_keyword_is_not_collateral_damage():
    """串匹配只认 `INTO OUTFILE/DUMPFILE` 这个搭配：名叫 outfile_config 的
    正常业务表不能被误杀。"""
    assert plan("DELETE FROM outfile_config WHERE id=1").passed


def test_export_phrase_in_a_string_value_is_rejected_too():
    """宁可错杀：写进文本里的 into outfile 判不掉就没有"区分语法与字面量"的把握，
    代价是一条把这句话当数据的 INSERT 得改写，方向是安全的一侧。"""
    p = plan("INSERT INTO report(body) VALUES('select into outfile')")
    assert not p.passed and "OUTFILE" in p.reason


# ------------------------------------------------------------------ 多语句
def test_multi_statement_rejected():
    p = plan("DELETE FROM t WHERE id=1; DROP TABLE users", "write_ddl")
    assert not p.passed and "单条" in p.reason


# ------------------------------------------------------------------ 备份 / 确认词
def test_update_plans_backup_of_affected_rows_only():
    p = plan("UPDATE t SET amount=0 WHERE id<10")
    assert p.passed
    assert p.backup_table.startswith("_bak_t_")
    assert p.backup_sql == (f"CREATE TABLE `{DB}`.`{p.backup_table}` AS "
                            f"SELECT * FROM `{DB}`.`t` WHERE id < 10")
    assert p.estimate_sql == f"SELECT COUNT(*) FROM `{DB}`.`t` WHERE id < 10"


def test_destructive_needs_table_name_and_full_backup():
    p = plan("DROP TABLE sales_test", "write_ddl")
    assert p.passed and p.destructive
    assert p.confirm_phrase == "sales_test"
    assert p.backup_sql.endswith(f"SELECT * FROM `{DB}`.`sales_test`")


def test_destructive_rejected_when_backup_disabled():
    """关掉自动备份又不给整表后悔药，就不能允许删表（失败要朝安全的一侧失败）。"""
    s = _settings(write_auto_backup=False)
    p = plan("DROP TABLE sales_test", "write_ddl", settings=s)
    assert not p.passed and "备份" in p.reason


def test_non_destructive_alter_needs_no_phrase():
    p = plan("ALTER TABLE t ADD COLUMN note TEXT", "write_ddl")
    assert p.passed and not p.destructive and p.confirm_phrase == ""


def test_drop_column_is_destructive_and_backed_up():
    p = plan("ALTER TABLE t DROP COLUMN city", "write_ddl")
    assert p.passed and p.destructive and p.confirm_phrase == "t"
    assert p.backup_sql and "SELECT * FROM" in p.backup_sql


def test_backup_prefix_is_sanitized_and_name_capped():
    """前缀来自环境变量，也不能带出标识符字符集之外的东西（它要拼进表名）。"""
    s = _settings(write_backup_prefix="; drop x; --")
    p = plan("DROP TABLE t", "write_ddl", settings=s)
    assert p.backup_table.startswith("dropx_"), p.backup_table
    assert not any(c in p.backup_table for c in ";'\" -\\")
    assert len(p.backup_table) <= 64


def test_backup_prefix_falls_back_when_empty_or_punctuation():
    s = _settings(write_backup_prefix="!!!")
    p = plan("DROP TABLE t", "write_ddl", settings=s)
    assert p.backup_table.startswith("_bak_")


def test_backup_names_are_unique_within_the_same_second():
    """同一张表连着写两次（先修数据再补条件）会落在同一秒：只用秒级时间戳会算出
    同一个备份名，第二条 CREATE TABLE 直接撞车（真 MySQL 实跑撞出来的）。"""
    names = {plan(f"DELETE FROM t WHERE id={i}").backup_table for i in range(30)}
    assert len(names) == 30, "备份表名必须次次不同"
    assert all(n.startswith("_bak_t_") and len(n) <= 64 for n in names)


def test_long_table_name_keeps_the_unique_tail_not_the_prefix():
    """表名长到超 64 时砍中间、留尾巴：截掉时间戳与随机位就等于放弃唯一性。"""
    long_table = "t" * 60
    a = plan(f"DELETE FROM {long_table} WHERE id=1").backup_table
    b = plan(f"DELETE FROM {long_table} WHERE id=2").backup_table
    assert len(a) <= 64 and len(b) <= 64 and a != b
    assert re.search(r"_\d{8}_\d{6}_[0-9a-f]{6}$", a), a   # 尾巴完整保留
    assert a.startswith("_bak_t")


@pytest.mark.parametrize("table", ["t;drop", "1abc", "tbl-name", "t`--", "t'"])
def test_illegal_table_name_rejected(table):
    """只钉真正危险的形态：拼进限定名/备份表名之前，字符集必须卡死。"""
    p = plan(f"DELETE FROM {table} WHERE id=1")
    assert not p.passed, f"非法表名 {table} 不该被放行"


@pytest.mark.parametrize("sql", ["DELETE FROM `t` WHERE id=1",
                                 "DELETE FROM kaggle_demo.t WHERE id=1"])
def test_quoted_and_qualified_but_legal_names_still_pass(sql):
    """反向验证：反引号与库限定是 MySQL 的正常写法，不能一并当成非法拒掉。"""
    assert plan(sql).passed


def test_table_alias_survives_into_estimate_and_backup():
    """`UPDATE t a SET a.x WHERE a.id` 的别名必须搬进重写的预估/备份语句，
    否则执行时报 unknown column a.id，看着像安检器坏了。"""
    p = plan("UPDATE t a SET a.amount=0 WHERE a.id<10")
    assert p.passed, p.reason
    assert "AS `a`" in p.estimate_sql, p.estimate_sql
    assert "AS `a`" in p.backup_sql, p.backup_sql
    assert "a.id" in p.backup_sql, p.backup_sql


def test_illegal_alias_rejected():
    p = plan("UPDATE t a-b SET a.amount=0 WHERE a.id<10")
    assert not p.passed


# ------------------------------------------------------------------ 预估
def test_insert_values_estimate_is_row_count():
    p = plan("INSERT INTO t(a) VALUES(1),(2),(3)")
    assert p.passed and p.estimate_sql == "SELECT 3 AS estimated"


def test_insert_select_estimate_counts_source():
    p = plan("INSERT INTO t(a) SELECT x FROM u WHERE y>1")
    assert p.passed and p.estimate_sql.startswith("SELECT COUNT(*) FROM (")
    assert "y > 1" in p.estimate_sql


def test_create_has_no_estimate():
    p = plan("CREATE TABLE t2 (id INT)", "write_ddl")
    assert p.passed and p.estimate_sql is None


# ------------------------------------------------------------------ 辅助
def test_tier_rank_ordering_and_unknown():
    assert tier_rank("read") < tier_rank("write_dml") < tier_rank("write_ddl")
    assert tier_rank(None) == 0 and tier_rank("yolo") == 0


def test_classify_kind_covers_roots():
    assert classify_kind("DELETE FROM t WHERE id=1") == "delete"
    assert classify_kind("TRUNCATE TABLE t") == "truncate"
    assert classify_kind("SELECT 1; DROP TABLE t") == "unknown"


def test_plan_to_dict_contract_shape():
    d = plan("DELETE FROM t WHERE id=1").to_dict()
    assert set(d) >= {"passed", "reason", "statement_kind", "targets", "destructive",
                      "confirm_phrase", "backup_table", "backup_sql", "estimate_sql",
                      "final_sql"}
    assert d["final_sql"] == "DELETE FROM t WHERE id=1"
