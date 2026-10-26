"""适配器生命周期与文件源清理。

覆盖点（都是"不加就会静默坏掉"的类型）：
- MetaTool 用完必须 close，否则文件源 SQLite 一直被占着 —— Windows 上删不掉；
- close 之后 .db 可以被真正删除（这是 DELETE /api/datasource/{id} 能清干净的前提）；
- _remove_uploaded_file 的越界保护：非文件源、空路径、目录外的路径一律不删。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from basic.api.datasource_api import _remove_uploaded_file  # noqa: E402
from basic.config.settings import Settings  # noqa: E402
from basic.core.data_source.base import build_adapter  # noqa: E402
from basic.core.file_import import import_to_sqlite  # noqa: E402
from basic.core.meta_tools import MetaTool  # noqa: E402


@pytest.fixture
def file_ds(tmp_path):
    """造一个真实的文件数据源：CSV → SQLite → ds dict。"""
    src = tmp_path / "demo.csv"
    with src.open("w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows([["城市", "销售额"],
                                 ["武汉", "100"], ["北京", "200.5"]])
    db_path = tmp_path / "file_demo.db"
    rep = import_to_sqlite(src, db_path, "demo")
    return {"id": 1, "name": "demo.csv", "type": "file", "host": "", "port": 0,
            "user": "", "password": "", "database": rep["table"],
            "file_path": rep["db_path"]}


def test_meta_tool_close_releases_file_so_it_can_be_deleted(file_ds):
    """不 close → 句柄占着 → Windows 上 unlink 抛 PermissionError。"""
    settings = Settings()
    tool = MetaTool(build_adapter(file_ds), settings)
    assert tool.list_tables("demo") == ["demo"]     # 先真的连一次（拿到连接）
    tool.close()

    p = Path(file_ds["file_path"])
    p.unlink()                       # 能删掉 = 连接确实释放了
    assert not p.exists()


def test_meta_tool_context_manager_closes(file_ds):
    settings = Settings()
    with MetaTool(build_adapter(file_ds), settings) as tool:
        tool.list_tables("demo")
    Path(file_ds["file_path"]).unlink()
    assert not Path(file_ds["file_path"]).exists()


def test_delete_helper_removes_only_uploads_file(tmp_path, file_ds):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    # ① 非文件源：不动
    assert _remove_uploaded_file({"type": "mysql", "file_path": ""}, uploads) is False
    # ② 文件源但无路径：不动
    assert _remove_uploaded_file({"type": "file", "file_path": ""}, uploads) is False
    # ③ 路径在 uploads 之外：拒绝删（防误删）
    outside = tmp_path / "elsewhere.db"
    outside.write_bytes(b"x")
    assert _remove_uploaded_file({"type": "file", "file_path": str(outside)}, uploads) is False
    assert outside.exists()
    # ④ 正常：删掉
    inside = uploads / "file_abc.db"
    inside.write_bytes(b"x")
    assert _remove_uploaded_file({"type": "file", "file_path": str(inside)}, uploads) is True
    assert not inside.exists()
    # ⑤ 文件已不在：返回 False 但不抛
    assert _remove_uploaded_file({"type": "file", "file_path": str(inside)}, uploads) is False
