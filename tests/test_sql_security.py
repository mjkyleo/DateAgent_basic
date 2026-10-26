"""SQL 安全门测试（设计文档 §13 验收：非法 SQL 被拦截）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from basic.security.sql_security import SqlSecurityChecker
from basic.config.settings import Settings


def _check(sql: str, max_limit: int = 1000):
    return SqlSecurityChecker(Settings()).check(sql, max_limit=max_limit)


class TestSqlSecurity:
    def test_valid_select_gets_limit(self):
        r = _check("SELECT id, amount FROM t_order")
        assert r["passed"] and "LIMIT 1000" in r["final_sql"]

    def test_delete_rejected(self):
        r = _check("DELETE FROM t_order WHERE id = 1")
        assert not r["passed"] and "仅允许 SELECT" in r["reason"]

    def test_drop_rejected(self):
        r = _check("DROP TABLE t_order")
        assert not r["passed"]

    def test_multi_statement_rejected(self):
        r = _check("SELECT 1; SELECT 2")
        assert not r["passed"] and "单条" in r["reason"]

    def test_semicolon_trailing_ok(self):
        r = _check("SELECT 1;")
        assert r["passed"]

    def test_insert_rejected(self):
        r = _check("INSERT INTO t_order(amount) VALUES (1)")
        assert not r["passed"]

    def test_update_rejected(self):
        r = _check("UPDATE t_order SET amount = 0")
        assert not r["passed"]

    def test_into_outfile_rejected(self):
        r = _check("SELECT * FROM t_order INTO OUTFILE '/tmp/x.csv'")
        assert not r["passed"] and "OUTFILE" in r["reason"].upper()

    def test_load_file_rejected(self):
        r = _check("SELECT LOAD_FILE('/etc/passwd')")
        assert not r["passed"] and "load_file" in r["reason"]

    def test_explicit_limit_capped(self):
        r = _check("SELECT * FROM t_order LIMIT 99999")
        assert r["passed"] and "LIMIT 1000" in r["final_sql"]

    def test_explicit_small_limit_kept(self):
        r = _check("SELECT * FROM t_order LIMIT 5")
        assert r["passed"] and "LIMIT 5" in r["final_sql"]

    def test_union_of_selects_ok(self):
        r = _check("SELECT 1 UNION SELECT 2")
        assert r["passed"]

    def test_union_with_delete_rejected(self):
        r = _check("SELECT 1 UNION DELETE FROM t")
        assert not r["passed"]

    def test_syntax_error_rejected(self):
        r = _check("SELECT FROM WHERE")
        assert not r["passed"] and "语法" in r["reason"]

    def test_empty_rejected(self):
        r = _check("   ")
        assert not r["passed"]
