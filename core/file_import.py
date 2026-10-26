"""文件导入：CSV/TSV/JSON/XLSX → 单表 SQLite（设计文档 §9.1 文件数据源的落地）。

边界（全部硬封顶，防大文件拖垮进程）：
- 单文件 ≤ MAX_BYTES；行数 ≤ MAX_ROWS；列数 ≤ MAX_COLS；
- 表名/列名统一清洗成 [A-Za-z0-9_]（与 MetaTool 标识符白名单一致，杜绝奇怪标识符）；
- 类型推断：整列像整数 → INTEGER，像数字 → REAL，否则 TEXT；空串 → NULL；
- 只用 stdlib csv/json + openpyxl（xlsx），不引 pandas。

产出的 SQLite 交给 FileAdapter 以只读模式打开——导入与查询分离，导入完原文件即可删。
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Tuple

MAX_COLS = 100
MAX_ROWS = 200_000
MAX_BYTES = 20 * 1024 * 1024
ALLOWED_EXT = {".csv", ".tsv", ".txt", ".json", ".xlsx"}

_IDENT_RE = re.compile(r"[^0-9A-Za-z_]+")
_IDENT_CJK_RE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")


def sanitize_ident(name: str, *, fallback: str = "t", allow_cjk: bool = False) -> str:
    """清洗成合法标识符：非法字符转 _，去首尾 _，数字开头补前缀，限长 60。

    allow_cjk=True 时保留中文（用于列名——前端树/LLM 上下文都按原名展示）；
    表名一律走 ASCII（LLM 生成的 SQL 里裸写中文表名过不了 sqlglot 解析）。
    """
    s = (_IDENT_CJK_RE if allow_cjk else _IDENT_RE).sub("_", str(name or "")).strip("_")[:60]
    if not s:
        s = fallback
    if s[0].isdigit():
        s = f"{fallback}_{s}"
    return s


def import_to_sqlite(src: Path, out_db: Path, table: str, *,
                     max_rows: int = MAX_ROWS, max_cols: int = MAX_COLS) -> Dict[str, Any]:
    """把 src 解析成 out_db 里的一张表。返回 {db_path, table, rows, columns}。

    max_rows/max_cols 由调用方（settings）传入——配置化：改 .env 即生效，不改代码。
    """
    table = sanitize_ident(table)                      # 表名：ASCII（可被 sqlglot 解析）
    headers, rows_iter, total = _read_rows(src)
    if not headers:
        raise ValueError("文件没有可识别的表头或数据")
    if len(headers) > max_cols:
        raise ValueError(f"列数 {len(headers)} 超过上限 {max_cols}")

    cols = [_unique_names(headers)]
    # 类型推断（采样前 2000 行）：int → real → text
    kinds = _infer_kinds(cols[0], _sample_rows(rows_iter, 2000))
    # 重新拿全量迭代器（生成器只能消费一次）
    _, rows_iter2, _ = _read_rows(src)

    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()
    conn = sqlite3.connect(str(out_db))
    try:
        # cols[0] 已是合法标识符（_unique_names 清洗过），DDL 里不再二次清洗
        ddl = ", ".join(f'"{c}" {kinds[i]}' for i, c in enumerate(cols[0]))
        conn.execute(f'CREATE TABLE "{table}" ({ddl})')
        n = 0
        batch: List[Tuple[Any, ...]] = []
        for raw in rows_iter2:
            if n >= max_rows:
                break
            batch.append(tuple(_cast(v, kinds[i]) for i, v in enumerate(raw[:len(cols[0])])))
            n += 1
            if len(batch) >= 2000:
                conn.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols[0]))})',
                                 batch)
                batch = []
        if batch:
            conn.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols[0]))})', batch)
        conn.commit()
    finally:
        conn.close()
    return {"db_path": str(out_db), "table": table, "rows": n,
            "columns": [{"name": c, "type": kinds[i]} for i, c in enumerate(cols[0])]}


# ------------------------------------------------------------ 读取

def _read_rows(src: Path) -> Tuple[List[str], Iterator[List[str]], int]:
    ext = src.suffix.lower()
    if ext in (".csv", ".tsv", ".txt"):
        return _read_delimited(src, _sniff_delimiter(src))
    if ext == ".json":
        return _read_json(src)
    if ext == ".xlsx":
        return _read_xlsx(src)
    raise ValueError(f"不支持的文件类型：{ext}（支持 CSV/TSV/TXT/JSON/XLSX）")


def _sniff_delimiter(src: Path) -> str:
    with src.open("r", encoding="utf-8-sig", errors="replace") as f:
        head = f.readline()
    counts = {d: head.count(d) for d in (",", "\t", ";", "|")}
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else ","


def _read_delimited(src: Path, delim: str) -> Tuple[List[str], Iterator[List[str]], int]:
    def gen() -> Iterator[List[str]]:
        with src.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=delim)
            header = next(reader, None)
            if header is None:
                return
            width = len(header)
            for row in reader:
                yield (row + [""] * width)[:width]     # 缺列补空、多列截断
    with src.open("r", encoding="utf-8-sig", errors="replace") as f:
        first = csv.reader(f, delimiter=delim)
        header = next(first, None)
    if header is None:
        raise ValueError("文件为空")
    return [h.strip() or "col" for h in header], gen(), 0


def _read_json(src: Path) -> Tuple[List[str], Iterator[List[str]], int]:
    data = json.loads(src.read_text(encoding="utf-8-sig", errors="replace"))
    if isinstance(data, dict):                       # {"data": [...]} 常见导出格式
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                data = v
                break
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("JSON 需要是「对象数组」，例如 [{...},{...}]")
    keys: List[str] = []
    for obj in data[:200]:
        for k in obj.keys():
            if k not in keys:
                keys.append(k)
    def gen() -> Iterator[List[str]]:
        for obj in data:
            yield ["" if obj.get(k) is None else str(obj.get(k)) for k in keys]
    return keys, gen(), 0


def _read_xlsx(src: Path) -> Tuple[List[str], Iterator[List[str]], int]:
    try:
        from openpyxl import load_workbook
    except ImportError as e:  # noqa: F811
        raise ValueError("读取 XLSX 需要 openpyxl（pip install openpyxl）") from e
    wb = load_workbook(str(src), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = next(it, None)
    if header is None:
        raise ValueError("XLSX 第一个工作表为空")
    width = len(header)

    def gen() -> Iterator[List[str]]:
        for row in it:
            vals = ["" if v is None else str(v) for v in row]
            yield (vals + [""] * width)[:width]

    return [str(h).strip() if h is not None else "" for h in header], gen(), 0


# ------------------------------------------------------------ 类型推断与清洗

def _unique_names(headers: List[str]) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for i, h in enumerate(headers):
        name = sanitize_ident(h, fallback=f"col_{i + 1}", allow_cjk=True)
        base, k = name, 2
        while name in seen:
            name = f"{base}_{k}"
            k += 1
        seen.add(name)
        out.append(name)
    return out


def _sample_rows(it: Iterable[List[str]], n: int) -> List[List[str]]:
    return [r for _, r in zip(range(n), it)]


def _infer_kinds(headers: List[str], rows: List[List[str]]) -> List[str]:
    kinds = ["INTEGER"] * len(headers)
    for row in rows:
        for i in range(len(headers)):
            if kinds[i] == "TEXT":
                continue
            v = (row[i] if i < len(row) else "").strip()
            if v == "":
                continue
            if kinds[i] == "INTEGER":
                if re.fullmatch(r"[+-]?\d+", v):
                    continue
                kinds[i] = "REAL"
            if kinds[i] == "REAL":
                try:
                    float(v)
                    continue
                except ValueError:
                    kinds[i] = "TEXT"
    return kinds


def _cast(v: str, kind: str) -> Any:
    v = (v or "").strip()
    if v == "":
        return None
    if kind == "INTEGER":
        try:
            return int(v)
        except ValueError:
            return None
    if kind == "REAL":
        try:
            return float(v)
        except ValueError:
            return None
    return v


__all__ = ["ALLOWED_EXT", "MAX_BYTES", "MAX_COLS", "MAX_ROWS",
           "import_to_sqlite", "sanitize_ident"]
