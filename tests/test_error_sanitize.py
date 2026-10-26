"""错误信息脱敏回归（设计文档 §10.4）。

返回前端的错误不得暴露：连接串/账号、数据库 IP:Port、驱动与框架异常类名、多行调用栈。
这里锁死"技术栈前缀必须被抹掉"这一条——它曾在真实执行失败时把
(pymysql.err.OperationalError) 原样透给用户。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from basic.utils.helpers import sanitize_error  # noqa: E402


def test_strip_driver_exception_class():
    raw = ("(pymysql.err.OperationalError) (1054, \"Unknown column 'x' in 'field list'\") "
           "[SQL: SELECT x FROM t]")
    out = sanitize_error(raw)
    assert "pymysql" not in out
    assert "Unknown column" in out          # 业务可读部分要留下


def test_strip_sqlalchemy_exception_class():
    out = sanitize_error("(sqlalchemy.exc.OperationalError) (2003, \"Can't connect\")")
    assert "sqlalchemy" not in out
    assert "()" not in out.replace("(2003", "")   # 不残留空括号


def test_strip_dsn_and_ip():
    out = sanitize_error("mysql+pymysql://root:secret@127.0.0.1:3306/order_db 失败")
    assert "root" not in out and "secret" not in out
    assert "127.0.0.1" not in out
    assert "已脱敏" in out


def test_strip_db_account_names():
    """驱动原文里的账号：DSN 正则管不到 "Access denied for user 'x'@'y'" 和 "user=x"。"""
    out = sanitize_error('(1045, "Access denied for user \'analyst\'@\'localhost\' '
                         '(using password: YES)")')
    assert "analyst" not in out and "localhost" not in out
    out2 = sanitize_error("连接失败 host=db.internal user=analyst password=p@ss")
    assert "analyst" not in out2 and "p@ss" not in out2


def test_account_masking_does_not_eat_column_names():
    """脱敏只动账号：Unknown column 'username' 这种业务可读部分必须原样留着。"""
    out = sanitize_error("(1054, \"Unknown column 'username' in 'field list'\")")
    assert "username" in out and "Unknown column" in out


def test_flatten_stack_and_truncate():
    out = sanitize_error("第一行\n  第二行   带栈  " * 50)
    assert "\n" not in out
    assert len(out) <= 200


def test_plain_message_untouched():
    assert sanitize_error("尚未激活数据源") == "尚未激活数据源"
