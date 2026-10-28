"""写操作接口（P0：后端通道，总闸默认关）。

两个端点是一条不可拆的流程：
- POST /api/agent/prepare_write —— 只出「影响面预览」，一个字节都不改；
- POST /api/agent/execute_write —— 人确认之后才真的落刀。

为什么单独一个 /prepare_write 而不是把预览塞进 execute：写下去就没有第二次机会，
人必须在**动手之前**看见"哪张表、大概多少行、备份到哪儿、要不要手输表名"。
前端把这份计划摊开、人点头，才轮到 execute_write。

四条约束在这个文件里落两层（另两层在 write_security 与 QueryExecutor）：
  总闸(AGENT_ALLOW_WRITES) ∧ 数据源上限(allowed_ops) ∧ 本次档位 ∧ confirmed 位

与只读通道的差异（刻意的，不是漏了）：execute_sql 要求 SQL 先绑进会话并落
sql_confirmed，那是为了钉住"意图确认 → 生成 → 二次确认"的四步链路；
写通道 P0 不走 LLM 生成，人工确认的对象是这份执行计划本身，
所以确认位直接来自请求体，会话号只用于审计定位、不参与权限判定。

审计是写通道的前置条件：元数据库连不上时（启动期降级为本地 SQLite）没有
sql_audit 表，宁可 503 拒绝写入，也不出现"改了数据但查无此人"。
预览被拒也落一条 rejected —— 越权试探通常止步于预览，只记执行阶段就等于把它抹掉；
但预览本身不改数据，审计存储不可用时它是"尽力而为"（记日志，不 503）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from ..models.pydantic_schemas import (ExecuteWriteRequest, PrepareWriteRequest,
                                       WriteAuditItem, WriteExecuteResult,
                                       WritePlanResult)
from ..utils.helpers import sanitize_error
from ..utils.logging import get_logger
from .agent_api import _resolve_ds

log = get_logger("basic.api.write")
router = APIRouter(prefix="/api/agent", tags=["agent-write"])


def _audit_store(request: Request):
    """审计存储 = 元数据库（MySQL）。没有 add_audit 能力就说明它不可用。"""
    store = request.app.state.sessions
    if not hasattr(store, "add_audit"):
        raise HTTPException(503, "审计存储不可用（元数据库已降级为本地 SQLite），"
                                "写操作一律拒绝：改完数据查不到是谁改的，比不改更危险")
    return store


def _audit_store_soft(request: Request):
    """预览阶段的审计是"尽力而为"：预览一个字节都不改，元数据库不可用不该因此 503
    （真要写的时候 execute_write 会硬要求审计存储）。"""
    store = request.app.state.sessions
    return store if hasattr(store, "add_audit") else None


def _target_db(ds: Dict[str, Any], body_db: str) -> str:
    db = (body_db or ds.get("database") or "").strip()
    if not db:
        raise HTTPException(400, "未指定目标库：请选择数据源当前库或显式传 db")
    return db


def _record(store: Any, *, ds: Dict[str, Any], db: str, body: PrepareWriteRequest,
            status: str, error: str = "", plan: Optional[Dict[str, Any]] = None,
            result: Optional[Dict[str, Any]] = None) -> int:
    """落一条审计。三态都要落：executed / rejected / failed。

    rejected 也落，是因为"有人反复拿越权档位试探"本身就是要看得见的信号；
    只记成功的话，审计表会伪装成"一切正常"。
    """
    plan = plan or {}
    result = result or {}
    try:
        return int(store.add_audit(
            sql=body.sql, status=status, ds_id=int(ds.get("id") or 0),
            ds_name=ds.get("name") or "", target_db=db,
            session_id=body.session_id or "",
            statement_kind=result.get("statement_kind") or plan.get("statement_kind") or "",
            requested_tier=body.tier,
            effective_tier=result.get("effective_tier") or plan.get("effective_tier") or "",
            confirmed=bool(getattr(body, "confirmed", False)),
            destructive=bool(result.get("destructive") or plan.get("destructive")),
            estimated_rows=result.get("estimated_rows", plan.get("estimated_rows")),
            affected_rows=result.get("affected_rows"),
            backup_table=result.get("backup_table") or plan.get("backup_table") or "",
            rollback_sql=result.get("rollback_sql") or "",
            error=error, cost_ms=int(result.get("cost_ms") or 0)))
    except Exception as e:  # noqa: BLE001  审计写失败不能反过来把已成功的写入报成 500
        log.error("审计写入失败（status=%s, ds=%s）：%s", status, ds.get("id"),
                  sanitize_error(str(e)))
        return 0


# ---------------------------------------------------------------- 1. 影响面预览
@router.post("/prepare_write", response_model=WritePlanResult)
def prepare_write_endpoint(body: PrepareWriteRequest, request: Request):
    ds = _resolve_ds(request, body.ds_id)
    db = _target_db(ds, body.db)
    try:
        plan = request.app.state.executor.prepare_write(
            ds=ds, db=db, sql=body.sql, tier=body.tier)
    except PermissionError as e:
        # 预览被拒也要留痕：拿越权档位反复试探，最先露馅的就是这一步，
        # 只记执行阶段的拒绝等于把"试探"从审计里抹掉。
        store = _audit_store_soft(request)
        if store is not None:
            _record(store, ds=ds, db=db, body=body, status="rejected", error=str(e))
        else:
            log.warning("预览被拒但审计存储不可用，这条没留痕：ds=%s db=%s", ds.get("id"), db)
        # 403 的正文来自安检器/执行器，只有规则名与表名，不含连接信息
        raise HTTPException(403, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, sanitize_error(str(e))) from e
    return WritePlanResult(**plan)


# ---------------------------------------------------------------- 2. 确认后执行
@router.post("/execute_write", response_model=WriteExecuteResult)
def execute_write_endpoint(body: ExecuteWriteRequest, request: Request):
    store = _audit_store(request)
    ds = _resolve_ds(request, body.ds_id)
    db = _target_db(ds, body.db)

    # 确认位在接口层先判：与只读通道同一语义（未确认 = 409，不是 403）
    if not body.confirmed:
        _record(store, ds=ds, db=db, body=body, status="rejected",
                error="未确认（confirmed=false）")
        raise HTTPException(409, "写操作尚未确认：请先预览影响面并勾选确认")

    try:
        result = request.app.state.executor.execute_write(
            ds=ds, db=db, sql=body.sql, tier=body.tier, confirmed=True,
            confirm_phrase=body.confirm_phrase)
    except PermissionError as e:
        _record(store, ds=ds, db=db, body=body, status="rejected", error=str(e))
        raise HTTPException(403, str(e)) from e
    except Exception as e:  # noqa: BLE001  执行器已脱敏，这里兜住引擎侧异常
        msg = sanitize_error(str(e))
        _record(store, ds=ds, db=db, body=body, status="failed", error=msg)
        log.warning("写操作失败 ds=%s db=%s：%s", ds.get("id"), db, msg)
        raise HTTPException(500, msg) from e

    audit_id = _record(store, ds=ds, db=db, body=body, status="executed",
                       result=result)
    log.info("写入完成：%s %s 影响 %s 行（备份表 %s，审计 #%s）",
             result["statement_kind"], db, result["affected_rows"],
             result.get("backup_table") or "-", audit_id)
    return WriteExecuteResult(**result, audit_id=audit_id)


# ---------------------------------------------------------------- 3. 审计查询
@router.get("/write_audit", response_model=List[WriteAuditItem])
def write_audit_endpoint(request: Request,
                         limit: int = Query(50, ge=1, le=200),
                         ds_id: Optional[int] = None,
                         session_id: str = "",
                         status: Optional[str] = None):
    """谁在什么时候对哪张表写了什么、影响多少行、备份在哪、被拒的理由是什么。"""
    store = _audit_store(request)
    rows = store.list_audits(limit=limit, ds_id=ds_id,
                             session_id=session_id or None, status=status)
    return [WriteAuditItem(**r) for r in rows]


__all__ = ["router"]
