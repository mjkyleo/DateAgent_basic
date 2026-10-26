"""内容画像事实层测试（P0：让阶段①/② 讲得出"数据里真实的数字"）。

覆盖的都是"不加就会静默坏掉"的约束：
- 列注释进 prompt，但不破坏 llm_intent 规则兜底对 Schema 文本的解析；
- 内容事实的形状（时间跨度/维度高频值/度量范围/空值率）；
- 敏感列只给统计、绝不给取值（min/max/频次都不行）；
- 行数超预算时跳过列级聚合 —— 提问路径不能被 COUNT(DISTINCT) 拖死；
- 画像失败只降级为"仅结构"，不让第一步或提问失败；
- table_stats 的缓存只在显式 use_cache + 数据源作用域时生效；
- 聚合结果真的喂给了 advise_table（否则 high_null_low_cardinality 永远不响）；
- build_profile 的 phenomena（无 LLM 走规则版、有 LLM 走透传）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from basic.core import source_insight as si  # noqa: E402
from basic.core.llm import ModelConfig  # noqa: E402
from basic.core.llm_intent import _parse_schema_text, _parse_schema_types  # noqa: E402
from basic.core.meta_tools import (PROMPT_MAX_ROWS, SENSITIVE_RE, MetaTool,  # noqa: E402
                                   _shape_fact)
from basic.config.settings import Settings  # noqa: E402
from basic.core.data_source.base import build_adapter  # noqa: E402

PHONE_SAMPLE = "13800000007"


def _make_db(path: Path) -> None:
    """一张"像业务"的表：4 城市、APP/H5 两渠道、手机号、金额、高空值备注、时间列。"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t_order (id INTEGER PRIMARY KEY, 城市 TEXT, 渠道 TEXT, "
                "手机号 TEXT, 订单额 REAL, 备注 TEXT, 下单时间 DATETIME)")
    cities = ["武汉"] * 8 + ["北京"] * 6 + ["上海"] * 4 + ["广州"] * 2
    for i, city in enumerate(cities):
        con.execute(
            "INSERT INTO t_order (id, 城市, 渠道, 手机号, 订单额, 备注, 下单时间) "
            "VALUES (?,?,?,?,?,?,?)",
            (i + 1, city, "APP" if i < 15 else "H5", f"1380000{i:04d}",
             100.0 * (i + 1), None if i < 12 else ["正常", "退款"][i % 2],
             f"2024-{(i % 9) + 1:02d}-{(i % 28) + 1:02d} 09:00:00"))
    con.commit()
    con.close()


@pytest.fixture
def orders(tmp_path, request):
    path = tmp_path / "orders.db"
    _make_db(path)
    ds = {"type": "file", "file_path": str(path), "database": "demo"}
    # 作用域按测试名分开：缓存是进程级的，共用作用域会让上一个测试写下的结果污染下一个
    tool = MetaTool(build_adapter(ds), Settings(), cache_scope=f"pf-{request.node.name}")
    yield tool
    tool.close()


# ---------------------------------------------------------------- 事实形状
def test_content_facts_shape(orders):
    facts = orders.content_facts("demo", ["t_order"])
    assert len(facts) == 1
    f = facts[0]
    assert f["row_count"] == 20
    assert f["time_range"]["column"] == "下单时间"
    assert str(f["time_range"]["start"]).startswith("2024-01")

    dims = {d["column"]: d for d in f["dimensions"]}
    assert dims["城市"]["distinct"] == 4
    top = dims["城市"]["top"]
    assert len(top) <= 5 and top[0]["value"] == "武汉" and top[0]["count"] == 8
    assert set(dims) <= {"城市", "渠道", "备注"}, "主键/时间/金额不该混进维度"

    meas = {m["column"]: m for m in f["measures"]}
    assert meas["订单额"]["min"] == 100.0 and meas["订单额"]["avg"] == 1050.0
    assert "id" not in meas, "主键的 min/max 是序号统计，不是业务度量"

    assert [n["column"] for n in f["null_heavy"]] == ["备注"]
    assert f["null_heavy"][0]["ratio"] == 0.6


