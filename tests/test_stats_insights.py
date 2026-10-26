"""统计 / 洞察 / 可视化推荐测试（设计文档 §13 验收）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from basic.core.insight_engine import extract_insights
from basic.core.stats_analyzer import analyze
from basic.core.viz_recommender import recommend
from basic.models.pydantic_schemas import QueryResult


def _qr(columns, rows):
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), cost_ms=1)


TREND_ROWS = [
    {"month": f"2025-{m:02d}", "total": v}
    for m, v in zip(range(1, 13), [100, 80, 120, 140, 160, 180, 200, 220, 260, 300, 340, 400])
]

GROUP_ROWS = [
    {"region": "华东", "amount": 500},
    {"region": "华北", "amount": 300},
    {"region": "华南", "amount": 100},
    {"region": "西南", "amount": None},
]


class TestStatsAnalyzer:
    def test_numeric_profile(self):
        stats = analyze(_qr(["amount"], [{"amount": 1}, {"amount": 2}, {"amount": None}]))
        prof = stats["columns"]["amount"]
        assert prof["kind"] == "numeric"
        assert prof["null_count"] == 1 and prof["null_ratio"] == round(1 / 3, 4)
        assert prof["sum"] == 3 and prof["avg"] == 1.5 and prof["max"] == 2

    def test_group_and_topn(self):
        stats = analyze(_qr(["region", "amount"], GROUP_ROWS))
        g = stats["group"]
        assert g["category"] == "region" and g["value"] == "amount"
        assert g["groups"][0]["group"] == "华东" and g["groups"][0]["sum"] == 500
        assert stats["topn"]["top"][0]["group"] == "华东"
        assert stats["topn"]["bottom"][0]["group"] == "华南"   # bottom 升序，首位最小

    def test_monthly_trend_with_mom(self):
        stats = analyze(_qr(["month", "total"], TREND_ROWS))
        series = stats["trend"]["series"]
        assert len(series) == 12 and series[0]["period"] == "2025-01"
        assert series[1]["mom_pct"] == -20.0   # 100 → 80
        assert series[-1]["mom_pct"] is not None

    def test_empty_rows_safe(self):
        stats = analyze(_qr(["a", "b"], []))
        assert stats["row_count"] == 0


class TestInsightEngine:
    def test_trend_up_insight(self):
        stats = analyze(_qr(["month", "total"], TREND_ROWS))
        insights = extract_insights(stats)
        titles = [i.title for i in insights]
        assert any("上升趋势" in t for t in titles)
        assert all(i.evidence for i in insights)          # 每条都有依据
        assert any("峰值月份" in t for t in titles)
        assert any("谷值月份" in t for t in titles)

    def test_null_anomaly(self):
        rows = [{"region": "A", "amount": None if i < 3 else i} for i in range(10)]
        insights = extract_insights(analyze(_qr(["region", "amount"], rows)))
        assert any("空值偏高" in i.title for i in insights)

    def test_comparison_insight(self):
        insights = extract_insights(analyze(_qr(["region", "amount"], GROUP_ROWS)))
        titles = [i.title for i in insights]
        assert any("头部与尾部差距" in t for t in titles)
        assert any("高于均值" in t for t in titles)

    def test_no_insight_when_insufficient(self):
        insights = extract_insights(analyze(_qr(["a"], [{"a": 1}])))
        assert isinstance(insights, list)


class TestVizRecommender:
    def test_trend_intent_line(self):
        stats = analyze(_qr(["month", "total"], TREND_ROWS))
        assert recommend(stats, "时间趋势分析") == "line"

    def test_group_bar(self):
        stats = analyze(_qr(["region", "amount"], GROUP_ROWS))
        assert recommend(stats, "分类对比分析") == "bar"

    def test_fallback_table(self):
        assert recommend({"row_count": 1, "columns": {}}, "") == "table"
