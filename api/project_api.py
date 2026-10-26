"""项目（会话分组）：每个项目对应一个本地文件夹。

分工：项目只存「名称 + 本地文件夹路径」，会话正文仍在 MySQL（sessions.project_id）。
文件夹是项目的产物目录 —— 不指定路径时在 `PROJECTS_DIR/<项目名>` 下自动创建，
所以"项目 = 一个独立文件夹"这条约定在磁盘上真实成立，可以整体拷走。

改名与改路径是两件事：
- 只改名字：文件夹不动（用户可能已经在里面放了东西，跟着改名会丢引用）；
- 显式给 folder_path：才按新路径解析/创建。
"""
from __future__ import annotations

from typing import List
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..models.pydantic_schemas import (OkResponse, Project, ProjectCreate,
                                      ProjectUpdate)
from ..utils.logging import get_logger

log = get_logger("basic.api.project")

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _store(request: Request):
    store = getattr(request.app.state, "sessions", request.app.state.store)
    # 项目表只在 MySQL 元数据库里；元数据库不可用时项目面板会 500，
    # 报错写成人能照做的话，比 AttributeError 有用。
    if not hasattr(store, "list_projects"):
        raise HTTPException(503, "项目数据存放在 MySQL 元数据库，当前会话存储已降级为本地文件；"
                                 "请配置 AGENT_MYSQL_* 后重启服务")
    return store


def _to_project(row: dict) -> Project:
    return Project(id=row["id"], name=row["name"],
                   folder_path=row.get("folder_path") or "",
                   session_count=int(row.get("session_count") or 0),
                   is_default=bool(row.get("is_default")),
                   created_at=row.get("created_at", ""),
                   updated_at=row.get("updated_at", ""))


@router.get("", response_model=List[Project])
def list_projects(request: Request):
    return [_to_project(r) for r in _store(request).list_projects()]


@router.post("", response_model=Project)
def create_project(body: ProjectCreate, request: Request):
    store = _store(request)
    folder = (body.folder_path or "").strip()
    name = (body.name or "").strip()
    # 名称默认取文件夹名 —— 这是需求里写明的默认行为
    if not name and folder:
        name = Path(folder).name or folder
    if not name:
        raise HTTPException(422, "请填写项目名，或指定一个文件夹路径")
    exists = [p for p in store.list_projects()
              if p["name"] == name and not folder]
    if exists:
        raise HTTPException(409, f"项目「{name}」已存在，请换个名字")
    if folder:
        target = Path(folder).expanduser()
        dup = [p for p in store.list_projects()
               if p["folder_path"] and Path(p["folder_path"]).resolve()
               == target.resolve()]
        if dup:
            raise HTTPException(409, f"该文件夹已被项目「{dup[0]['name']}」使用")
    try:
        pid = store.create_project(name=name, folder_path=folder)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    log.info("新建项目 %s → %s", name, (store.get_project(pid) or {}).get("folder_path"))
    return _to_project({**(store.get_project(pid) or {}), "session_count": 0})


@router.put("/{project_id}", response_model=Project)
def update_project(project_id: int, body: ProjectUpdate, request: Request):
    store = _store(request)
    current = store.get_project(project_id)
    if not current:
        raise HTTPException(404, "项目不存在")
    if current.get("is_default") and body.name and body.name.strip() != current["name"]:
        raise HTTPException(409, "「未分组」是默认项目，不能改名")
    try:
        store.update_project(project_id, **body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    row = store.get_project(project_id) or {}
    row["session_count"] = next(
        (p["session_count"] for p in store.list_projects() if p["id"] == project_id), 0)
    return _to_project(row)


@router.delete("/{project_id}", response_model=OkResponse)
def delete_project(project_id: int, request: Request):
    """删项目 → 其会话移回「未分组」。刻意不删会话、不删磁盘文件夹。"""
    store = _store(request)
    current = store.get_project(project_id)
    if not current:
        raise HTTPException(404, "项目不存在")
    if current.get("is_default"):
        raise HTTPException(409, "「未分组」是默认项目，不能删除")
    if not store.delete_project(project_id):
        raise HTTPException(400, "删除失败")
    return OkResponse(msg="已删除，原会话已移回「未分组」")
