"""基础统计分析模块（设计文档 §9.6）。

纯 Python 计算，不依赖重型分析引擎。输入 QueryResult，输出统计 dict：
- 列画像：类型推断（数值/时间/分类）、非空、空值率、distinct、min/max/avg/sum；
- 分组聚合：恰好一个分类列 + 一个数值列时，输出分组指标；
- TopN：按数值列降序取头部（含尾部对照）；
- 时间趋势：时间列 + 数值列时，按月聚合并给出环比。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models.pydantic_schemas import QueryResult
from ..utils.helpers import is_number, is_time_like, to_month, to_number, uniq


def analyze(result: QueryResult) -> Dict[str, Any]:
    cols = result.columns
    rows = result.rows
    n = len(rows)

    col_profile: Dict[str, Dict[str, Any]] = {}
    for c in cols:
        values = [r.get(c) for r in rows]
        non_null = [v for v in values if v is not None]
        prof: Dict[str, Any] = {
            "kind": _infer_kind(values),
            "non_null": len(non_null),
            "null_count": n - len(non_null),
            "null_ratio": round((n - len(non_null)) / n, 4) if n else 0.0,
            "distinct": len(uniq(non_null)),
        }
        if prof["kind"] == "numeric" and non_null:
            nums = [to_number(v) for v in non_null if to_number(v) is not None]
            if nums:
                prof.update({
                    "min": min(nums), "max": max(nums),
                    "avg": round(sum(nums) / len(nums), 4),
                    "sum": round(sum(nums), 4),
                })
        elif prof["kind"] == "time" and non_null:
            months = [to_month(v) for v in non_null if to_month(v)]
            if months:
                prof["min_time"], prof["max_time"] = min(months), max(months)
        col_profile[c] = prof

    stats: Dict[str, Any] = {
        "row_count": n,
        "column_count": len(cols),
        "columns": col_profile,
    }

    numeric_cols = [c for c, p in col_profile.items() if p["kind"] == "numeric"]
    time_cols = [c for c, p in col_profile.items() if p["kind"] == "time"]
    cat_cols = [c for c, p in col_profile.items() if p["kind"] == "category"]

    # 分组聚合：1 分类 + 1 数值
    if cat_cols and numeric_cols:
        cat, val = cat_cols[0], numeric_cols[0]
        groups = _group_agg(rows, cat, val)
        stats["group"] = {"category": cat, "value": val, "groups": groups}
        if groups:
            stats["topn"] = {
                "category": cat, "value": val,
                "top": groups[:5],
                "bottom": groups[-5:][::-1] if len(groups) > 1 else [],
            }

    # 时间趋势：1 时间 + 1 数值
    if time_cols and numeric_cols:
        t, val = time_cols[0], numeric_cols[0]
        series = _monthly_series(rows, t, val)
        if len(series) >= 2:
            stats["trend"] = {"time": t, "value": val, "series": series}
    return stats


# ---------------------------------------------------------------- 内部
def _infer_kind(values: List[Any]) -> str:
    sample = [v for v in values if v is not None][:50]
    if not sample:
        return "category"
    if all(is_number(v) for v in sample):
        return "numeric"
    if all(is_time_like(v) for v in sample):
        return "time"
    return "category"


def _group_agg(rows: List[Dict[str, Any]], cat: str, val: str,
               max_groups: int = 100) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, float]] = {}
    for r in rows:
        k = str(r.get(cat))
        v = to_number(r.get(val))
        if v is None:
            continue
        slot = agg.setdefault(k, {"sum": 0.0, "count": 0})
        slot["sum"] += v
        slot["count"] += 1
    out = [{"group": k, "sum": round(s["sum"], 4), "count": int(s["count"]),
            "avg": round(s["sum"] / s["count"], 4)} for k, s in agg.items()]
    out.sort(key=lambda x: x["sum"], reverse=True)
    return out[:max_groups]


def _monthly_series(rows: List[Dict[str, Any]], t: str, val: str) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, float]] = {}
    for r in rows:
        m = to_month(r.get(t))
        v = to_number(r.get(val))
        if not m or v is None:
            continue
        slot = agg.setdefault(m, {"sum": 0.0, "count": 0})
        slot["sum"] += v
        slot["count"] += 1
    series, prev = [], None
    for m in sorted(agg):
        cur = round(agg[m]["sum"], 4)
        mom = None if prev in (None, 0) else round((cur - prev) / prev * 100, 2)
        series.append({"period": m, "value": cur, "count": int(agg[m]["count"]),
                       "mom_pct": mom})
        prev = cur
    return series


__all__ = ["analyze"]
