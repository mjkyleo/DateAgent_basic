"""任务清单（/api/tasks）回归测试：建议 → 可编辑待办 → 批量执行。

覆盖点：
1. 元数据库降级（存储没有任务能力）时全部 503，不假装能用；
2. CRUD：新任务排在末尾、编辑只改提交过的字段、证据快照原样回读；
3. 调序：整份顺序提交；别的项目的任务不被挪动；
4. 批量执行的确认门：confirmed=false 一律 409（绕过界面不能跑 SQL）；
5. 混合语义：绑了 SQL 的就跑，没绑的明确跳过并给原因（不假装跑了）；
6. 跑成功要落会话（SQL + sql_confirmed + 结果），数字可回看、可溯源；
7. 任务绑定的数据源被删 → 跳过，绝不悄悄退回"当前激活源"（会答非所问）；
8. 写库语句过不了真实安全门 → 任务标记失败，错误已脱敏。

接口层用假任务存储（生产是 MySQL）；执行器除了安全门那一条都用桩，
不连真实业务库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from basic.main import create_app  # noqa: E402
from basic.models.pydantic_schemas import QueryResult  # noqa: E402
from basic.storage.local_store import LocalStore  # noqa: E402

STATUSES = ("todo", "running", "done", "failed")


class _TaskStore:
    """假会话/任务存储：实现 task_api 用到的方法（生产是 MysqlStore）。

    只存内存字典，不含 SQL —— 本文件验证接口层的行为契约（确认门、跳过语义、
    落库形状）；建表与排序 SQL 本身的正确性由真实 MySQL 启动实跑覆盖。
    """

    def __init__(self) -> None:
        self.default_project_name = "未分组"
        self.projects = {1: {"id": 1, "name": "未分组", "folder_path": "",
                             "is_default": True, "session_count": 0},
                         2: {"id": 2, "name": "另一个项目", "folder_path": "",
                             "is_default": False, "session_count": 0}}
        self.tasks: dict[int, dict] = {}
        self.sessions: dict[str, dict] = {}
        self._tid = 0
        self._sid = 0

    # ---------------------------------------------------------------- 项目
    def list_projects(self):
        return list(self.projects.values())

    def get_project(self, project_id: int):
        return self.projects.get(int(project_id))

    def ensure_project(self, name: str, folder_path: str = "") -> int:
        return 1 if name == self.default_project_name else 2

    # ---------------------------------------------------------------- 会话
    def create_session(self, *, ds_id: int, user_question: str, intent: str = "",
                       target_db: str = "", project_id: int = 0, title: str = "") -> str:
        self._sid += 1
        sid = f"sess{self._sid}"
        self.sessions[sid] = {"session_id": sid, "ds_id": ds_id, "user_question": user_question,
                              "intent": intent, "project_id": project_id, "title": title,
                              "sql": "", "sql_confirmed": False, "query_result": None}
        return sid

    def update_session(self, session_id: str, **fields) -> None:
        row = self.sessions.get(session_id)
        if row:
            row.update({k: v for k, v in fields.items() if v is not None})

    def get_session(self, session_id: str):
        return self.sessions.get(session_id)

    # ---------------------------------------------------------------- 任务
    def list_tasks(self, project_id: int):
        return sorted([dict(t) for t in self.tasks.values()
                       if t["project_id"] == int(project_id)],
                      key=lambda t: (t["sort_order"], t["id"]))

    def next_task_order(self, project_id: int) -> int:
        orders = [t["sort_order"] for t in self.tasks.values()
                  if t["project_id"] == int(project_id)]
        return max(orders or [0]) + 1

    def create_task(self, *, project_id: int, title: str, ds_id: int = 0, note: str = "",
                    source_kind: str = "manual", source_ref=None, question: str = "",
                    sql: str = "", status: str = "todo", sort_order=None) -> int:
        clean = (title or "").strip()[:512]
        if not clean:
            raise ValueError("任务标题不能为空")
        if status not in STATUSES:
            raise ValueError(f"未知任务状态：{status}")
        self._tid += 1
        self.tasks[self._tid] = {
            "id": self._tid, "project_id": int(project_id), "ds_id": int(ds_id),
            "title": clean, "note": note or "", "source_kind": source_kind,
            "source_ref": source_ref, "question": question or "", "sql": sql or "",
            "status": status,
            "sort_order": int(sort_order) if sort_order is not None
            else self.next_task_order(project_id),
            "last_result": None, "session_id": "",
            "created_at": "", "updated_at": ""}
        return self._tid

    def get_task(self, task_id: int):
        row = self.tasks.get(int(task_id))
        return dict(row) if row else None

    def update_task(self, task_id: int, **fields):
        row = self.tasks.get(int(task_id))
        if not row:
            return None
        for key, value in fields.items():
            if value is None:
                continue
            if key == "title" and not str(value).strip():
                raise ValueError("任务标题不能为空")
            if key == "status" and value not in STATUSES:
                raise ValueError(f"未知任务状态：{value}")
            row[key] = value
        return dict(row)

    def delete_task(self, task_id: int) -> bool:
        return self.tasks.pop(int(task_id), None) is not None

    def reorder_tasks(self, project_id: int, task_ids):
        """与 MysqlStore 同语义：只认本项目已有的 id，未提交的接到末尾。"""
        known = [t["id"] for t in self.list_tasks(project_id)]
        listed = [int(i) for i in task_ids or [] if int(i) in known]
        listed = list(dict.fromkeys(listed))
        final = listed + [i for i in known if i not in listed]
        for idx, tid in enumerate(final, start=1):
            self.tasks[tid]["sort_order"] = idx
        return self.list_tasks(project_id)


class _StubExecutor:
    """假执行器：记录被执行的 SQL，可按需抛错。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, str]] = []

    def execute(self, *, ds, sql, max_rows=None) -> QueryResult:
        self.calls.append((int(ds["id"]), sql))
        if self.error:
            raise self.error
        return QueryResult(columns=["cnt"], rows=[{"cnt": 7}], row_count=1,
                           cost_ms=12, truncated=False)


