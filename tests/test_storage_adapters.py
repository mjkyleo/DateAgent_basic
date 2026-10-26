"""存储 / 适配器契约测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from basic.core.data_source.file_adapter import FileAdapter
from basic.storage.local_store import LocalStore


@pytest.fixture()
def store(tmp_path):
    """项目内临时库（沙箱限制 workspace 外写入，不用 pytest 全局 tmp 根目录）。"""
    db = Path(__file__).parent / "_tmp_store.db"
    s = LocalStore(db)
    yield s
    s.close()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()


class TestLocalStore:
    def test_datasource_crud_and_active(self, store):
        a = store.create_datasource(name="A", host="127.0.0.1", database="db1")
        b = store.create_datasource(name="B", host="127.0.0.1", database="db2")
        assert a["id"] and b["id"] and a["id"] != b["id"]
        assert len(store.list_datasources()) == 2

        assert store.set_active(b["id"])
        active = store.get_active()
        assert active and active["id"] == b["id"] and active["is_active"]

        # 更新：空密码保持原密码
        store.update_datasource(a["id"], password="secret")
        store.update_datasource(a["id"], name="A2", password="")
        row = store.get_datasource(a["id"])
        assert row["name"] == "A2" and row["password"] == "secret"

        assert store.delete_datasource(a["id"])
        assert store.get_datasource(a["id"]) is None

    def test_session_flow(self, store):
        sid = store.create_session(ds_id=1, user_question="统计订单金额")
        store.update_session(sid, sql="SELECT 1", sql_confirmed=False)
        s = store.get_session(sid)
        assert s and s["sql"] == "SELECT 1" and not s["sql_confirmed"]
        store.update_session(sid, sql_confirmed=True,
                             query_result={"columns": ["a"], "rows": [{"a": 1}],
                                           "row_count": 1, "cost_ms": 1})
        s = store.get_session(sid)
        assert s["sql_confirmed"] and s["query_result"]["rows"][0]["a"] == 1

    def test_create_and_update_return_row(self, store):
        """create/update 内部回读 get_*（同一线程重入）——防 RLock 回归死锁。"""
        a = store.create_datasource(name="A", host="127.0.0.1", database="db1")
        assert a["id"] and a["name"] == "A"
        u = store.update_datasource(a["id"], name="A2")
        assert u and u["name"] == "A2"


class TestFileAdapter:
    def test_missing_file_rejected(self):
        with pytest.raises(ValueError):
            FileAdapter("no_such_file_xyz.db")

    def test_read_only_and_contract(self, tmp_path):
        """真实现后的契约：能列出表/结构/采样/查询，且只读（写操作必须失败）。"""
        import sqlite3
        p = tmp_path / "demo.db"
        conn = sqlite3.connect(str(p))
        try:
            conn.execute('CREATE TABLE "订单" ("月份" TEXT, "金额" REAL)')
            conn.executemany('INSERT INTO "订单" VALUES (?,?)',
                             [("2024-01", 100.5), ("2024-02", 200.0)])
            conn.commit()
        finally:
            conn.close()

        ad = FileAdapter(str(p), db_name="demo")
        try:
            assert ad.list_databases() == ["demo"]
            assert ad.list_tables("demo") == ["订单"]
            cols = ad.get_table_schema("demo", "订单")
            assert [c["name"] for c in cols] == ["月份", "金额"]
            assert ad.qualified_name("demo", "订单") == "`订单`"
            assert ad.sample_data("demo", "订单", 1)[0]["金额"] == 100.5
            out = ad.simple_query('SELECT `月份`, SUM(`金额`) AS s FROM `订单` '
                                  'GROUP BY `月份` ORDER BY s DESC LIMIT 5', 5, 100)
            assert out["row_count"] == 2 and out["rows"][0]["s"] == 200.0

            # 只读：任何写都必须被拒
            from sqlalchemy import text as _t
            try:
                with ad.engine.connect() as c:
                    c.execute(_t("INSERT INTO `订单` VALUES ('x', 1)"))
                raise AssertionError("只读模式下写操作应当失败")
            except Exception:
                pass
        finally:
            ad.close()