def test_numeric_columns_really_get_aggregated(orders):
    """AVG 拼接少了个逗号时，SQLite/MySQL 都报语法错，数值列会全变成"无法统计" ——
    表现不是报错，而是度量清单静默为空、第一步永远讲不出金额。"""
    stats = orders.table_stats("demo", "t_order")
    bad = [c["column"] for c in stats["columns"] if c.get("error")]
    assert bad == []
    amount = next(c for c in stats["columns"] if c["column"] == "订单额")
    assert amount["avg_value"] == 1050
    assert amount["min_value"] == 100.0 and amount["max_value"] == 2000.0


def test_null_not_counted_as_a_top_value(orders):
    """空值率已经单独报了，高频取值里再出现 None 会诱导模型写 = 'None'。"""
    cols = orders.adapter.get_table_schema("demo", "t_order")
    fact = orders.table_fact("t_order", cols, orders.table_stats("demo", "t_order"))
    dims = {d["column"]: d for d in fact["dimensions"]}
    assert "备注" in dims
    assert all(v["value"] is not None for d in dims.values() for v in d["top"])


def test_facts_text_is_prompt_safe_for_rule_parser(orders):
    """事实块不能长得像"字段清单"：规则兜底会把 `- ` 行当列名解析。"""
    text = MetaTool.facts_text(orders.content_facts("demo", ["t_order"]))
    assert text and "表 t_order" in text
    assert not any(line.strip().startswith("- ") for line in text.splitlines())


# ---------------------------------------------------------------- 敏感列
def test_sensitive_column_masked_in_stats(orders):
    stats = orders.table_stats("demo", "t_order")
    phone = next(c for c in stats["columns"] if c["column"] == "手机号")
    assert phone["masked"] is True
    assert phone["min_value"] is None and phone["max_value"] is None
    assert phone["top_values"] == []
    assert phone["distinct_count"] == 20          # 统计量保留，只是不给内容
    assert SENSITIVE_RE.search("手机号")


@pytest.mark.parametrize("name", ["phone", "user_email", "身份证", "家庭地址", "客户姓名", "password"])
def test_sensitive_regex_covers_the_usual_fields(name):
    assert SENSITIVE_RE.search(name), name


def test_no_sensitive_values_anywhere_in_prompt(orders):
    text = orders.schema_context("demo", ["t_order"])
    assert PHONE_SAMPLE not in text                       # 真实号码一个字符都不能出现
    assert "手机号" in text                               # 列名与"被脱敏"这件事要说明
    assert "含敏感列" in text
    amounts = "100.0"                                     # 度量范围是允许的（聚合事实）
    assert amounts in text


# ---------------------------------------------------------------- 预算与降级
def test_row_budget_skips_column_aggregation(orders, monkeypatch):
    calls: List[str] = []
    monkeypatch.setattr(MetaTool, "table_row_estimate",
                        lambda self, db, t: PROMPT_MAX_ROWS + 1)
    monkeypatch.setattr(MetaTool, "_collect_stats",
                        lambda self, db, t, col_cap, top_limit: calls.append(t) or {})
    facts = orders.content_facts("demo", ["t_order"])
    assert calls == [], "超过预算的表不能做列级聚合"
    assert "skipped" in facts[0] and facts[0]["row_count"] == PROMPT_MAX_ROWS + 1
    assert MetaTool.facts_text(facts).startswith("表 t_order")


def test_profiling_failure_degrades_to_structure_only(orders, monkeypatch):
    def _boom(self, db, t, col_cap, top_limit):
        raise RuntimeError("information_schema 挂了")
    monkeypatch.setattr(MetaTool, "_collect_stats", _boom)
    seen: List[Any] = []
    monkeypatch.setattr(si, "advise_table",
                        lambda meta, stats=None: seen.append(stats) or [])

    assert orders.content_facts("demo", ["t_order"]) == []
    text = orders.schema_context("demo", ["t_order"])
    assert "## 表 t_order" in text                        # 结构层照常给模型
    assert "【数据内容事实】" not in text                  # 内容层缺失就整段不出现

    profiles = si.collect(orders, "demo", ["t_order"])
    assert profiles and profiles[0].fact == {}
    assert profiles[0].columns, "画像失败不影响表结构"
    assert seen == [None], "规则层要收到 stats=None，而不是根本没被调用"


