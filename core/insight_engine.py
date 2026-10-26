"""规则洞察引擎（设计文档 §9.7）。

规则驱动、不依赖 LLM 推理；每条洞察必须附带判断依据。
三类规则：
- anomaly 异常：空值占比过高、极值偏离、列无区分度；
- trend 趋势：月度序列上升/下降/波动，峰谷月份及偏离度；
- comparison 对比：分组 top/bottom 差距、头部份额、与均值差异。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models.pydantic_schemas import Insight

NULL_RATIO_THRESHOLD = 0.2      # 空值占比告警线
EXTREME_RATIO = 3.0             # 极值 = 超过均值 3 倍（或低于 1/3）
TREND_FLAT_BAND = 5.0           # 趋势判定：首末月变化在 ±5% 内视为波动/平稳


def extract_insights(stats: Dict[str, Any], result_meta: Dict[str, Any] | None = None) -> List[Insight]:
    insights: List[Insight] = []
    insights += _anomaly_rules(stats)
    insights += _trend_rules(stats)
    insights += _comparison_rules(stats)
    return insights[:8]   # 上限 8 条，避免刷屏


# ---------------------------------------------------------------- 异常
def _anomaly_rules(stats: Dict[str, Any]) -> List[Insight]:
    out: List[Insight] = []
    for col, prof in (stats.get("columns") or {}).items():
        ratio = prof.get("null_ratio") or 0.0
        if ratio >= NULL_RATIO_THRESHOLD and prof.get("null_count", 0) > 0:
            out.append(Insight(
                type="anomaly", title=f"字段「{col}」空值偏高",
                detail=f"空值 {prof['null_count']} 行，占比 {ratio * 100:.1f}%，"
                       f"高于 {NULL_RATIO_THRESHOLD * 100:.0f}% 告警线。",
                evidence=f"列画像：null_count={prof['null_count']}, "
                         f"row_count={stats.get('row_count', 0)}"))
        if prof.get("kind") == "category" and prof.get("distinct") == 1 \
                and (stats.get("row_count") or 0) > 0:
            out.append(Insight(
                type="anomaly", title=f"字段「{col}」无区分度",
                detail="该列所有取值相同，作为分组维度无意义。",
                evidence=f"列画像：distinct=1"))
        avg = prof.get("avg")
        mx = prof.get("max")
        if prof.get("kind") == "numeric" and avg and mx is not None and avg > 0 \
                and mx > EXTREME_RATIO * avg:
            out.append(Insight(
                type="anomaly", title=f"字段「{col}」存在极端值",
                detail=f"最大值 {mx:g} 超过均值 {avg:g} 的 {EXTREME_RATIO:g} 倍，"
                       f"建议核查是否为异常记录。",
                evidence=f"列画像：max={mx:g}, avg={avg:g}"))
    return out


# ---------------------------------------------------------------- 趋势
def _trend_rules(stats: Dict[str, Any]) -> List[Insight]:
    trend = stats.get("trend")
    if not trend or len(trend.get("series") or []) < 2:
        return []
    series = trend["series"]
    first, last = series[0], series[-1]
    total_change = ((last["value"] - first["value"]) / first["value"] * 100
                    if first["value"] else 0.0)
    val_name = trend.get("value", "指标")
    if total_change > TREND_FLAT_BAND:
        direction = "上升"
    elif total_change < -TREND_FLAT_BAND:
        direction = "下降"
    else:
        direction = "平稳波动"
    out = [Insight(
        type="trend", title=f"{val_name}整体呈{direction}趋势",
        detail=f"{first['period']} 至 {last['period']} 共 {len(series)} 个月，"
               f"从 {first['value']:g} 变为 {last['value']:g}，"
               f"累计变化 {total_change:+.1f}%。",
        evidence=f"时间序列首末月对比：{first['period']}={first['value']:g}, "
                 f"{last['period']}={last['value']:g}")]

    mean = sum(s["value"] for s in series) / len(series)
    if mean:
        peak = max(series, key=lambda s: s["value"])
        trough = min(series, key=lambda s: s["value"])
        if peak["value"] > 0:
            out.append(Insight(
                type="trend", title=f"峰值月份：{peak['period']}",
                detail=f"{peak['period']} 达最高 {peak['value']:g}，"
                       f"高于月均值 {((peak['value'] - mean) / mean * 100):.1f}%。",
                evidence=f"月度聚合：mean={mean:.2f}, max={peak['value']:g}"))
        if trough["value"] >= 0 and trough["value"] < mean:
            out.append(Insight(
                type="trend", title=f"谷值月份：{trough['period']}",
                detail=f"{trough['period']} 最低 {trough['value']:g}，"
                       f"低于月均值 {((mean - trough['value']) / mean * 100):.1f}%。",
                evidence=f"月度聚合：mean={mean:.2f}, min={trough['value']:g}"))
    return out


# ---------------------------------------------------------------- 对比
def _comparison_rules(stats: Dict[str, Any]) -> List[Insight]:
    group = stats.get("group")
    if not group:
        return []
    groups = group.get("groups") or []
    if len(groups) < 2:
        return []
    cat, val = group.get("category", "分组"), group.get("value", "指标")
    top, bottom = groups[0], groups[-1]
    total = sum(g["sum"] for g in groups)
    out: List[Insight] = []
    out.append(Insight(
        type="comparison", title=f"{cat} 头部与尾部差距明显",
        detail=f"最高「{top['group']}」{top['sum']:g}，最低「{bottom['group']}」"
               f"{bottom['sum']:g}，相差 "
               f"{(top['sum'] - bottom['sum']):g}"
               + (f"（{top['sum'] / bottom['sum']:.1f} 倍）" if bottom["sum"] else "") + "。",
        evidence=f"分组聚合排序：top={top['group']}, bottom={bottom['group']}"))
    if total > 0:
        share = top["sum"] / total * 100
        if share >= 30:
            out.append(Insight(
                type="comparison", title=f"「{top['group']}」贡献 {share:.1f}%",
                detail=f"头部 {cat}「{top['group']}」占 {val} 合计的 {share:.1f}%，"
                       f"集中度较高。",
                evidence=f"分组聚合：top_sum={top['sum']:g}, total={total:.2f}"))
    mean = total / len(groups)
    above = [g for g in groups if g["sum"] > mean]
    if 0 < len(above) < len(groups):
        out.append(Insight(
            type="comparison", title=f"{len(above)}/{len(groups)} 个{cat}高于均值",
            detail=f"{len(groups)} 个{cat}中 {len(above)} 个 {val} 高于均值 {mean:.2f}。",
            evidence=f"分组聚合：mean={mean:.2f}"))
    return out


__all__ = ["extract_insights"]