@pytest.fixture
def client(tmp_path):
    app = create_app()
    app.state.settings.store_path = str(tmp_path / "s.db")
    local = LocalStore(app.state.settings.store_path)
    app.state.store = local
    tasks = _TaskStore()
    app.state.sessions = tasks
    try:
        yield TestClient(app), local, tasks
    finally:
        local.close()


@pytest.fixture
def ds(client):
    """一个 MySQL 数据源（不连库：批量执行走桩执行器）。"""
    _client, store, _tasks = client
    return store.create_datasource(name="业务库", host="127.0.0.1", database="db")


def _add(client: TestClient, *, title="给订单表加索引", sql="", **kw) -> dict:
    """建一条任务并回读存储里的行（断言落库结果，而不是只看响应）。"""
    r = client.post("/api/tasks", json={"project_id": 1, "title": title, "sql": sql, **kw})
    assert r.status_code == 200, r.text
    return client.app.state.sessions.get_task(r.json()["id"])


# ---------------------------------------------------------------- 1. 降级存储
def test_tasks_endpoint_returns_503_when_store_has_no_tasks(tmp_path):
    """SQLite 降级存储没有任务表：要 503 + 能照做的提示，不是 500/空列表。"""
    app = create_app()
    app.state.settings.store_path = str(tmp_path / "s2.db")
    local = LocalStore(app.state.settings.store_path)
    app.state.store = local
    app.state.sessions = local          # 降级的就是它
    try:
        c = TestClient(app)
        r = c.get("/api/tasks")
        assert r.status_code == 503, r.text
        assert "AGENT_MYSQL" in r.json()["detail"]
        assert c.post("/api/tasks", json={"project_id": 1, "title": "x"}).status_code == 503
        assert c.post("/api/tasks/run",
                      json={"project_id": 1, "confirmed": True}).status_code == 503
    finally:
        local.close()


# ---------------------------------------------------------------- 2. CRUD
def test_task_crud_roundtrip(client, ds):
    client, _store, _tasks = client
    row = _add(client, sql="SELECT COUNT(*) FROM t_order", ds_id=ds["id"],
               note="先确认全表扫描", source_kind="advice",
               source_ref={"table": "t_order", "code": "no_index",
                           "evidence": "rows=9551, indexes=[]"},
               question="订单总量是多少")
    assert row["status"] == "todo" and row["sort_order"] == 1
    assert row["source_ref"]["evidence"] == "rows=9551, indexes=[]"   # 证据快照原样回读
    assert row["ds_id"] == ds["id"]

    r = client.put(f"/api/tasks/{row['id']}", json={"title": "给 t_order 的 city 加索引"})
    assert r.status_code == 200 and r.json()["title"] == "给 t_order 的 city 加索引"
    # 只提交 title：note/sql 必须保持原值（漏传的字段不能被抹成空）
    assert r.json()["note"] == "先确认全表扫描"
    assert r.json()["sql"] == "SELECT COUNT(*) FROM t_order"

    assert client.get("/api/tasks", params={"project_id": 1}).json()[0]["status"] == "todo"
    d = client.delete(f"/api/tasks/{row['id']}")
    assert d.status_code == 200
    assert client.get("/api/tasks", params={"project_id": 1}).json() == []


def test_new_task_appended_at_end(client):
    client, _store, _tasks = client
    _add(client, title="第一步")
    _add(client, title="第二步")
    titles = [t["title"] for t in client.get("/api/tasks", params={"project_id": 1}).json()]
    assert titles == ["第一步", "第二步"]


