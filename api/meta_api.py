"""元数据探查接口（设计文档 §7.2）：库、表、结构、采样、分布统计。

全部经 MetaTool 有界封装；默认作用于当前激活数据源（?ds_id= 可临时覆盖）。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import APIRouter, HTTPException, Query, Request

from ..core.meta_tools import MetaTool
from ..models.pydantic_schemas import TableSchema, TableStats
from ..utils.helpers import sanitize_error

router = APIRouter(prefix="/api/meta", tags=["meta"])


@contextmanager
def _meta_tool(request: Request, ds_id: int | None) -> Iterator[MetaTool]:
    """构造 MetaTool：优先 ds_id 参数，否则当前激活数据源；都无则 400。

    用 with 包起来以确保连接释放——不关的话 MySQL 会耗尽连接数，
    文件源的 SQLite 会一直被锁住（删数据源时删不掉 .db）。
    """
    from ..core.data_source.base import build_adapter
    store = request.app.state.store
    ds = store.get_datasource(ds_id) if ds_id else None
    if ds is None:
        ds = store.get_active()
    if not ds:
        raise HTTPException(400, "尚未配置或激活数据源：请先新增 MySQL 连接并切换激活")
    tool = MetaTool(build_adapter(ds), request.app.state.settings,
                    cache_scope=f"ds{ds.get('id')}")
    try:
        yield tool
    finally:
        tool.close()


@router.get("/databases")
def list_databases(request: Request, ds_id: int | None = None):
    try:
        with _meta_tool(request, ds_id) as tool:
            return {"databases": tool.list_databases()}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"获取数据库列表失败：{sanitize_error(str(e))}") from e


@router.get("/{db_name}/tables")
def list_tables(db_name: str, request: Request, ds_id: int | None = None):
    try:
        with _meta_tool(request, ds_id) as tool:
            return {"database": db_name, "tables": tool.list_tables(db_name)}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"获取表列表失败：{sanitize_error(str(e))}") from e


@router.get("/{db_name}/table/{table_name}/schema", response_model=TableSchema)
def table_schema(db_name: str, table_name: str, request: Request,
                 ds_id: int | None = None):
    try:
        with _meta_tool(request, ds_id) as tool:
            return tool.table_schema(db_name, table_name)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"获取表结构失败：{sanitize_error(str(e))}") from e


@router.get("/{db_name}/table/{table_name}/sample")
def table_sample(db_name: str, table_name: str, request: Request,
                 limit: int = Query(default=10, ge=1, le=200), ds_id: int | None = None):
    try:
        with _meta_tool(request, ds_id) as tool:
            rows = tool.sample(db_name, table_name, limit)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"采样失败：{sanitize_error(str(e))}") from e
    return {"database": db_name, "table": table_name, "rows": rows,
            "row_count": len(rows)}


@router.get("/{db_name}/table/{table_name}/stats", response_model=TableStats)
def table_stats(db_name: str, table_name: str, request: Request,
                ds_id: int | None = None):
    try:
        with _meta_tool(request, ds_id) as tool:
            return tool.table_stats(db_name, table_name)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"分布统计失败：{sanitize_error(str(e))}") from e


__all__ = ["router"]
