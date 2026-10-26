"""接口级：双层确认的"确认状态"要真的落库（回看页依赖这两个布尔）。

只测状态写入，不碰 LLM/数据库：数据源故意填不可达的 MySQL，
generate_sql 取不到表清单会走 except 分支（known_tables=[]），不影响断言。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from basic.main import create_app  # noqa: E402
from basic.storage.local_store import LocalStore  # noqa: E402


@pytest.fixture
def client(tmp_path):
    app = create_app()
    store = LocalStore(str(tmp_path / "s.db"))     # 隔离真实 data/store.db
    app.state.store = store
    # 会话与模型配置走 app.state.sessions（生产是 MySQL）；测试环境同样注入临时库，
    # 否则请求会写到真实 data/store.db 里，断言看到的却是另一个库。
    app.state.sessions = store
    try:
        yield TestClient(app), store
    finally:
        store.close()


def _new_session(store, client):
    ds = client.post("/api/datasource", json={
        "name": "fake", "type": "mysql", "host": "127.0.0.1", "port": 1,
        "user": "u", "password": "p", "database": "db"}).json()
    store.set_active(ds["id"])
    return store.create_session(ds_id=ds["id"], user_question="各平台均值",
                                intent="分类对比分析", target_db="db")


def test_generate_sql_marks_intent_confirmed(client):
    """确认意图 → 生成 SQL 时落 intent_confirmed=1（否则回看永远显示"意图 未确认"）。"""
    client, store = client
    sid = _new_session(store, client)
    assert store.get_session(sid)["intent_confirmed"] is False

    r = client.post("/api/agent/generate_sql", json={
        "session_id": sid,
        "intent_context": {"original_question": "各平台均值", "intent": "分类对比分析",
                           "target_db": "db", "target_tables": ["t"], "target_fields": [],
                           "sql_preview": "SELECT p, AVG(v) FROM t GROUP BY p",
                           "risk_level": "low", "hint": ""},
    })
    assert r.status_code == 200, r.text
    sess = store.get_session(sid)
    assert sess["intent_confirmed"] is True
    assert sess["sql_confirmed"] is False        # SQL 确认是第二层，此时还没到


def test_confirm_sql_marks_sql_confirmed(client):
    client, store = client
    sid = _new_session(store, client)
    store.update_session(sid, sql="SELECT 1", intent_confirmed=True)
    r = client.post("/api/agent/confirm_sql", json={"session_id": sid})
    assert r.status_code == 200, r.text
    assert store.get_session(sid)["sql_confirmed"] is True


def test_execute_rejects_unconfirmed_sql(client):
    """未确认的 SQL 必须 409（后端强制，不可绕过）。"""
    client, store = client
    sid = _new_session(store, client)
    store.update_session(sid, sql="SELECT 1", intent_confirmed=True, sql_confirmed=False)
    r = client.post("/api/agent/execute_sql", json={"session_id": sid, "sql": ""})
    assert r.status_code == 409, r.text


def test_delete_active_ds_falls_back_to_newest(client):
    """删掉激活中的数据源后要回落到剩余数据源，不能落在"未激活"空态。"""
    client, store = client
    a = client.post("/api/datasource", json={
        "name": "a", "type": "mysql", "host": "127.0.0.1", "port": 1,
        "user": "u", "password": "p", "database": "db"}).json()
    b = client.post("/api/datasource", json={
        "name": "b", "type": "mysql", "host": "127.0.0.1", "port": 1,
        "user": "u", "password": "p", "database": "db"}).json()
    store.set_active(a["id"])

    r = client.delete(f"/api/datasource/{a['id']}")
    assert r.status_code == 200, r.text
    assert "已切换到「b」" in r.json()["msg"]        # 回落到最近新增的（id 最大的）
    assert store.get_active()["name"] == "b"

    # 再删 b（此时 b 是激活源）：没有剩余源可回落 → 无"已切换到"字样，激活清空
    r2 = client.delete(f"/api/datasource/{b['id']}")
    assert "已切换到" not in r2.json()["msg"]
    assert store.get_active() is None
