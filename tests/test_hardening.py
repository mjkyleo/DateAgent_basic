"""加固回归测试：把本次优化修掉的口子钉住，防止改回去。

覆盖点：
1. execute_sql 不允许"不带 session 直投 SQL"绕过双层确认（早前的真实缺口）；
2. SELECT ... INTO（写会话变量/文件）被安全门拦下；
3. 删除数据源时级联清理它的历史轮次，不留孤儿；
4. 表清单缓存带数据源维度 —— 换源不会串到上一个库的表名；
5. 失败不缓存：取不到表清单时照旧抛异常，缓存层不改变失败语义。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from basic.config.settings import Settings  # noqa: E402
from basic.core.meta_tools import MetaTool  # noqa: E402
from basic.main import create_app  # noqa: E402
from basic.security.sql_security import SqlSecurityChecker  # noqa: E402
from basic.storage.local_store import LocalStore  # noqa: E402
from basic.utils import cache  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.cache_invalidate()
    yield
    cache.cache_invalidate()


@pytest.fixture
def client(tmp_path):
    app = create_app()
    store = LocalStore(str(tmp_path / "s.db"))
    app.state.store = store
    app.state.sessions = store      # 会话存储：测试同样注入临时库（见 test_confirm_flow 注释）
    try:
        yield TestClient(app), store
    finally:
        store.close()


def _ds_and_session(store, client):
    ds = client.post("/api/datasource", json={
        "name": "fake", "type": "mysql", "host": "127.0.0.1", "port": 1,
        "user": "u", "password": "p", "database": "db"}).json()
    store.set_active(ds["id"])
    return ds, store.create_session(ds_id=ds["id"], user_question="各平台均值",
                                    intent="分类对比分析", target_db="db")


# ---------------------------------------------------------------- 1. 双层确认不可绕过
def test_execute_without_session_is_rejected(client):
    """不带 session_id 直投 SQL 必须 409 —— 之前这里会直接执行，是绕过的口子。"""
    client, store = client
    _ds_and_session(store, client)
    r = client.post("/api/agent/execute_sql", json={"session_id": "", "sql": "SELECT 1"})
    assert r.status_code == 409, r.text


def test_execute_with_confirmed_session_passes_guard(client):
    """已确认的会话能走到执行阶段（这里数据源不可达 → 500，但不是 409）。"""
    client, store = client
    _, sid = _ds_and_session(store, client)
    store.update_session(sid, sql="SELECT 1", intent_confirmed=True, sql_confirmed=True)
    r = client.post("/api/agent/execute_sql", json={"session_id": sid})
    assert r.status_code != 409, r.text


def test_execute_unknown_session_is_404(client):
    client, _store = client
    r = client.post("/api/agent/execute_sql",
                    json={"session_id": "not-exist", "sql": "SELECT 1"})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------- 2. 安全门补强
def _check(sql: str):
    return SqlSecurityChecker(Settings()).check(sql, max_limit=1000)


def test_select_into_variable_rejected():
    """SELECT 1 INTO @x 会把结果写进会话变量（等价于落盘），必须拦。"""
    r = _check("SELECT amount FROM t_order INTO @x")
    assert not r["passed"]


def test_plain_select_still_allowed():
    assert _check("SELECT amount FROM t_order LIMIT 10")["passed"] is True


# ---------------------------------------------------------------- 3. 级联清理
def test_delete_datasource_cascades_rounds(client):
    client, store = client
    ds, sid = _ds_and_session(store, client)
    assert store.get_session(sid) is not None
    r = client.delete(f"/api/datasource/{ds['id']}")
    assert r.status_code == 200, r.text
    assert store.get_session(sid) is None          # 轮次跟着一起没了


def test_delete_datasource_keeps_other_ds_rounds(client):
    client, store = client
    ds_a, sid_a = _ds_and_session(store, client)
    ds_b = client.post("/api/datasource", json={
        "name": "other", "type": "mysql", "host": "127.0.0.1", "port": 1,
        "user": "u", "password": "p", "database": "db"}).json()
    sid_b = store.create_session(ds_id=ds_b["id"], user_question="另一源的轮次")
    client.delete(f"/api/datasource/{ds_a['id']}")
    assert store.get_session(sid_a) is None
    assert store.get_session(sid_b) is not None    # 别人的轮次不受影响


# ---------------------------------------------------------------- 4/5. 元数据缓存
class _FakeAdapter:
    """假的适配器：记下被调用次数，用来验证缓存命中与失败不缓存。"""

    def __init__(self, tables=None, fail=False):
        self._tables = tables if tables is not None else ["t_order"]
        self.calls = 0
        self.fail = fail

    def list_tables(self, db_name):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return list(self._tables)

    def close(self):
        pass


def test_list_tables_cached_per_datasource():
    settings = Settings()
    settings.meta_cache_ttl_s = 30
    a1 = _FakeAdapter(["t_order"])
    t1 = MetaTool(a1, settings, cache_scope="ds1")
    assert t1.list_tables("db") == ["t_order"]
    assert t1.list_tables("db") == ["t_order"]
    assert a1.calls == 1, "同一数据源同一库应命中缓存"

    a2 = _FakeAdapter(["t_user"])
    t2 = MetaTool(a2, settings, cache_scope="ds2")
    assert t2.list_tables("db") == ["t_user"], "换数据源必须拿到自己的表，不能串"
    assert a2.calls == 1


def test_failed_list_tables_not_cached():
    settings = Settings()
    settings.meta_cache_ttl_s = 30
    bad = _FakeAdapter(fail=True)
    tool = MetaTool(bad, settings, cache_scope="ds9")
    with pytest.raises(RuntimeError):
        tool.list_tables("db")
    with pytest.raises(RuntimeError):
        tool.list_tables("db")
    assert bad.calls == 2, "失败不能被缓存，否则错误会被'记住'"


def test_no_scope_means_no_cache():
    settings = Settings()
    settings.meta_cache_ttl_s = 30
    a = _FakeAdapter(["t"])
    tool = MetaTool(a, settings)           # 不传 cache_scope
    tool.list_tables("db")
    tool.list_tables("db")
    assert a.calls == 2, "没有作用域就不该缓存（测试/一次性探查用）"
