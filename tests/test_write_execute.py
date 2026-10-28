"""写通道执行与审计的回归测试（P0：执行器 + 接口层，不连真实业务库）。

覆盖的是"顺序"和"失败朝哪一侧失败"，不是语法：

- 顺序硬约束：判档位 → 估影响 → 备份 → 执行。备份没成功，主语句绝不能碰库；
- 上限按**执行时**重新估的数字判，不拿预览时那份旧数字放行；
- 预估拿不到数 = 拒（等于让人盲签），不是"放行但少个数字"；
- 总闸关 / 源是只读 / 档位不够 / 破坏性没输表名 → 四条路都得拒，且库上一个字节没变；
- 文件源（SQLite mode=ro）从适配器层就拒绝写；
- 回滚只是给人看的文本，系统不代跑（审计里能看到，写入序列里不能出现）；
- 接口层：未确认 409、安检拒绝 403、引擎失败 500，三条都要落审计行；
  审计存储不可用（元数据库降级）时直接 503，连执行器都不进。

执行器用假适配器（记录每一次 execute_write/simple_query），接口层用真执行器 +
monkeypatch 的 build_adapter，所以这四层约束是被真代码判出来的，不是桩替我判的。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from basic.config.settings import Settings  # noqa: E402
from basic.core.data_source.base import build_adapter  # noqa: E402
from basic.core.query_executor import QueryExecutor  # noqa: E402
from basic.main import create_app  # noqa: E402
from basic.storage.local_store import LocalStore  # noqa: E402

DB = "biz"
DEL_SQL = "DELETE FROM t_order WHERE id=1"
UPD_SQL = "UPDATE t_order SET amount=0 WHERE id<10"


class FakeAdapter:
    """假业务库：把每次读/写调用原样记下来，顺序与次数就是断言对象。"""

    supports_write = True

    def __init__(self, *, count: int = 3, fail_backup: bool = False,
                 fail_estimate: bool = False) -> None:
        self.count = count
        self.fail_backup = fail_backup
        self.fail_estimate = fail_estimate
        self.reads: List[str] = []
        self.writes: List[str] = []
        self.closed = 0

    def qualified_name(self, db_name: str, table_name: str) -> str:
        return f"`{db_name}`.`{table_name}`"

    def simple_query(self, sql: str, timeout: int, max_rows: int) -> Dict[str, Any]:
        self.reads.append(sql)
        if self.fail_estimate:
            raise RuntimeError("boom: can't reach server 10.0.0.1:3306 password=secret")
        return {"columns": ["c"], "rows": [{"c": self.count}], "row_count": 1,
                "cost_ms": 1, "truncated": False}

    def execute_write(self, sql: str, *, timeout: int = 10) -> Dict[str, Any]:
        if self.fail_backup and sql.lstrip().upper().startswith("CREATE TABLE"):
            raise RuntimeError("backup failed: disk full")
        self.writes.append(sql)
        return {"affected_rows": self.count, "cost_ms": 5}

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    s.allow_writes = True
    s.write_max_affected_rows = 5000
    s.write_auto_backup = True
    return s


def make_executor(settings: Settings, adapter: FakeAdapter, monkeypatch) -> QueryExecutor:
    monkeypatch.setattr("basic.core.query_executor.build_adapter",
                        lambda ds: adapter)
    return QueryExecutor(settings)


def ds_dict(allowed_ops: str = "write_ddl") -> Dict[str, Any]:
    return {"id": 1, "name": "业务库", "type": "mysql", "database": DB,
            "allowed_ops": allowed_ops, "host": "127.0.0.1", "user": "u",
            "password": "p"}


# ============================================================ 执行器：顺序与中止
def test_estimate_then_backup_then_execute_in_order(settings, monkeypatch):
    adapter = FakeAdapter(count=7)
    ex = make_executor(settings, adapter, monkeypatch)
    out = ex.execute_write(ds=ds_dict(), db=DB, sql=UPD_SQL,
                           tier="write_dml", confirmed=True)
    assert adapter.reads == [f"SELECT COUNT(*) FROM `{DB}`.`t_order` WHERE id < 10"]
    assert len(adapter.writes) == 2
    assert adapter.writes[0].startswith(f"CREATE TABLE `{DB}`.`_bak_t_order_")
    assert adapter.writes[1] == UPD_SQL
    assert out["estimated_rows"] == 7 and out["affected_rows"] == 7
    assert out["backup_table"].startswith("_bak_t_order_")


def test_unconfirmed_never_touches_the_database(settings, monkeypatch):
    """反向验证的正面：确认位是唯一由人给的开关，没它连预估都不该跑。"""
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError):
        ex.execute_write(ds=ds_dict(), db=DB, sql=DEL_SQL,
                         tier="write_dml", confirmed=False)
    assert adapter.writes == [] and adapter.reads == []


def test_backup_failure_aborts_before_main_statement(settings, monkeypatch):
    """备份失败必须中止：宁可什么都写不进去，也不能出现"改了但没后悔药"。"""
    adapter = FakeAdapter(count=3, fail_backup=True)
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(RuntimeError, match="备份表创建失败"):
        ex.execute_write(ds=ds_dict(), db=DB, sql=DEL_SQL,
                         tier="write_dml", confirmed=True)
    assert adapter.writes == []          # 主语句一次都没执行


def test_estimate_failure_aborts_write(settings, monkeypatch):
    """预估失败 = 这条语句本身跑不通（表不存在/列写错），更不能盲写。"""
    adapter = FakeAdapter(fail_estimate=True)
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError, match="预估失败"):
        ex.execute_write(ds=ds_dict(), db=DB, sql=DEL_SQL,
                         tier="write_dml", confirmed=True)
    assert adapter.writes == []


def test_estimate_failure_message_is_sanitized(settings, monkeypatch):
    adapter = FakeAdapter(fail_estimate=True)
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError) as e:
        ex.execute_write(ds=ds_dict(), db=DB, sql=DEL_SQL,
                         tier="write_dml", confirmed=True)
    assert "secret" not in str(e.value) and "10.0.0.1" not in str(e.value)


def test_over_cap_uses_fresh_estimate_not_preview_number(settings, monkeypatch):
    """预览时说 3 行、执行时已经变成 9999 行：按新的数字拒，不能拿旧数字放行。"""
    adapter = FakeAdapter(count=9999)
    settings.write_max_affected_rows = 100
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError, match="超过允许上限"):
        ex.execute_write(ds=ds_dict(), db=DB, sql=DEL_SQL,
                         tier="write_dml", confirmed=True)
    assert adapter.writes == []


def test_create_table_needs_no_estimate_or_backup(settings, monkeypatch):
    """建表没有"受影响存量行"，也就不需要备份；直接执行。"""
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    out = ex.execute_write(ds=ds_dict(), db=DB, sql="CREATE TABLE t_new (id INT)",
                           tier="write_ddl", confirmed=True)
    assert adapter.reads == [] and adapter.writes == ["CREATE TABLE t_new (id INT)"]
    assert out["backup_table"] is None


def test_destructive_requires_typed_table_name(settings, monkeypatch):
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError, match="表名确认"):
        ex.execute_write(ds=ds_dict(), db=DB, sql="DROP TABLE t_old",
                         tier="write_ddl", confirmed=True, confirm_phrase="")
    assert adapter.writes == []
    out = ex.execute_write(ds=ds_dict(), db=DB, sql="DROP TABLE t_old",
                           tier="write_ddl", confirmed=True, confirm_phrase="t_old")
    assert out["destructive"] is True
    assert out["backup_table"].startswith("_bak_t_old_")
    assert adapter.writes[0].startswith(f"CREATE TABLE `{DB}`.`_bak_t_old_")
    assert adapter.writes[1] == "DROP TABLE t_old"


def test_wrong_phrase_still_rejected(settings, monkeypatch):
    """确认词输错一个字符也算没确认 —— 它是"我知道这张表没了"的签名。"""
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError):
        ex.execute_write(ds=ds_dict(), db=DB, sql="TRUNCATE TABLE t_old",
                         tier="write_ddl", confirmed=True, confirm_phrase="t_ol")
    assert adapter.writes == []


def test_rollback_hint_is_returned_but_never_executed(settings, monkeypatch):
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    out = ex.execute_write(ds=ds_dict(), db=DB, sql=UPD_SQL,
                           tier="write_dml", confirmed=True)
    assert "REPLACE INTO" in out["rollback_sql"]
    assert not any("REPLACE INTO" in s for s in adapter.writes)


def test_prepare_write_reads_only_never_writes(settings, monkeypatch):
    adapter = FakeAdapter(count=4)
    ex = make_executor(settings, adapter, monkeypatch)
    plan = ex.prepare_write(ds=ds_dict(), db=DB, sql=DEL_SQL, tier="write_dml")
    assert plan["passed"] and plan["estimated_rows"] == 4
    assert adapter.writes == []
    assert plan["max_allowed_rows"] == settings.write_max_affected_rows


# ============================================================ 四层约束
def test_master_switch_off_blocks_write_on_a_writable_source(settings, monkeypatch):
    """源被标成可写 ≠ 能写：总闸在代码之外，公开仓库里的代码不等于线上开着闸。"""
    settings.allow_writes = False
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError, match="只读"):
        ex.execute_write(ds=ds_dict("write_ddl"), db=DB, sql=DEL_SQL,
                         tier="write_dml", confirmed=True)
    assert adapter.writes == []


def test_read_only_source_ceiling_blocks_before_planning(settings, monkeypatch):
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError, match="只读源"):
        ex.execute_write(ds=ds_dict("read"), db=DB, sql=DEL_SQL,
                         tier="write_ddl", confirmed=True)
    assert adapter.writes == []


def test_source_ceiling_caps_the_requested_tier(settings, monkeypatch):
    """源上限 write_dml 时，请求 write_ddl 也抬不高：DROP 会被按 write_dml 拒。"""
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError) as e:
        ex.execute_write(ds=ds_dict("write_dml"), db=DB, sql="DROP TABLE t_old",
                         tier="write_ddl", confirmed=True, confirm_phrase="t_old")
    assert "write_ddl" in str(e.value) and adapter.writes == []


def test_write_to_meta_database_rejected(settings, monkeypatch):
    """业务库与元数据库常是同一账号同一实例：不硬挡就等于允许 Agent 删自己的会话表。"""
    adapter = FakeAdapter()
    ex = make_executor(settings, adapter, monkeypatch)
    with pytest.raises(PermissionError, match="元数据库|系统库"):
        ex.execute_write(ds=ds_dict(), db=settings.mysql_database, sql="DROP TABLE sessions",
                         tier="write_ddl", confirmed=True, confirm_phrase="sessions")
    assert adapter.writes == []


def test_file_source_refuses_writes_from_the_adapter_layer(tmp_path):
    """SQLite 以 mode=ro 打开，写能力在适配器上就是 False —— 与档位无关。"""
    p = tmp_path / "demo.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (id INT)")
    con.commit()
    con.close()
    adapter = build_adapter({"type": "file", "file_path": str(p)})
    assert adapter.supports_write is False
    with pytest.raises(PermissionError):
        adapter.execute_write("DELETE FROM t WHERE id=1")
    adapter.close()


# ============================================================ 接口层
class FakeAuditStore:
    """假元数据库：只实现审计与会话接口用到的方法。"""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self._id = 0

    def add_audit(self, **f: Any) -> int:
        self._id += 1
        self.rows.append({"id": self._id, **f})
        return self._id

    def list_audits(self, *, limit: int = 50, ds_id=None, session_id=None,
                    status=None) -> List[Dict[str, Any]]:
        out = list(reversed(self.rows))       # 与生产同一契约：新→旧
        if ds_id is not None:
            out = [r for r in out if r.get("ds_id") == ds_id]
        if session_id:
            out = [r for r in out if r.get("session_id") == session_id]
        if status:
            out = [r for r in out if r.get("status") == status]
        return [{"id": r["id"], "session_id": r.get("session_id", ""),
                 "ds_id": r.get("ds_id", 0), "ds_name": r.get("ds_name", ""),
                 "target_db": r.get("target_db", ""),
                 "statement_kind": r.get("statement_kind", ""),
                 "sql": r.get("sql", ""), "requested_tier": r.get("requested_tier", ""),
                 "effective_tier": r.get("effective_tier", ""),
                 "confirmed": bool(r.get("confirmed")),
                 "destructive": bool(r.get("destructive")),
                 "estimated_rows": r.get("estimated_rows"),
                 "affected_rows": r.get("affected_rows"),
                 "backup_table": r.get("backup_table", ""),
                 "rollback_sql": r.get("rollback_sql", ""),
                 "status": r.get("status", "executed"), "error": r.get("error", ""),
                 "cost_ms": r.get("cost_ms", 0),
                 "created_at": "2026-01-01T00:00:00"} for r in out[:limit]]


@pytest.fixture
def app(tmp_path, monkeypatch):
    """真接口 + 真执行器 + 假适配器 + 假审计存储（全程不连业务库）。

    不加 TestClient 的上下文管理器：那会跑 lifespan，把这里的假存储换回真的
    SQLite/MySQL，测试就会写进开发库的 sql_audit 表（踩过）。
    """
    client_app = create_app()
    local = LocalStore(str(tmp_path / "s.db"))
    client_app.state.store = local
    audit = FakeAuditStore()
    client_app.state.sessions = audit
    monkeypatch.setattr(client_app.state.settings, "allow_writes", True)
    adapter = FakeAdapter(count=2)
    monkeypatch.setattr("basic.core.query_executor.build_adapter", lambda ds: adapter)
    client_app.state.executor = QueryExecutor(client_app.state.settings)
    ds = local.create_datasource(name="业务库", host="127.0.0.1", database=DB,
                                 allowed_ops="write_ddl")
    try:
        yield TestClient(client_app), local, audit, adapter, ds
    finally:
        local.close()


def _body(ds, **over):
    return {"ds_id": ds["id"], "db": DB, "sql": DEL_SQL, "tier": "write_dml",
            "confirmed": True, **over}


def test_api_prepare_returns_plan_without_writing(app):
    c, _l, _a, adapter, ds = app
    r = c.post("/api/agent/prepare_write",
               json={"ds_id": ds["id"], "db": DB, "sql": DEL_SQL, "tier": "write_dml"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] and body["estimated_rows"] == 2
    assert body["confirm_phrase"] == "" and body["backup_table"].startswith("_bak_")
    assert adapter.writes == []


def test_api_prepare_rejection_is_audited(app):
    """越权试探多半止步于预览：只记执行阶段的拒绝，等于把试探从审计里抹掉。"""
    c, _l, audit, adapter, ds = app
    r = c.post("/api/agent/prepare_write",
               json={"ds_id": ds["id"], "db": DB, "sql": UPD_SQL, "tier": "read"})
    assert r.status_code == 403, r.text
    assert adapter.writes == []
    row = audit.rows[-1]
    assert row["status"] == "rejected" and "档位" in row["error"]
    assert row["sql"] == UPD_SQL and row["requested_tier"] == "read"


def test_api_prepare_rejection_survives_missing_audit_store(tmp_path, monkeypatch):
    """预览一个字节都不改：审计存储不可用时它照样报 403（不是 503），只是没留痕。
    真要写的时候 execute_write 仍然硬要求审计存储。"""
    client_app = create_app()
    local = LocalStore(str(tmp_path / "s.db"))
    client_app.state.store = local
    client_app.state.sessions = object()      # 没有 add_audit 的存储 = 降级
    monkeypatch.setattr(client_app.state.settings, "allow_writes", True)
    adapter = FakeAdapter()
    monkeypatch.setattr("basic.core.query_executor.build_adapter", lambda ds: adapter)
    client_app.state.executor = QueryExecutor(client_app.state.settings)
    ds = local.create_datasource(name="业务库", host="127.0.0.1", database=DB,
                                 allowed_ops="write_ddl")
    c = TestClient(client_app)
    r = c.post("/api/agent/prepare_write",
               json={"ds_id": ds["id"], "db": DB, "sql": UPD_SQL, "tier": "read"})
    assert r.status_code == 403, r.text
    assert adapter.writes == []
    local.close()


def test_api_execute_records_effective_tier_when_source_caps_it(app):
    """审计要记"实际按哪个档位执行"：只看请求档位会把一次降级执行记成全权执行。"""
    c, local, audit, _adapter, _ds = app
    capped = local.create_datasource(name="只能改数据", host="127.0.0.1", database=DB,
                                     allowed_ops="write_dml")
    r = c.post("/api/agent/execute_write", json=_body(capped, tier="write_ddl"))
    assert r.status_code == 200, r.text
    assert r.json()["effective_tier"] == "write_dml"
    assert audit.rows[-1]["requested_tier"] == "write_ddl"
    assert audit.rows[-1]["effective_tier"] == "write_dml"


def test_api_execute_requires_confirmation_with_409(app):
    """未确认 = 409，与只读通道同一语义；且这条尝试要进审计（谁想绕过界面）。"""
    c, _l, audit, adapter, ds = app
    r = c.post("/api/agent/execute_write", json=_body(ds, confirmed=False))
    assert r.status_code == 409, r.text
    assert adapter.writes == []
    assert audit.rows[-1]["status"] == "rejected"


def test_api_execute_happy_path_records_executed_audit(app):
    c, _l, audit, _adapter, ds = app
    r = c.post("/api/agent/execute_write", json=_body(ds))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected_rows"] == 2 and body["audit_id"] == audit.rows[-1]["id"]
    row = audit.rows[-1]
    assert row["status"] == "executed" and row["affected_rows"] == 2
    assert row["ds_id"] == ds["id"] and row["target_db"] == DB
    assert row["statement_kind"] == "delete"


def test_api_tier_below_requirement_is_403_and_audited(app):
    c, _l, audit, adapter, ds = app
    r = c.post("/api/agent/execute_write",
               json=_body(ds, sql=UPD_SQL, tier="read"))
    assert r.status_code == 403, r.text
    assert adapter.writes == []
    assert audit.rows[-1]["status"] == "rejected" and "档位" in audit.rows[-1]["error"]


def test_api_engine_failure_is_500_audited_and_sanitized(app, monkeypatch):
    c, _l, audit, _adapter, ds = app
    boom = FakeAdapter(count=2)
    boom.fail_backup = True
    monkeypatch.setattr("basic.core.query_executor.build_adapter", lambda cfg: boom)
    r = c.post("/api/agent/execute_write", json=_body(ds))
    assert r.status_code == 500, r.text
    assert "disk full" in r.text and "10.0.0.1" not in r.text
    assert audit.rows[-1]["status"] == "failed" and "disk full" in audit.rows[-1]["error"]


def test_api_audit_failed_status_written(app):
    c, _l, audit, _adapter, ds = app
    # 越界档位被拒后，审计里三态分得开：executed / rejected 各一条
    c.post("/api/agent/execute_write", json=_body(ds))
    c.post("/api/agent/execute_write", json=_body(ds, sql=UPD_SQL, tier="read"))
    assert [r["status"] for r in audit.rows] == ["executed", "rejected"]


def test_api_write_rejected_when_audit_store_unavailable(tmp_path, monkeypatch):
    """元数据库降级（没有 sql_audit 表）时必须 503：改完数据查不到是谁改的，
    比不改更危险。执行器也不能被推进去。"""
    client_app = create_app()
    local = LocalStore(str(tmp_path / "s.db"))
    client_app.state.store = local
    client_app.state.sessions = object()      # 没有 add_audit 的存储 = 降级
    monkeypatch.setattr(client_app.state.settings, "allow_writes", True)
    adapter = FakeAdapter()
    monkeypatch.setattr("basic.core.query_executor.build_adapter", lambda ds: adapter)
    client_app.state.executor = QueryExecutor(client_app.state.settings)
    ds = local.create_datasource(name="业务库", host="127.0.0.1", database=DB,
                                 allowed_ops="write_ddl")
    c = TestClient(client_app)
    r = c.post("/api/agent/execute_write", json=_body(ds))
    assert r.status_code == 503, r.text
    assert adapter.writes == []
    local.close()


def test_api_destructive_without_phrase_is_403(app):
    c, _l, _audit, adapter, ds = app
    r = c.post("/api/agent/execute_write",
               json=_body(ds, sql="DROP TABLE t_old", tier="write_ddl",
                          confirm_phrase=""))
    assert r.status_code == 403, r.text
    assert adapter.writes == []


def test_file_datasource_cannot_be_marked_writable(app):
    """文件源（CSV 落成的 SQLite）以 mode=ro 打开，物理上写不了。
    接口必须明确 400，不能静默忽略 —— 静默会让人以为界面上已经开了写权限。"""
    c, local, _a, _adapter, _ds = app
    f = local.create_datasource(name="上传的表", type="file",
                                file_path="data/uploads/x.db", database="x")
    r = c.put(f"/api/datasource/{f['id']}", json={"allowed_ops": "write_dml"})
    assert r.status_code == 400, r.text
    assert "只读" in r.text
    assert local.get_datasource(f["id"])["allowed_ops"] == "read"


def test_master_gate_defaults_to_off(monkeypatch):
    """默认值必须是关：这条钉的是"代码进了公开仓库 ≠ 线上能写"。"""
    monkeypatch.delenv("AGENT_ALLOW_WRITES", raising=False)
    fresh = Settings()
    assert fresh.allow_writes is False
    assert fresh.perm_tiers == ("read", "write_dml", "write_ddl")
    assert fresh.mysql_database.lower() in fresh.protected_databases


def test_api_audit_list_filterable(app):
    c, _l, _a, _adapter, ds = app
    c.post("/api/agent/execute_write", json=_body(ds))
    c.post("/api/agent/execute_write", json=_body(ds, sql=UPD_SQL))
    r = c.get("/api/agent/write_audit", params={"ds_id": ds["id"], "limit": 10})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2 and rows[0]["id"] > rows[1]["id"]
    assert c.get("/api/agent/write_audit", params={"status": "nope"}).json() == []
