"""写能力界面实测用的沙箱：一个专用库 + 一个专用数据源，测完整套删掉。

为什么不在业务库里练手：界面实测要跑 CREATE/INSERT/UPDATE/DELETE/DROP，
这些动作会留下备份表（_bak_*）。单独开一个库，`down` 一句 DROP DATABASE 就干净了，
业务库里不会多出莫名其妙的备份表，也不用去数"哪些是我造的"。

凭据一律从元数据库解析结果里取（环境变量优先，未配密码时复用已激活的 MySQL 数据源），
脚本不打印、不落盘、不进日志。

用法：
    python -m basic.scripts.write_ui_sandbox up     # 建库建表 + 建数据源 + 切为激活
    python -m basic.scripts.write_ui_sandbox down   # 删数据源 + 删审计行 + DROP 库
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine, text          # noqa: E402

from basic.config.settings import settings          # noqa: E402
from basic.storage.local_store import LocalStore    # noqa: E402
from basic.storage.mysql_store import (MysqlStore,  # noqa: E402
                                       resolve_meta_mysql)

SANDBOX_DB = "agent_write_ui"
SANDBOX_DS = "UI写入演练（沙箱）"
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "_write_ui_sandbox.json"


def _engine():
    from urllib.parse import quote_plus
    meta = resolve_meta_mysql(LocalStore(settings.store_path), settings)
    auth = f"{quote_plus(meta['user'])}:{quote_plus(meta['password'])}" if meta["user"] else ""
    return create_engine(
        f"mysql+pymysql://{auth}@{meta['host']}:{int(meta['port'])}/?charset=utf8mb4",
        pool_pre_ping=True)


def up() -> int:
    store = LocalStore(settings.store_path)
    before = store.get_active()
    eng = _engine()
    with eng.begin() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS `{SANDBOX_DB}`"))
        c.execute(text(f"CREATE DATABASE `{SANDBOX_DB}` "
                       "DEFAULT CHARACTER SET utf8mb4"))
        c.execute(text(f"CREATE TABLE `{SANDBOX_DB}`.`ui_order` ("
                       "id INT PRIMARY KEY AUTO_INCREMENT, city VARCHAR(32) NOT NULL, "
                       "amount DECIMAL(10,2) NOT NULL)"))
        c.execute(text(f"INSERT INTO `{SANDBOX_DB}`.`ui_order` (city, amount) VALUES "
                       "('上海', 100.00), ('北京', 200.00), ('广州', 300.00)"))
    meta = resolve_meta_mysql(store, settings)
    ds = store.create_datasource(
        name=SANDBOX_DS, type="mysql", host=meta["host"], port=int(meta["port"]),
        user=meta["user"], password=meta["password"], database=SANDBOX_DB,
        allowed_ops="write_ddl")
    ds_id = int(ds["id"])
    store.set_active(ds_id)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(
        {"ds_id": ds_id, "prev_active_id": int(before["id"]) if before else None},
        ensure_ascii=False), encoding="utf-8")
    print(f"沙箱就绪：库 {SANDBOX_DB} · 数据源 #{ds_id}（上限 write_ddl，已切为激活）")
    print("  表 ui_order：3 行（上海/北京/广州）")
    return 0


def down() -> int:
    store = LocalStore(settings.store_path)
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    rows = [d for d in store.list_datasources()
            if d.get("name") == SANDBOX_DS or int(state.get("ds_id") or 0) == int(d.get("id") or -1)]
    for d in rows:
        store.delete_datasource(int(d["id"]))
        print(f"  已删数据源 #{d['id']}「{d['name']}」")
    prev = state.get("prev_active_id")
    if prev and store.get_datasource(int(prev)):
        store.set_active(int(prev))
        print(f"  激活源已还原为 #{prev}")

    eng = _engine()
    with eng.begin() as c:
        left = c.execute(text(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA='{SANDBOX_DB}'")).fetchall()
        for schema, name in left:
            print(f"  库里还剩：{schema}.{name}")
        c.execute(text(f"DROP DATABASE IF EXISTS `{SANDBOX_DB}`"))
    print(f"  已 DROP DATABASE {SANDBOX_DB}")

    # 界面实测落下的审计行属于沙箱库，一并清掉（元数据库不可用时跳过，不阻断清理）
    try:
        ms = MysqlStore(**resolve_meta_mysql(store, settings))
        with ms._engine.begin() as c:            # noqa: SLF001
            n = c.execute(text("DELETE FROM sql_audit WHERE target_db=:db"),
                          {"db": SANDBOX_DB}).rowcount
        print(f"  已清理沙箱审计行 {n} 条")
        ms.close()
    except Exception as e:  # noqa: BLE001
        print(f"  审计清理跳过（元数据库不可用）：{type(e).__name__}")
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    with eng.connect() as c:
        gone = c.execute(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                              f"WHERE SCHEMA_NAME='{SANDBOX_DB}'")).fetchall()
    print("沙箱已清空" if not gone else "警告：库还在，请手工确认")
    return 0


if __name__ == "__main__":
    sys.exit(up() if (sys.argv[1:] or ["up"])[0] == "up" else down())
