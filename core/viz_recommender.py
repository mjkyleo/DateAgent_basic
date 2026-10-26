"""可视化推荐模块（设计文档 §9.8）。

按字段类型（数值/时间/分类）+ 用户意图推荐图表：
- 时间趋势意图 → 折线图；
- 分类对比 → 柱状图；
- 占比分析 → 饼图；
- 兜底 → 表格。
推荐只是默认值，前端允许用户手动切换（table/bar/line/pie）。
"""
from __future__ import annotations

from typing import Any, Dict, Literal

ChartKind = Literal["table", "bar", "line", "pie"]


def recommend(stats: Dict[str, Any], intent: str = "") -> ChartKind:
    has_trend = bool(stats.get("trend"))
    has_group = bool(stats.get("group"))
    intent = intent or ""

    if has_trend and ("趋势" in intent or not has_group):
        return "line"
    if "占比" in intent and has_group and _share_skew(stats):
        return "pie"
    if has_group:
        return "bar"
    if has_trend:
        return "line"
    return "table"


def _share_skew(stats: Dict[str, Any]) -> bool:
    """头部份额 >= 50% 时饼图才有意义（否则类别太碎）。"""
    groups = (stats.get("group") or {}).get("groups") or []
    if len(groups) < 2:
        return False
    total = sum(g.get("sum", 0) for g in groups)
    if total <= 0:
        return False
    return groups[0].get("sum", 0) / total >= 0.5


def build_chart_option(kind: ChartKind, query_result: Dict[str, Any],
                       stats: Dict[str, Any], title: str = "") -> Dict[str, Any]:
    """生成 ECharts option（由后端出 option，前端直接 setOption）。"""
    cols = query_result.get("columns") or []
    rows = query_result.get("rows") or []
    if len(cols) < 2 or not rows:
        return {"title": {"text": title or "数据表"}}
    num_col = next((c for c in cols if (stats.get("columns", {}).get(c, {})
                                        .get("kind") == "numeric")), None)
    cat_col = next((c for c in cols if c != num_col), None)
    if not num_col or not cat_col:
        return {"title": {"text": title or "数据表"}}
    data = rows[:100]
    x = [str(r.get(cat_col, "")) for r in data]
    y = [float(r.get(num_col) or 0) for r in data]
    base = {"title": {"text": title or f"{num_col} 按 {cat_col}", "left": "center"},
            "tooltip": {"trigger": "axis"}}
    if kind == "line":
        return {**base, "xAxis": {"type": "category", "data": x},
                "yAxis": {"type": "value"},
                "series": [{"type": "line", "smooth": True, "data": y}]}
    if kind == "pie":
        return {"title": {"text": title or f"{num_col} 占比", "left": "center"},
                "tooltip": {"trigger": "item"},
                "series": [{"type": "pie", "radius": "58%",
                            "data": [{"name": a, "value": b} for a, b in zip(x, y)]}]}
    if kind == "bar":
        return {**base, "xAxis": {"type": "category", "data": x,
                                  "axisLabel": {"rotate": 30 if len(x) > 8 else 0}},
                "yAxis": {"type": "value"},
                "series": [{"type": "bar", "data": y, "barMaxWidth": 42}]}
    return {"title": {"text": title or "数据表"}}


__all__ = ["recommend", "build_chart_option"]