def test_blank_title_rejected(client):
    client, _store, _tasks = client
    assert client.post("/api/tasks",
                       json={"project_id": 1, "title": "   "}).status_code == 422


def test_update_or_delete_missing_task_is_404(client):
    client, _store, _tasks = client
    assert client.put("/api/tasks/999", json={"title": "x"}).status_code == 404
    assert client.delete("/api/tasks/999").status_code == 404


# ---------------------------------------------------------------- 3. 调序
def test_reorder_changes_sequence(client):
    client, _store, _tasks = client
    a = _add(client, title="A")
    b = _add(client, title="B")
    c = _add(client, title="C")
    r = client.post("/api/tasks/reorder",
                    json={"project_id": 1, "ids": [c["id"], a["id"], b["id"]]})
    assert r.status_code == 200
    assert [t["title"] for t in r.json()] == ["C", "A", "B"]
    assert [t["title"] for t in client.get("/api/tasks", params={"project_id": 1}).json()] == \
        ["C", "A", "B"]                       # 顺序真的落库了，不是响应里的临时排序


def test_reorder_ignores_other_projects_tasks(client):
    """界面可能挂着旧项目的清单：跨项目的 id 不能把别人的任务挪过来。"""
    client, _store, tasks = client
    mine = _add(client, title="本项目")
    theirs = tasks.create_task(project_id=2, title="别的项目", sql="SELECT 1")
    r = client.post("/api/tasks/reorder", json={"project_id": 1, "ids": [theirs, mine["id"]]})
    assert [t["id"] for t in r.json()] == [mine["id"]]
    assert tasks.get_task(theirs)["project_id"] == 2   # 未被拖进本项目


# ---------------------------------------------------------------- 4. 确认门
def test_run_requires_explicit_confirmation(client, ds):
    client, _store, tasks = client
    _add(client, title="跑一条", sql="SELECT 1", ds_id=ds["id"])
    stub = _StubExecutor()
    client.app.state.executor = stub
    r = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": False})
    assert r.status_code == 409, r.text
    assert stub.calls == []                   # 没确认就一条都不能碰库
    assert tasks.list_tasks(1)[0]["status"] == "todo"


# ---------------------------------------------------------------- 5. 混合语义
def test_run_skips_tasks_without_sql_and_keeps_them_todo(client, ds):
    client, _store, tasks = client
    manual = _add(client, title="找 DBA 开慢查询日志")     # 没绑 SQL：人自己办
    bound = _add(client, title="验证改写后的行数", sql="SELECT COUNT(*) FROM t", ds_id=ds["id"])
    client.app.state.executor = _StubExecutor()
    r = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": True})
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {i["id"]: i for i in body["items"]}
    assert body["ran_count"] == 1 and body["skipped_count"] == 1
    assert by_id[manual["id"]]["ran"] is False and "未绑定 SQL" in by_id[manual["id"]]["reason"]
    assert by_id[bound["id"]]["status"] == "done"
    assert tasks.get_task(manual["id"])["status"] == "todo"    # 跳过不等于完成


def test_run_skips_already_done_tasks(client, ds):
    client, _store, tasks = client
    done = _add(client, title="已办", sql="SELECT 1", ds_id=ds["id"])
    tasks.update_task(done["id"], status="done")
    stub = _StubExecutor()
    client.app.state.executor = stub
    # 显式勾了一条已完成的：给跳过原因；不带 ids 的统一执行则根本不会碰已完成项
    body = client.post("/api/tasks/run",
                       json={"project_id": 1, "ids": [done["id"]], "confirmed": True}).json()
    assert stub.calls == [] and body["skipped_count"] == 1
    assert "已完成" in body["items"][0]["reason"]

    body2 = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": True}).json()
    assert body2["items"] == [] and stub.calls == []


def test_run_only_picks_selected_ids(client, ds):
    client, _store, _tasks = client
    a = _add(client, title="只跑这条", sql="SELECT 1", ds_id=ds["id"])
    _add(client, title="这条别跑", sql="SELECT 2", ds_id=ds["id"])
    stub = _StubExecutor()
    client.app.state.executor = stub
    client.post("/api/tasks/run", json={"project_id": 1, "ids": [a["id"]], "confirmed": True})
    assert [sql for _, sql in stub.calls] == ["SELECT 1"]


def test_run_with_foreign_or_gone_ids_is_404(client):
    """旧清单点了执行：明确报错，不能静默少跑几条让用户以为都跑了。"""
    client, _store, tasks = client
    other = tasks.create_task(project_id=2, title="别的项目", sql="SELECT 1")
    r = client.post("/api/tasks/run", json={"project_id": 1, "ids": [other], "confirmed": True})
    assert r.status_code == 404 and "刷新清单" in r.json()["detail"]


