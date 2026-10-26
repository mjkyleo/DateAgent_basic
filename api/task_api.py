"""任务清单接口：把分析给出的建议变成「人可编辑、可调序、可批量执行」的待办。

清单按项目一份，存在 MySQL 的 project_tasks（见 storage/mysql_store.py 的任务章节）。
元数据库不可用时全部返回 503 —— 与模型/项目面板同一口径，降级存储没有任务表，
不假装能用。

批量执行（POST /api/tasks/run）的安全边界，逐条对应"只读 + 双层人工确认"的约定：
- 只跑**绑了 SQL** 的任务；没绑的明确跳过并给原因，由人标记完成（不假装跑了）；
- 必须带 `confirmed=true`（界面勾选"我已核对待执行的 SQL"）—— 这就是第二层确认本身，
  绕过界面的裸调用一律 409，和 /api/agent/execute_sql 同一条规矩；
- 每条任务落一条会话，回写 SQL 与查询结果：数字永远可以回看、可以溯源；
- 真正执行仍走 QueryExecutor：内部再次的 SELECT-only 安全门 + 超时 + 行数封顶。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from ..models.pydantic_schemas import (OkResponse, Task, TaskCreate,
                                      TaskReorderRequest, TaskRunItem,
                                      TaskRunRequest, TaskRunResult, TaskUpdate)
from ..utils.helpers import sanitize_error
from ..utils.logging import get_logger
# 复用 agent_api 的存储能力垫片：任务同样要兼容"MySQL 正常 / 已降级 SQLite"两种存储，
# 这两处逻辑（项目解析、建会话）必须和主链路一致，复制一份迟早各改各的。
from .agent_api import _create_session, _project_of

log = get_logger("basic.api.task")
router = APIRouter(prefix="/api/tasks", tags=["tasks"])

#: 单次批量执行的条数上限。单条查询已有超时兜底，但几十条串起来会把请求
#: 拖到网关超时、前端只看到一个空白错误 —— 宁可当场拒绝并说明分两批。
MAX_RUN_PER_REQUEST = 50


def _store(request: Request):
    store = getattr(request.app.state, "sessions", request.app.state.store)
    # 任务表只在 MySQL 元数据库里；错误写成人能照做的话，比 AttributeError 有用
    if not hasattr(store, "list_tasks"):
        raise HTTPException(503, "任务清单存放在 MySQL 元数据库，当前会话存储已降级为本地文件；"
                                 "请配置 AGENT_MYSQL_* 后重启服务")
    return store


def _to_task(row: Dict[str, Any]) -> Task:
    """JSON 列可能装着非对象值（历史脏数据）→ 收敛成 None，别让整张列表 500。"""
    return Task(**{**row,
                   "source_ref": row.get("source_ref")
                   if isinstance(row.get("source_ref"), dict) else None,
                   "last_result": row.get("last_result")
                   if isinstance(row.get("last_result"), dict) else None})


def _ds_for_task(request: Request, ds_id: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """定位任务要查的数据源，返回 (配置, 不可用原因)。

    显式绑定过的源必须还在：源被删了就跳过并说明，绝不能悄悄退回"当前激活源"去查
    —— 用户看到的建议来自 A 库，结果却是 B 库的数字，那是最坏的一种"成功"。
    没绑定过（老任务/手工新建）才用激活源，并把源名记进结果，让数字带上出处。
    """
    store = request.app.state.store
    if ds_id:
        ds = store.get_datasource(int(ds_id))
        return (ds, "") if ds else (None, f"任务绑定的数据源 #{ds_id} 已删除，请重新绑定或改用当前激活源")
    ds = store.get_active()
    return (ds, "") if ds else (None, "尚未配置或激活数据源：请先新增连接并切换激活")


def _active_ds_id(request: Request) -> int:
    """当前激活数据源 id（没有则 0）：新建任务时顺手记下出处，不强制先配源。"""
    active = request.app.state.store.get_active()
    return int(active.get("id") or 0) if active else 0


# ---------------------------------------------------------------- CRUD
@router.get("", response_model=List[Task])
def list_tasks(request: Request, project_id: Optional[int] = None):
    store = _store(request)
    return [_to_task(t) for t in store.list_tasks(_project_of(store, project_id))]


@router.post("", response_model=Task)
def create_task(body: TaskCreate, request: Request):
    store = _store(request)
    pid = _project_of(store, body.project_id)
    ds_id = body.ds_id or _active_ds_id(request)
    try:
        tid = store.create_task(project_id=pid, title=body.title, ds_id=ds_id,
                                note=body.note, source_kind=body.source_kind,
                                source_ref=body.source_ref, question=body.question,
                                sql=body.sql)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _to_task(store.get_task(tid) or {})


@router.put("/{task_id}", response_model=Task)
def update_task(task_id: int, body: TaskUpdate, request: Request):
    store = _store(request)
    current = store.get_task(task_id)
    if not current:
        raise HTTPException(404, "任务不存在")
    # exclude_unset：没传的字段保持原值。编辑标题时把 note 抹成空串是另一种 bug
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    try:
        row = store.update_task(task_id, **fields)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _to_task(row or current)


@router.delete("/{task_id}", response_model=OkResponse)
def delete_task(task_id: int, request: Request):
    """删任务。只删清单里这一行 —— 它跑出来的会话属于历史，保留。"""
    store = _store(request)
    if not store.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    if not store.delete_task(task_id):
        raise HTTPException(400, "删除失败")
    return OkResponse(msg="已从清单删除")


# ---------------------------------------------------------------- 调序
@router.post("/reorder", response_model=List[Task])
def reorder_tasks(body: TaskReorderRequest, request: Request):
    """提交调序后的完整顺序（↑↓ 每点一次提交整份，避免逐条 +1/-1 的并发错位）。"""
    store = _store(request)
    pid = _project_of(store, body.project_id)
    return [_to_task(t) for t in store.reorder_tasks(pid, body.ids)]


# ---------------------------------------------------------------- 批量执行
@router.post("/run", response_model=TaskRunResult)
def run_tasks(body: TaskRunRequest, request: Request):
    if not body.confirmed:
        raise HTTPException(409, "批量执行需要先确认：请勾选「我已核对待执行的 SQL」")
    store = _store(request)
    pid = _project_of(store, body.project_id)
    tasks = store.list_tasks(pid)
    wanted = {int(i) for i in body.ids or []}
    if wanted:
        picked = [t for t in tasks if int(t["id"]) in wanted]
        missing = wanted - {int(t["id"]) for t in picked}
        if missing:
            # 界面带着旧清单点了执行：明确报错，不能静默少跑几条
            raise HTTPException(404, f"有 {len(missing)} 条任务不属于本项目或已被删除，请刷新清单")
    else:
        # 不筛选"有 SQL 的"：没绑 SQL 的未完成任务也要逐条给出跳过原因 ——
        # "统一执行"把不该悄悄忽略的条目隐形掉，用户看到的是"跑完了"，其实是没跑。
        picked = [t for t in tasks if t.get("status") != "done"]
    if len(picked) > MAX_RUN_PER_REQUEST:
        raise HTTPException(422, f"一次最多执行 {MAX_RUN_PER_REQUEST} 条（本次 {len(picked)} 条），请分批")

    items = [_run_one(request, store, t) for t in picked]
    ran = [i for i in items if i.ran]
    ok = [i for i in ran if i.status == "done"]
    failed = [i for i in ran if i.status == "failed"]
    skipped = [i for i in items if not i.ran]
    log.info("项目 %s 批量执行任务：跑 %d（成功 %d / 失败 %d）、跳过 %d",
             pid, len(ran), len(ok), len(failed), len(skipped))
    return TaskRunResult(items=items, ran_count=len(ran), failed_count=len(failed),
                         skipped_count=len(skipped),
                         msg=(f"已执行 {len(ran)} 条（成功 {len(ok)}、失败 {len(failed)}），"
                              f"跳过 {len(skipped)} 条"))


def _run_one(request: Request, store: Any, task: Dict[str, Any]) -> TaskRunItem:
    tid = int(task["id"])
    title = task.get("title") or ""
    sql = (task.get("sql") or "").strip()
    if not sql:
        return TaskRunItem(id=tid, title=title, ran=False, status=task.get("status") or "todo",
                           reason="未绑定 SQL：这类任务需要人工办结，做完请标记「已完成」")
    if task.get("status") == "done":
        return TaskRunItem(id=tid, title=title, ran=False, status="done",
                           reason="已完成，跳过（需要重跑请改回「待办」）")
    ds, why = _ds_for_task(request, int(task.get("ds_id") or 0))
    if not ds:
        return TaskRunItem(id=tid, title=title, ran=False,
                           status=task.get("status") or "todo", reason=why)

    # 一条任务 = 一轮会话：SQL、结果、用的哪个源全部落在 sessions 里，
    # 清单只显示摘要，点「查看结果」回看的就是这轮真实历史。
    sid = _create_session(store, ds_id=int(ds["id"]),
                          user_question=(task.get("question") or "").strip() or title,
                          intent="任务清单批量执行", project_id=int(task["project_id"] or 0),
                          title=title[:255])
    store.update_task(tid, status="running", session_id=sid)
    # 确认状态先落库再执行：万一中途崩溃，会话留下的记录也是"人已确认、跑挂了"
    # 而不是"没确认就跑了"（execute_sql 那道 409 门读的就是这个标记）。
    store.update_session(sid, sql=sql, sql_confirmed=True)
    try:
        result = request.app.state.executor.execute(ds=ds, sql=sql)
    except Exception as e:  # noqa: BLE001  PermissionError（安全门）与执行错误同等对待
        detail = sanitize_error(str(e))
        store.update_task(tid, status="failed",
                          last_result={"error": detail, "ds_name": ds.get("name") or "",
                                       "finished_at": _stamp()})
        return TaskRunItem(id=tid, title=title, ran=True, status="failed",
                           session_id=sid, error=detail)
    store.update_session(sid, query_result=result.model_dump(mode="json"))
    store.update_task(tid, status="done",
                      last_result={"row_count": result.row_count, "cost_ms": result.cost_ms,
                                   "truncated": bool(result.truncated),
                                   "ds_name": ds.get("name") or "",
                                   "finished_at": _stamp()})
    return TaskRunItem(id=tid, title=title, ran=True, status="done", session_id=sid,
                       row_count=result.row_count)


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
