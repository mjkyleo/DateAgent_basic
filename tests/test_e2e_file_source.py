"""端到端：导入 CSV → 提问 → 双层确认 → 执行 → 分析，全程不碰真实 MySQL。

这条链路之前是断的：文件源（SQLite 单库）被当成 MySQL 处理，
模板/提示词都生成 `库`.`表` 形式的引用，SQLite 直接报
"no such table: demo.demo"，导入完提问必失败。
本用例把"导入即可问数"这条主路径钉住。

刻意关掉 LLM（llm_enabled=False）走规则兜底：离线、确定性、不依赖外部服务。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from basic.main import create_app  # noqa: E402
from basic.utils import cache  # noqa: E402

CSV = "月份,平台,订单额\n2025-01,天猫,100\n2025-02,天猫,120\n2025-01,京东,80\n"


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.cache_invalidate()
    yield
    cache.cache_invalidate()


@pytest.fixture
def client(tmp_path):
    app = create_app()
    app.state.settings.llm_enabled = False      # 强制规则兜底：离线且确定
    app.state.settings.store_path = str(tmp_path / "s.db")
    # 上传落盘目录也必须换到临时路径：否则每次跑测试都往真实的 data/uploads/
    # 里漏一个 SQLite 文件（数据源记录在临时库、文件却在真实目录里成孤儿），
    # 目录会越跑越脏。
    app.state.settings.uploads_dir = str(tmp_path / "uploads")
    from basic.storage.local_store import LocalStore
    store = LocalStore(app.state.settings.store_path)
    app.state.store = store
    app.state.sessions = store      # 会话存储：测试同样注入临时库（见 test_confirm_flow 注释）
    try:
        yield TestClient(app)
    finally:
        store.close()


def _upload(client):
    r = client.post("/api/datasource/file",
                    files={"file": ("demo.csv", CSV, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_creates_active_file_datasource(client):
    ds = _upload(client)
    assert ds["type"] == "file"
    assert ds["is_active"] is True          # 导入即激活，不用再手动切


def test_full_round_runs_on_file_source(client):
    """导入 → 意图 → 生成 SQL → 确认 → 执行：每一步都要通。"""
    ds = _upload(client)

    r = client.post("/api/agent/parse_intent", json={
        "ds_id": ds["id"], "user_query": "各平台订单额对比"})
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["session_id"], "必须落 session，否则后面无法确认/执行"
    # 单库来源：SQL 里不能出现 `库`.`表`（SQLite 会当成不存在的表）
    assert "`.`" not in card["sql_preview"]

    r = client.post("/api/agent/generate_sql", json={
        "session_id": card["session_id"], "intent_context": card})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["passed"], f"安全门未通过：{res.get('reason')}"
    assert "`.`" not in res["final_sql"]

    assert client.post("/api/agent/confirm_sql",
                       json={"session_id": card["session_id"]}).status_code == 200

    r = client.post("/api/agent/execute_sql", json={"session_id": card["session_id"]})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["row_count"] == 2, f"按平台聚合应得 2 组，实际：{q['rows']}"
    # 规则兜底模板把聚合列起名为 total；这里按"平台 → 合计"校验，不依赖别名
    got = {row["平台"]: next(v for k, v in row.items() if k != "平台")
           for row in q["rows"]}
    assert got == {"天猫": 220, "京东": 80}


def test_analysis_produces_insights_and_chart(client):
    ds = _upload(client)
    card = client.post("/api/agent/parse_intent", json={
        "ds_id": ds["id"], "user_query": "各平台订单额对比"}).json()
    sid = card["session_id"]
    client.post("/api/agent/generate_sql", json={
        "session_id": sid, "intent_context": card})
    client.post("/api/agent/confirm_sql", json={"session_id": sid})
    query = client.post("/api/agent/execute_sql", json={"session_id": sid}).json()

    r = client.post("/api/agent/analysis", json={
        "query_result": query, "session_id": sid, "intent": card["intent"]})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["suggest_chart"] in ("table", "bar", "line", "pie")
    assert a["summary_text"], "摘要不能为空"
    # 有分组就该给出对比类洞察，且每条都要带依据（"可解释"是硬要求）
    assert a["insights"], "分组结果至少要有一条洞察"
    for ins in a["insights"]:
        assert ins["evidence"]


def test_mysql_style_still_qualifies_tables():
    """反向确认：MySQL 适配器仍然要求库限定名（不能为了修文件源把 MySQL 改坏）。"""
    from basic.core.data_source.base import DataSourceAdapter
    from basic.core.data_source.file_adapter import FileAdapter
    from basic.core.data_source.mysql_adapter import MysqlAdapter

    assert DataSourceAdapter.qualifies_tables is True
    assert MysqlAdapter.qualifies_tables is True
    assert FileAdapter.qualifies_tables is False