# ---------------------------------------------------------------- 6. 落库与溯源
def test_run_records_session_with_sql_confirmation_and_result(client, ds):
    client, _store, tasks = client
    row = _add(client, title="订单总量核对", sql="SELECT COUNT(*) FROM t_order",
               ds_id=ds["id"], question="订单总量是多少")
    stub = _StubExecutor()
    client.app.state.executor = stub
    body = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": True}).json()
    item = body["items"][0]
    assert item["session_id"] and item["row_count"] == 1

    sess = tasks.sessions[item["session_id"]]
    assert sess["sql"] == "SELECT COUNT(*) FROM t_order"
    # 批量执行的用户确认必须落进会话：回看历史时不会显示成"未确认就跑了"
    assert sess["sql_confirmed"] is True
    assert sess["query_result"]["rows"][0]["cnt"] == 7
    assert sess["user_question"] == "订单总量是多少"      # 有 question 用 question
    assert sess["project_id"] == 1                      # 会话归到清单所在项目

    after = tasks.get_task(row["id"])
    assert after["status"] == "done"
    assert after["session_id"] == item["session_id"]
    assert after["last_result"]["row_count"] == 1
    assert after["last_result"]["ds_name"] == "业务库"   # 结果带上出处


# ---------------------------------------------------------------- 7. 数据源缺失
def test_run_does_not_fall_back_when_bound_datasource_gone(client):
    """绑定的源被删了：跳过并说明，绝不能悄悄改用当前激活源去查别的库。"""
    client, store, tasks = client
    dead = store.create_datasource(name="将被删除", host="127.0.0.1", database="db1")
    alive = store.create_datasource(name="现在激活的", host="127.0.0.1", database="db2")
    store.set_active(alive["id"])
    row = _add(client, title="原来查 A 库", sql="SELECT 1", ds_id=dead["id"])
    store.delete_datasource(dead["id"])
    stub = _StubExecutor()
    client.app.state.executor = stub
    body = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": True}).json()
    item = body["items"][0]
    assert item["ran"] is False and "已删除" in item["reason"]
    assert stub.calls == []
    assert tasks.get_task(row["id"])["status"] == "todo"


def test_run_without_any_datasource_skips_with_hint(client):
    client, _store, tasks = client
    row = _add(client, title="没源也能建", sql="SELECT 1")   # ds_id 未绑定 → 落到激活源
    store_obj = client.app.state.store
    for d in store_obj.list_datasources():
        store_obj.delete_datasource(d["id"])
    stub = _StubExecutor()
    client.app.state.executor = stub
    body = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": True}).json()
    assert body["items"][0]["ran"] is False and "数据源" in body["items"][0]["reason"]
    assert stub.calls == [] and tasks.get_task(row["id"])["status"] == "todo"


# ---------------------------------------------------------------- 8. 失败与安全门
def test_run_failure_marks_failed_with_sanitized_error(client, ds):
    client, _store, tasks = client
    row = _add(client, title="会挂的一条", sql="SELECT * FROM 不存在", ds_id=ds["id"])
    client.app.state.executor = _StubExecutor(
        error=RuntimeError("查询执行失败：(1146) Table 'db.不存在' doesn't exist "
                           "at 127.0.0.1:3306 user=root"))
    body = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": True}).json()
    assert body["failed_count"] == 1
    assert body["items"][0]["status"] == "failed"
    blob = json.dumps(body, ensure_ascii=False)
    assert "127.0.0.1" not in blob               # 连接串/内网地址不外泄
    assert "root" not in blob
    after = tasks.get_task(row["id"])
    assert after["status"] == "failed" and after["last_result"]["error"]


def test_write_sql_never_executes_even_in_batch(client, ds):
    """任务里塞写库语句：真实安全门（不是桩）必须先拦住，任务标失败。"""
    client, _store, tasks = client
    row = _add(client, title="伪装成任务的删表", sql="DELETE FROM t_order", ds_id=ds["id"])
    # 不换掉 app.state.executor：这里要的就是生产那个 QueryExecutor
    body = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": True}).json()
    item = body["items"][0]
    assert item["status"] == "failed" and "安全校验" in item["error"]
    assert tasks.get_task(row["id"])["status"] == "failed"
    # 会话里的 SQL 保持原样可供审计，但没有任何结果被写入
    sess = tasks.sessions[item["session_id"]]
    assert sess["query_result"] is None


def test_run_caps_batch_size(client, ds):
    client, _store, _tasks = client
    for i in range(51):
        _add(client, title=f"任务{i}", sql="SELECT 1", ds_id=ds["id"])
    stub = _StubExecutor()
    client.app.state.executor = stub
    r = client.post("/api/tasks/run", json={"project_id": 1, "confirmed": True})
    assert r.status_code == 422 and "分批" in r.json()["detail"]
    assert stub.calls == []
