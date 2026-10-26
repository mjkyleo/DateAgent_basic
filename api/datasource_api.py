"""数据源管理接口（设计文档 §7.1）：增删改查、测试连接、切换激活、文件导入。

密码纪律：响永不回显明文（password_set 标记即可）；连接串只在内存组装。
文件导入：CSV/TSV/JSON/XLSX → 解析成单表 SQLite（core/file_import.py），
建数据源后自动激活——导入即可问数。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..core.file_import import ALLOWED_EXT, import_to_sqlite, sanitize_ident
from ..models.pydantic_schemas import (Datasource, DatasourceCreate, DatasourceUpdate,
                                       OkResponse, SwitchResponse, TestConnectionResponse)
from ..utils.cache import cache_invalidate
from ..utils.helpers import sanitize_error
from ..utils.logging import get_logger

log = get_logger("basic.api.datasource")
router = APIRouter(prefix="/api/datasource", tags=["datasource"])


def _store(request: Request):
    return request.app.state.store


def _invalidate(ds_id: int | None = None) -> None:
    """数据源变了 → 丢掉它的元数据缓存（表清单），否则会拿旧库的表名做校验。"""
    cache_invalidate(f"ds{ds_id}" if ds_id is not None else "")


@router.post("", response_model=Datasource)
def create_datasource(body: DatasourceCreate, request: Request):
    if body.type != "mysql":
        raise HTTPException(400, "MySQL 连接请用本接口；文件请走 /api/datasource/file 上传")
    ds = _store(request).create_datasource(
        name=body.name.strip(), type=body.type, host=body.host.strip(),
        port=body.port, user=body.user.strip(), password=body.password,
        database=body.database.strip())
    return _public(ds)


@router.post("/file", response_model=Datasource)
async def upload_file_datasource(request: Request, file: UploadFile = File(...)):
    """上传文件建数据源：解析 → 单表 SQLite → 建源 → 自动激活。"""
    settings = request.app.state.settings
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "仅支持 " + "、".join(sorted(
            e.lstrip('.').upper() for e in ALLOWED_EXT)) + " 文件")

    uploads = Path(settings.uploads_dir)
    uploads.mkdir(parents=True, exist_ok=True)
    raw_path = uploads / f"src_{uuid.uuid4().hex[:12]}{ext}"
    db_path: Path | None = None        # 先声明：失败分支要连半成品 .db 一起清

    # 上限取自配置（MAX_UPLOAD_MB / MAX_IMPORT_ROWS），硬编码常量只是默认值
    max_bytes = int(getattr(settings, "max_upload_mb", 20)) * 1024 * 1024

    # 流式落盘 + 大小硬封顶（不把大文件整个读进内存）
    size = 0
    try:
        with raw_path.open("wb") as f:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f"文件超过 {max_bytes // (1024 * 1024)} MB 上限")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "文件为空")

        stem = sanitize_ident(Path(filename).stem)
        db_path = uploads / f"file_{uuid.uuid4().hex[:12]}.db"
        # 解析+写库（上限 20 万行）是纯同步重活：本路由是 async，直接跑会卡住
        # 整个事件循环（所有请求排队），必须丢线程池。
        report = await run_in_threadpool(
            import_to_sqlite, raw_path, db_path, stem,
            max_rows=int(getattr(settings, "max_import_rows", 200_000)))
    except HTTPException:
        raw_path.unlink(missing_ok=True)
        _unlink_quiet(db_path)          # 半成品 .db 也要清，否则 uploads/ 留孤儿
        raise
    except Exception as e:  # noqa: BLE001  解析失败要清掉半成品
        raw_path.unlink(missing_ok=True)
        _unlink_quiet(db_path)
        log.warning("文件导入失败 %s：%s", filename, sanitize_error(str(e)))
        raise HTTPException(400, f"文件解析失败：{sanitize_error(str(e))}") from e
    finally:
        await file.close()
    raw_path.unlink(missing_ok=True)   # 数据已进 SQLite，原文件不保留

    ds = _store(request).create_datasource(
        name=filename, type="file", host="", port=0, user="", password="",
        database=report["table"], file_path=report["db_path"])
    _store(request).set_active(ds["id"])     # 导入即激活，马上可以提问
    # 重新取：set_active 改的是库里状态，返回创建时快照的话 is_active 会是 false
    return _public(_store(request).get_datasource(ds["id"]) or ds)


@router.get("", response_model=list[Datasource])
def list_datasources(request: Request):
    return [_public(d) for d in _store(request).list_datasources()]


@router.get("/{ds_id}", response_model=Datasource)
def get_datasource(ds_id: int, request: Request):
    ds = _store(request).get_datasource(ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    return _public(ds)


@router.put("/{ds_id}", response_model=Datasource)
def update_datasource(ds_id: int, body: DatasourceUpdate, request: Request):
    store = _store(request)
    if not store.get_datasource(ds_id):
        raise HTTPException(404, "数据源不存在")
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    ds = store.update_datasource(ds_id, **fields)
    _invalidate(ds_id)      # 连接参数可能改了（换库/换账号）→ 表清单缓存作废
    return _public(ds or {})


@router.delete("/{ds_id}", response_model=OkResponse)
def delete_datasource(ds_id: int, request: Request):
    store = _store(request)
    ds = store.get_datasource(ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    was_active = bool(ds.get("is_active"))
    store.delete_datasource(ds_id)
    _invalidate(ds_id)
    # 会话落在会话存储（生产是 MySQL，与 store 不是同一个对象）：
    # 不级联删会留下指向已删数据源的孤儿会话，历史列表里点开会 404。
    sessions = request.app.state.sessions
    if sessions is not store and hasattr(sessions, "delete_rounds_by_ds"):
        try:
            removed = sessions.delete_rounds_by_ds(ds_id)
            if removed:
                log.info("数据源 %s 删除，级联清理会话 %s 条", ds_id, removed)
        except Exception as e:  # noqa: BLE001  级联失败不该挡住数据源删除
            log.warning("级联清理数据源 %s 的会话失败：%s", ds_id, sanitize_error(str(e)))
    # 删的是激活源：回落到最近新增的剩余数据源，避免页面落在"未激活"空态
    fallback_msg = ""
    if was_active:
        rest = store.list_datasources()          # 按 id 升序 → 取最后一个是最近新增的
        if rest and store.set_active(rest[-1]["id"]):
            fallback_msg = f"，已切换到「{rest[-1]['name']}」"
    # 文件源：连同导入生成的 SQLite 一起删，避免 data/uploads/ 留孤儿文件
    removed_file = _remove_uploaded_file(ds, Path(request.app.state.settings.uploads_dir))
    removed_msg = "（含导入的数据文件）" if removed_file else ""
    return OkResponse(success=True, msg=f"已删除{removed_msg}{fallback_msg}")


def _unlink_quiet(p: Path | None) -> None:
    """删除失败也不影响主流程（清临时文件属于 best-effort）。"""
    if p is None:
        return
    try:
        Path(p).unlink(missing_ok=True)
    except OSError:                    # Windows 上文件被占用时会到这
        log.warning("临时文件清理失败（可手动删除）：%s", p.name)


def _remove_uploaded_file(ds: Dict[str, Any], uploads_root: Path) -> bool:
    """删除文件源对应的 SQLite（best-effort：删不掉也不影响数据源删除）。"""
    if (ds.get("type") or "").lower() != "file":
        return False
    path = (ds.get("file_path") or "").strip()
    if not path:
        return False
    try:
        p = Path(path)
        # 只允许删 uploads 目录下的文件（防误删其它路径）
        if p.resolve().parent != uploads_root.resolve():
            return False
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False


@router.post("/{ds_id}/test", response_model=TestConnectionResponse)
def test_connection(ds_id: int, request: Request):
    store = _store(request)
    ds = store.get_datasource(ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    from ..core.data_source.base import build_adapter
    try:
        adapter = build_adapter(ds)
        ok = adapter.test_connection()
        adapter.close()
    except Exception as e:  # noqa: BLE001  脱敏：不回显连接串/IP
        return TestConnectionResponse(connected=False,
                                      msg=f"连接失败：{sanitize_error(str(e))}")
    return TestConnectionResponse(connected=ok,
                                  msg="连接成功" if ok else "连接失败：凭据或网络不可达")


@router.post("/{ds_id}/switch", response_model=SwitchResponse)
def switch_datasource(ds_id: int, request: Request):
    if not _store(request).set_active(ds_id):
        raise HTTPException(404, "数据源不存在")
    return SwitchResponse(success=True, ds_id=ds_id, msg="已切换为当前激活数据源")


def _public(ds: dict) -> dict:
    """出参脱敏：password 置空，仅保留是否已设置的标记。"""
    out = dict(ds)
    out["password_set"] = bool(ds.get("password"))
    out["password"] = None
    return out


__all__ = ["router"]