# ---------------------------------------------------------------- 缓存
def test_table_stats_cached_only_when_asked(orders, monkeypatch):
    orders.settings.meta_cache_ttl_s = 30
    n: List[int] = []

    def _spy(self, db, t, col_cap, top_limit):
        n.append(1)
        return {"database": db, "table": t, "row_count": 20, "columns": []}
    monkeypatch.setattr(MetaTool, "_collect_stats", _spy)

    for _ in range(2):
        orders.table_stats("demo", "t_order", max_cols=12, top_limit=5, use_cache=True)
    assert len(n) == 1, "同一数据源同一表应命中缓存"

    for _ in range(2):
        orders.table_stats("demo", "t_order")             # 前端整表视图：不缓存
    assert len(n) == 3, "不显式 use_cache 就不该缓存（结果要新）"


def test_failed_stats_are_not_cached(orders, monkeypatch):
    orders.settings.meta_cache_ttl_s = 30
    calls: List[int] = []

    def _flaky(self, db, t, col_cap, top_limit):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("临时失败")
        return {"database": db, "table": t, "row_count": 20, "columns": []}
    monkeypatch.setattr(MetaTool, "_collect_stats", _flaky)
    with pytest.raises(RuntimeError):
        orders.table_stats("demo", "t_order", use_cache=True)
    orders.table_stats("demo", "t_order", use_cache=True)
    assert len(calls) == 2, "失败不能被写进缓存，否则一次抖动污染整轮会话"


# ---------------------------------------------------------------- 规则接线
def test_stats_reach_advise_table(orders):
    """stats 参数早就算好了却没传进来过 —— 这条规则此前永远不会响。"""
    profiles = si.collect(orders, "demo", ["t_order"])
    codes = {a.code for a in profiles[0].advices}
    assert "high_null_low_cardinality" in codes


def test_shape_fact_classifies_by_type_not_name():
    """中文列名不带"金额/数量"字样，按名字猜会把维度当度量。"""
    cols = [{"name": "订单额", "type": "DECIMAL(10,2)"}, {"name": "平台", "type": "TEXT"}]
    stats = {"row_count": 3, "columns": [
        {"column": "订单额", "null_ratio": 0, "distinct_count": 3,
         "min_value": 1.5, "max_value": 9.0, "avg_value": 5.0, "top_values": []},
        {"column": "平台", "null_ratio": 0, "distinct_count": 3,
         "min_value": "A", "max_value": "C", "avg_value": None,
         "top_values": [{"value": "A", "count": 2}]},
    ]}
    fact = _shape_fact("t", cols, stats)
    assert [m["column"] for m in fact["measures"]] == ["订单额"]
    assert [d["column"] for d in fact["dimensions"]] == ["平台"]


# ---------------------------------------------------------------- schema_context 注释
class _ColsOnlyAdapter:
    qualifies_tables = False

    def __init__(self, cols: List[Dict[str, Any]]) -> None:
        self._cols = cols

    def get_table_schema(self, db_name: str, table_name: str):
        return self._cols

    def list_tables(self, db_name: str):
        return ["t_order"]


def _cols_with_comments():
    return [{"name": "id", "type": "INTEGER", "primary_key": True, "comment": ""},
            {"name": "城市", "type": "TEXT", "primary_key": False, "comment": "下单城市（收货地）"},
            {"name": "下单时间", "type": "DATETIME", "primary_key": False, "comment": "订单创建时间，int 之外都算日期"},
            {"name": "订单额", "type": "DECIMAL(10,2)", "primary_key": False, "comment": "含税，单位元"}]


def test_comments_reach_prompt_without_breaking_rule_parser():
    tool = MetaTool(_ColsOnlyAdapter(_cols_with_comments()), Settings())
    text = tool.schema_context("demo", ["t_order"], with_facts=False)
    assert "下单城市（收货地）" in text

    assert _parse_schema_text(text) == {"t_order": ["id", "城市", "下单时间", "订单额"]}
    types = _parse_schema_types(text)
    # 注释里出现的 "int"/"日期" 不能污染类型推断
    assert types["t_order"]["订单额"] == "DECIMAL(10,2)"
    assert types["t_order"]["下单时间"] == "DATETIME"


