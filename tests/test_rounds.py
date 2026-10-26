"""轮次（一轮 = 一条 session）持久化与历史回读。

覆盖点：
- parse_intent 阶段就落 intent/target_db —— 中途取消的轮次也留痕；
- list_rounds 只回摘要字段，按时间倒序，明细走 get_session；
- 旧库（无 intent/target_db 列）能自动补列后正常工作；
- delete_round 真删。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from basic.storage.local_store import LocalStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """Windows 上 SQLite 句柄不关闭会导致临时目录无法删除，必须显式 close。"""
    s = LocalStore(str(tmp_path / "s.db"))
    try:
        yield s
    finally:
        s.close()


def test_create_session_records_intent_and_db(store):
    sid = store.create_session(ds_id=1, user_question="每月订单额趋势",
                               intent="时间趋势分析", target_db="kaggle_demo")
    got = store.get_session(sid)
    assert got["user_question"] == "每月订单额趋势"
    assert got["intent"] == "时间趋势分析"
    assert got["target_db"] == "kaggle_demo"
    assert got["query_result"] is None and got["analysis_result"] is None


def test_list_rounds_descending_and_summary_fields(store):
    a = store.create_session(ds_id=1, user_question="第一轮", intent="分类对比分析")
    b = store.create_session(ds_id=1, user_question="第二轮", intent="时间趋势分析")
    store.update_session(b, sql="SELECT 1", query_result={"row_count": 7, "cost_ms": 75},
                         analysis_result={"suggest_chart": "bar", "summary_text": "摘要"})
    rounds = store.list_rounds()
    assert [r["session_id"] for r in rounds] == [b, a]      # 倒序
    head = rounds[0]
    assert head["row_count"] == 7 and head["cost_ms"] == 75
    assert head["suggest_chart"] == "bar" and head["has_result"] is True
    assert rounds[1]["has_result"] is False                 # 未执行的轮次也列出


def test_list_rounds_limit_and_ds_filter(store):
    for i in range(5):
        store.create_session(ds_id=1 if i < 3 else 2, user_question=f"q{i}")
    assert len(store.list_rounds(limit=3)) == 3
    assert len(store.list_rounds(ds_id=2)) == 2
    assert len(store.list_rounds(limit=999)) == 5           # 上限被夹到 200


def test_round_detail_returns_full_payload(store):
    sid = store.create_session(ds_id=1, user_question="各平台均值", intent="分类对比分析")
    store.update_session(sid, sql="SELECT p, AVG(v) FROM t GROUP BY p", sql_confirmed=True,
                         query_result={"columns": ["p", "v"], "rows": [{"p": "A", "v": 1}],
                                       "row_count": 1, "cost_ms": 3, "truncated": False},
                         analysis_result={"summary_text": "x", "insights": [],
                                          "suggest_chart": "bar", "chart_option": {}})
    full = store.get_session(sid)
    assert full["sql"].startswith("SELECT")
    assert full["sql_confirmed"] is True
    assert full["query_result"]["row_count"] == 1
    assert full["analysis_result"]["suggest_chart"] == "bar"


def test_delete_round(store):
    sid = store.create_session(ds_id=1, user_question="待删除")
    assert store.delete_round(sid) is True
    assert store.get_session(sid) is None
    assert store.delete_round(sid) is False                  # 重复删除返回 False


def test_legacy_db_gets_new_columns(tmp_path):
    """模拟旧库：手工建一张没有 intent/target_db 的 sessions 表，验证自动补列。"""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, ds_id INTEGER,"
                     " user_question TEXT, intent_confirmed INTEGER DEFAULT 0, sql TEXT,"
                     " sql_confirmed INTEGER DEFAULT 0, query_result TEXT,"
                     " analysis_result TEXT, created_at TEXT, updated_at TEXT)")
        conn.commit()
    finally:
        conn.close()

    s = LocalStore(path)                                     # 不应抛错
    try:
        sid = s.create_session(ds_id=1, user_question="旧库新轮",
                               intent="分类对比分析", target_db="db1")
        assert s.get_session(sid)["intent"] == "分类对比分析"
        assert s.list_rounds()[0]["target_db"] == "db1"
    finally:
        s.close()
