"""文件导入（file_import）单测：类型推断 / 标识符清洗 / 封顶 / 各格式解析。"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from basic.core.file_import import ALLOWED_EXT, import_to_sqlite, sanitize_ident  # noqa: E402


def _write_csv(p: Path, rows):
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)


def test_sanitize_ident_rules():
    assert sanitize_ident("销售明细 2024!") == "t_2024"       # 默认 ASCII（表名），数字开头补前缀
    assert sanitize_ident("月份*())", allow_cjk=True) == "月份", "列名允许中文"
    assert sanitize_ident("月份*())") == "t", "表名默认走 ASCII"
    assert sanitize_ident("2024开始") == "t_2024"             # 数字开头补前缀 + 表名去中文
    assert sanitize_ident("") == "t"
    assert sanitize_ident("   ") == "t"
    assert len(sanitize_ident("x" * 100)) == 60


def test_csv_import_with_chinese_headers(tmp_path):
    src = tmp_path / "销售.csv"
    _write_csv(src, [["月份", "城市", "销售额"],
                     ["2024-01", "武汉", "100"],
                     ["2024-02", "北京", "200.5"],
                     ["2024-03", "上海", ""]])
    out = tmp_path / "out.db"
    rep = import_to_sqlite(src, out, "销售 2024!")
    assert rep["table"] == "t_2024"                     # 表名 ASCII 化
    assert [c["name"] for c in rep["columns"]] == ["月份", "城市", "销售额"]
    assert [c["type"] for c in rep["columns"]] == ["TEXT", "TEXT", "REAL"]
    conn = sqlite3.connect(str(out))
    try:
        rows = conn.execute('SELECT "月份","销售额" FROM "t_2024" ORDER BY "月份"').fetchall()
    finally:
        conn.close()
    assert rows[0] == ("2024-01", 100.0)
    assert rows[2] == ("2024-03", None)                 # 空串 → NULL


def test_json_import(tmp_path):
    src = tmp_path / "d.json"
    src.write_text(json.dumps([{"城市": "武汉", "人口": 1000}, {"城市": "北京", "人口": 2000}],
                              ensure_ascii=False), encoding="utf-8")
    rep = import_to_sqlite(src, tmp_path / "o.db", "cities")
    assert [c["name"] for c in rep["columns"]] == ["城市", "人口"]
    assert rep["rows"] == 2 and rep["columns"][1]["type"] == "INTEGER"


def test_xlsx_import(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    src = tmp_path / "t.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["名称", "数量"])
    ws.append(["A", 1])
    ws.append(["B", 2])
    wb.save(src)
    rep = import_to_sqlite(src, tmp_path / "o.db", "t")
    assert rep["rows"] == 2 and rep["columns"][1]["type"] == "INTEGER"


def test_row_cap(tmp_path):
    """行数上限由参数传入（配置化：settings.max_import_rows），不再靠改模块常量。"""
    src = tmp_path / "big.csv"
    _write_csv(src, [["a"]] + [[str(i)] for i in range(5000)])
    rep = import_to_sqlite(src, tmp_path / "o.db", "big", max_rows=100)
    assert rep["rows"] == 100
    full = import_to_sqlite(src, tmp_path / "o2.db", "big", max_rows=200_000)
    assert full["rows"] == 5000


def test_bad_json_rejected(tmp_path):
    src = tmp_path / "bad.json"
    src.write_text(json.dumps({"a": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        import_to_sqlite(src, tmp_path / "o.db", "x")


def test_unsupported_ext_rejected(tmp_path):
    src = tmp_path / "x.exe"
    src.write_bytes(b"MZ")
    with pytest.raises(ValueError):
        import_to_sqlite(src, tmp_path / "o.db", "x")


def test_allowed_ext_contains_common():
    assert {".csv", ".tsv", ".json", ".xlsx"} <= ALLOWED_EXT