# ---------------------------------------------------------------- phenomena
def test_phenomena_without_llm_are_fact_backed(orders):
    result = si.build_profile(tool=orders, db="demo", tables=["t_order"],
                              model=ModelConfig())
    assert result["llm_used"] is False
    assert result["phenomena"], "无 LLM 也要讲得出真实现象"
    assert all(any(ch.isdigit() for ch in line) for line in result["phenomena"])
    assert any("武汉" in line for line in result["phenomena"])
    assert all(line in result["overview"] for line in result["phenomena"][:3])


def test_llm_phenomena_pass_through_and_validate(orders, monkeypatch):
    def _fake_narrate(db, profiles, model, fallback=None):
        return {"overview": "订单库", "table_roles": {"t_order": "订单明细"},
                "optimization_plan": [], "query_tips": [],
                "phenomena": ["城市 4 种取值，武汉 8 行", "  ", {"怪": "格式"}]}
    monkeypatch.setattr(si, "narrate", _fake_narrate)
    result = si.build_profile(tool=orders, db="demo", tables=["t_order"],
                              model=ModelConfig(name="m", api_base="http://x",
                                                api_key="k", model="m"))
    assert result["llm_used"] is True
    assert len(result["phenomena"]) == 2                  # 空串被丢掉，非字符串被转成文本
    from basic.models.pydantic_schemas import SourceInsightResult
    parsed = SourceInsightResult(**result)
    assert parsed.phenomena[0].startswith("城市 4 种取值")


# ---------------------------------------------------------------- 叙述层与规则层分离
_READY = dict(name="m", api_base="http://x", api_key="k", model="m")


def test_facts_only_never_touches_llm(orders, monkeypatch):
    """narrate=false 的默认取数一次 LLM 都不能调。

    这条看着简单，却是整个改动的全部意义：之前第一步每次都等满 60 秒 ×（主 + 备）
    再回落规则摘要，用户白等两分钟。
    """
    def _boom(*a, **kw):
        raise AssertionError("规则层取数不应该调大模型")
    monkeypatch.setattr(si, "narrate", _boom)
    result = si.build_profile(tool=orders, db="demo", tables=["t_order"],
                              model=ModelConfig(**_READY), with_narration=False)
    assert result["llm_used"] is False
    assert result["advices"], "规则层结论与 LLM 无关，照常给出"
    assert result["phenomena"], "没有叙述也要讲得出真实现象"


def test_narration_timeout_floor_covers_both_models(orders, monkeypatch):
    """叙述层的超时下限要同时抬主模型和备用模型。

    实测这份业务化叙述要 150 秒以上才生成完：只抬主模型的话，主模型一超时
    就换到一个 60 秒的备用模型上，等于再白等一分钟然后照样失败。
    """
    seen = {}

    def _capture(db, profiles, model, fallback=None):
        seen["model"] = model
        seen["fallback"] = fallback
        return {"overview": "o", "phenomena": [], "table_roles": {},
                "optimization_plan": [], "query_tips": []}
    monkeypatch.setattr(si, "narrate", _capture)
    si.build_profile(tool=orders, db="demo", tables=["t_order"],
                     model=ModelConfig(**_READY, timeout_s=60),
                     fallback=ModelConfig(name="b", api_base="http://y",
                                          api_key="k2", model="backup", timeout_s=70))
    assert seen["model"].timeout_s == si.NARRATE_MIN_TIMEOUT_S
    assert seen["fallback"].timeout_s == si.NARRATE_MIN_TIMEOUT_S


def test_narration_timeout_floor_never_lowers(orders, monkeypatch):
    """下限只做抬升：用户把 LLM_TIMEOUT_S 配得更大时应当尊重配置。"""
    seen = {}

    def _capture(db, profiles, model, fallback=None):
        seen["model"] = model
        return {"overview": "o", "phenomena": [], "table_roles": {},
                "optimization_plan": [], "query_tips": []}
    monkeypatch.setattr(si, "narrate", _capture)
    si.build_profile(tool=orders, db="demo", tables=["t_order"],
                     model=ModelConfig(**_READY, timeout_s=900))
    assert seen["model"].timeout_s == 900


def test_insight_request_narrate_defaults_false():
    """接口默认必须是"只要事实"：默认值一改成 true，第一步就又变成两分钟阻塞。"""
    from basic.models.pydantic_schemas import SourceInsightRequest
    assert SourceInsightRequest().narrate is False
