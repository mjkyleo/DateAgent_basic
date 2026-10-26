"""运行时修复回归测试：钉住本次修掉的五个运行时 bug。

覆盖点：
1. 元数据库降级为 SQLite 时，parse_intent 带 session_id 不再 500
   （LocalStore 没有 bind_question，缺能力防护会直接 AttributeError）；
2. 删数据源级联清理"另一个会话存储"（MySQL）里的轮次，不留孤儿；
3. lifespan 启动时先关闭 create_app 预建的 store，不留双份连接；
4. 文件上传走线程池后导入链路行为不变（返回的行数/列数一致）；
5. 元数据库凭据复用不只看"激活源"——上传 CSV 把激活源变成 file 后，
   重启仍要能从其它 MySQL 源借到密码，否则静默降级 SQLite、面板全 500。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from basic.main import create_app  # noqa: E402
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
    app.state.settings.llm_enabled = False        # 强制规则兜底：离线且确定
    app.state.settings.mysql_enabled = False      # 会话走 SQLite 形态
    app.state.settings.store_path = str(tmp_path / "s.db")
    app.state.settings.uploads_dir = str(tmp_path / "uploads")
    store = LocalStore(app.state.settings.store_path)
    app.state.store = store
    app.state.sessions = store
    try:
        yield TestClient(app), store
    finally:
        store.close()


def _mysql_ds(client):
    ds = client.post("/api/datasource", json={
        "name": "fake", "type": "mysql", "host": "127.0.0.1", "port": 1,
        "user": "u", "password": "p", "database": "db"}).json()
    return ds


# ---------------------------------------------------------------- 1. 降级存储 bind_question
def test_sqlite_store_has_no_bind_question(client):
    """前提事实：SQLite 版会话存储确实没有 bind_question（防护的依据）。"""
    _client, store = client
    assert not hasattr(store, "bind_question")
    assert hasattr(LocalStore, "create_session")


def test_parse_intent_with_session_id_survives_degraded_store(client, monkeypatch):
    """降级 SQLite 时带 session_id 提问：修复前 AttributeError → 500，修复后走新建分支。"""
    client, store = client
    ds = _mysql_ds(client)
    # 假 MySQL 不可达：桩掉 schema 组装（本测试目标是会话绑定分支，不测元数据层）
    from basic.core.meta_tools import MetaTool
    monkeypatch.setattr(MetaTool, "schema_context", lambda self, db, tables, fields=None: (
        f"# 数据库 {db} 的表结构\n## 表 t_order\n  - 平台: TEXT\n  - 金额: DECIMAL\n"))
    sid = store.create_session(ds_id=ds["id"], user_question="")   # 空会话
    r = client.post("/api/agent/parse_intent", json={
        "user_query": "各平台订单量对比", "session_id": sid, "ds_id": ds["id"],
        "selected_db": "db", "selected_tables": ["t_order"]})
    assert r.status_code == 200, r.text
    new_sid = r.json()["session_id"]
    assert new_sid and new_sid != sid            # 未绑定旧空会话，而是新建一条
    assert store.get_session(new_sid)["user_question"] == "各平台订单量对比"


# ---------------------------------------------------------------- 2. 删源级联 MySQL 会话
class _RecordingSessions:
    """假会话存储：只记录 delete_rounds_by_ds 调用，且与数据源 store 不同对象。"""

    def __init__(self):
        self.calls = []

    def delete_rounds_by_ds(self, ds_id: int) -> int:
        self.calls.append(ds_id)
        return 3


def test_delete_datasource_cascades_to_mysql_store(client):
    client, _store = client
    ds = _mysql_ds(client)
    sessions = _RecordingSessions()
    client.app.state.sessions = sessions         # 模拟生产：会话在 MySQL，与 store 分离
    r = client.delete(f"/api/datasource/{ds['id']}")
    assert r.status_code == 200, r.text
    assert sessions.calls == [ds["id"]]


def test_delete_datasource_survives_cascade_failure(client):
    """级联清理抛错不该挡住数据源删除本身（主流程优先）。"""

    class _Boom(_RecordingSessions):
        def delete_rounds_by_ds(self, ds_id: int) -> int:
            raise RuntimeError("mysql down")

    client, _store = client
    ds = _mysql_ds(client)
    client.app.state.sessions = _Boom()
    r = client.delete(f"/api/datasource/{ds['id']}")
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 3. lifespan 关闭预建 store
def test_lifespan_closes_prebuilt_store(tmp_path, monkeypatch):
    """with TestClient 会触发 lifespan：create_app 预建的 store 必须被关掉。

    修复前 lifespan 直接覆盖 app.state.store，前一套 SQLite 连接从未 close；
    这里抓住两个实例，启动后断言旧的已关、新的开着。
    """
    created: list[LocalStore] = []
    real_init, real_close = LocalStore.__init__, LocalStore.close

    def _init(self, db_path):
        real_init(self, db_path)
        created.append(self)

    def _close(self):
        self._closed = True
        real_close(self)

    monkeypatch.setattr(LocalStore, "__init__", _init)
    monkeypatch.setattr(LocalStore, "close", _close)

    from basic.config.settings import settings as global_settings
    monkeypatch.setattr(global_settings, "store_path", str(tmp_path / "life.db"))
    monkeypatch.setattr(global_settings, "mysql_enabled", False)

    app = create_app()
    assert len(created) == 1 and created[0] is app.state.store
    pre = created[0]
    with TestClient(app):                          # 进入 lifespan
        pass
    assert len(created) == 2, "lifespan 应重建 store"
    assert getattr(pre, "_closed", False), "create_app 预建的 store 必须被关闭"
    assert getattr(created[1], "_closed", False), "退出 lifespan 时当前 store 应被关闭"


# ---------------------------------------------------------------- 5. 元数据库凭据复用
class _DsStore:
    """假数据源库：只暴露元数据库凭据复用要用的 get_active/list_datasources。"""

    def __init__(self, active, others=()):
        self._active = active
        self._others = list(others)

    def get_active(self):
        return self._active

    def list_datasources(self):
        return self._others


class _Settings:
    mysql_host = "127.0.0.1"
    mysql_port = 3306
    mysql_user = "root"
    mysql_password = ""          # 未显式配置：正是复用数据源凭据的分支
    mysql_database = "data_agent"


_MYSQL_DS = {"type": "mysql", "host": "127.0.0.1", "port": 3306,
             "user": "root", "password": "pw123", "database": "kaggle_demo"}
_FILE_DS = {"type": "file", "host": "", "port": 0, "user": "",
            "password": "", "database": "sales_test"}


def test_meta_mysql_falls_back_to_another_mysql_source():
    """激活源变成 file（真实场景：上传了 CSV），仍要借到别的 MySQL 源的密码。

    修复前只认激活源，返回空密码 → 1045 Access denied → 元数据库静默降级 SQLite，
    随后 /api/providers、/api/projects 全 500 —— 用户只是换了个数据源而已。
    """
    from basic.storage.mysql_store import resolve_meta_mysql
    cfg = resolve_meta_mysql(_DsStore(_FILE_DS, [_MYSQL_DS]), _Settings())
    assert cfg["password"] == "pw123"
    assert cfg["user"] == "root"


def test_meta_mysql_prefers_active_source():
    from basic.storage.mysql_store import resolve_meta_mysql
    other = dict(_MYSQL_DS, password="other")
    cfg = resolve_meta_mysql(_DsStore(_MYSQL_DS, [other]), _Settings())
    assert cfg["password"] == "pw123", "激活源优先，别越过它去用其它源"


def test_meta_mysql_env_password_wins():
    """显式配了 AGENT_MYSQL_PASSWORD 就不该再去翻数据源表。"""
    from basic.storage.mysql_store import resolve_meta_mysql

    class _S(_Settings):
        mysql_password = "from-env"

    cfg = resolve_meta_mysql(_DsStore(_FILE_DS, []), _S())
    assert cfg["password"] == "from-env"


def test_meta_mysql_without_any_mysql_source_stays_empty():
    """真没有 MySQL 源时保持空密码 —— 由上层照常降级，不臆造凭据。"""
    from basic.storage.mysql_store import resolve_meta_mysql
    cfg = resolve_meta_mysql(_DsStore(_FILE_DS, [_FILE_DS]), _Settings())
    assert cfg["password"] == ""


# ---------------------------------------------------------------- 6. 降级存储不弹 500 栈
def test_provider_and_project_routes_degrade_to_503(client, monkeypatch):
    """会话存储降成 SQLite（没有 provider/project 表）时报 503 说明，不是 500 栈。"""
    client, _store = client
    assert not hasattr(_store, "provider_secret")        # 前提：夹具注入的就是降级形态
    for path in ("/api/providers", "/api/projects"):
        r = client.get(path)
        assert r.status_code == 503, (path, r.text)
        assert "重启" in r.json()["detail"]
    # 按环境变量拉远端模型清单不依赖元数据库，降级时仍应可用
    from basic.api import provider_api
    monkeypatch.setattr(provider_api, "list_models",
                        lambda cfg: (True, ["m1"]))
    r = client.get("/api/providers/remote-models")
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["m1"]


# ---------------------------------------------------------------- 4. 上传线程池后行为不变
def test_file_upload_import_still_works(client):
    client, _store = client
    src = Path(client.app.state.settings.uploads_dir) / "t.csv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("平台,金额\nA,10\nB,20\nA,5\n", encoding="utf-8")
    with src.open("rb") as fh:
        r = client.post("/api/datasource/file", files={"file": ("销售.csv", fh, "text/csv")})
    assert r.status_code == 200, r.text
    ds = r.json()
    assert ds["type"] == "file" and ds["is_active"] is True
    # Datasource 响应不含 file_path：从本地库读回落盘路径，再直查生成的表
    row = client.app.state.store._conn.execute(
        "SELECT file_path FROM datasources WHERE id=?", (ds["id"],)).fetchone()
    import sqlite3
    conn = sqlite3.connect(row[0])
    try:
        n = conn.execute(f'SELECT COUNT(*) FROM "{ds["database"]}"').fetchone()[0]
    finally:
        conn.close()
    assert n == 3
