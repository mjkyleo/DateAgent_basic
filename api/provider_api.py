"""模型供应商接入（设计需求：多供应商 + 每家 1:N 模型）。

为什么是"供应商"而不是"模型"这一层：同一家厂商的接口地址、密钥、协议都一样，
变的是模型名。把这三样抽到供应商上，加模型只需填一个名字；
换厂商只改供应商，模型清单跟着走。

路由约定（请求 URL = 接口地址 + 协议路径）：
    https://api.deepseek.com/v1  +  /chat/completions
    https://api.moonshot.cn/v1   +  /chat/completions
所以「接口地址」要含版本前缀（/v1、/api/paas/v4 等），协议只写 /chat/completions。

密钥策略与数据源一致：只写不读。列表只回 has_key，
真正要用密钥的内部路径走 store.provider_secret()。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from ..core.llm import ModelConfig, list_models, probe, probe_endpoint, resolve_model
from ..models.pydantic_schemas import (AddModelRequest, AddModelsBatchRequest, OkResponse,
                                       Provider, ProviderCreate, ProviderTestRequest,
                                       ProviderUpdate, RemoteModelsResponse,
                                       TestConnectionResponse)

router = APIRouter(prefix="/api/providers", tags=["providers"])

# 供应商/模型清单落在 MySQL 元数据库里；连不上时启动会降级 SQLite（见 main._init_session_store），
# 而 SQLite 形态没有这套表。与其让 AttributeError 冒成带栈的 500，不如给一句能照做的提示。
_META_REQUIRED = ("供应商与模型清单需要 MySQL 元数据库，当前会话存储已降级为本地文件；"
                  "请配置 AGENT_MYSQL_* （或保留一个已配好的 MySQL 数据源）后重启服务")


def _store(request: Request):
    store = getattr(request.app.state, "sessions", request.app.state.store)
    if not hasattr(store, "provider_secret"):
        raise HTTPException(503, _META_REQUIRED)
    return store


def _to_provider(row: Dict[str, Any]) -> Provider:
    return Provider(id=row["id"], provider_key=row["provider_key"],
                    name=row["name"], api_base=row["api_base"],
                    protocol=row.get("protocol") or "/chat/completions",
                    enabled=bool(row.get("enabled", True)),
                    has_key=bool(row.get("has_key")),
                    models=row.get("models") or [],
                    created_at=row.get("created_at", ""),
                    updated_at=row.get("updated_at", ""))


def _cfg_of(row: Dict[str, Any], model: str = "") -> ModelConfig:
    """供应商行（含明文密钥）+ 模型名 → 调用配置。仅服务端内部使用。"""
    return ModelConfig(name=row.get("name") or row.get("provider_key") or "",
                       api_base=(row.get("api_base") or "").rstrip("/"),
                       protocol=row.get("protocol") or "/chat/completions",
                       api_key=row.get("api_key") or "",
                       model=model,
                       timeout_s=30)


# ------------------------------------------------------------------ 列表/新建
@router.get("", response_model=List[Provider])
def list_providers(request: Request):
    return [_to_provider(r) for r in _store(request).list_providers()]


@router.post("", response_model=Provider)
def create_provider(body: ProviderCreate, request: Request):
    store = _store(request)
    key = body.provider_key.strip()
    if any(p["provider_key"] == key for p in store.list_providers()):
        raise HTTPException(409, f"供应商 ID「{key}」已存在，请换一个")
    pid = store.create_provider(provider_key=key, name=body.name.strip(),
                                api_base=body.api_base.strip(),
                                api_key=body.api_key,
                                protocol=body.protocol or "/chat/completions",
                                enabled=body.enabled)
    # 顺带登记的模型：第一个设为该供应商默认；若全局还没有激活模型，直接激活它
    names = [m.strip() for m in body.models if m.strip()]
    first_mid = 0
    for i, name in enumerate(names):
        mid = store.add_provider_model(pid, name, make_default=(i == 0))
        if i == 0:
            first_mid = mid
    if first_mid and not store.active_provider_model():
        store.set_active_provider_model(first_mid)
    return _to_provider(store.get_provider(pid) or {})


# ------------------------------------------------------------------ 草稿测试
@router.post("/test", response_model=TestConnectionResponse)
def test_draft(body: ProviderTestRequest, request: Request):
    """表单未保存时的连通性测试。

    api_key 留空且给了 provider_id 时复用已存的密钥 —— 编辑供应商时
    密钥框是空的（不回显），不能因此判定"没填"。
    """
    store = _store(request)
    key = body.api_key
    if not key and body.provider_id:
        saved = store.provider_secret(body.provider_id)
        key = (saved or {}).get("api_key") or ""
    if not key:
        return TestConnectionResponse(connected=False, msg="请填写 API Key")
    cfg = ModelConfig(api_base=body.api_base.strip().rstrip("/"),
                      protocol=body.protocol or "/chat/completions",
                      api_key=key, model=body.model.strip(), timeout_s=20)
    if not cfg.model:
        ok, msg = probe_endpoint(cfg)
        return TestConnectionResponse(
            connected=ok,
            msg=msg if ok else msg,
        )
    ok, msg = probe(cfg)
    return TestConnectionResponse(connected=ok, msg=msg)


@router.get("/active")
def active_provider_model(request: Request):
    """当前 Agent 使用的 (供应商, 模型)。不含密钥。"""
    row = _store(request).active_provider_model()
    if not row:
        return {"provider": None, "model": None}
    provider = _store(request).get_provider(row["provider_id"])
    return {
        "provider": _to_provider(provider) if provider else None,
        "model": {"id": row["model_id"], "model_name": row["model_name"]},
    }


# ------------------------------------------------------------------ 模型清单拉取
@router.get("/remote-models", response_model=RemoteModelsResponse)
def env_remote_models(request: Request):
    """按环境变量配置（LLM_API_BASE/KEY）拉取模型清单，无需先接入供应商。"""
    st = request.app.state.settings
    cfg = ModelConfig(api_base=(st.llm_api_base or "").rstrip("/"),
                      api_key=st.llm_api_key or "", timeout_s=20)
    if not cfg.api_base or not cfg.api_key:
        return RemoteModelsResponse(supported=False,
                                    msg="未配置 LLM_API_BASE/LLM_API_KEY，无清单可拉")
    ok, models = list_models(cfg)
    if not ok:
        return RemoteModelsResponse(supported=False,
                                    msg="端点未提供模型清单（或密钥无效/网络不可达），请手动填写模型名")
    return RemoteModelsResponse(supported=True, models=models,
                                msg=f"共 {len(models)} 个模型；列表可调用性以逐个测试为准")


@router.get("/{provider_id}/remote-models", response_model=RemoteModelsResponse)
def provider_remote_models(provider_id: int, request: Request):
    """拉取已接入供应商的模型清单（用其存量密钥，不外泄）。"""
    store = _store(request)
    row = store.provider_secret(provider_id)
    if not row:
        raise HTTPException(404, "供应商不存在")
    ok, models = list_models(_cfg_of(row))
    if not ok:
        return RemoteModelsResponse(supported=False,
                                    msg="该端点未提供模型清单（或密钥无效/网络不可达），请手动填写模型名")
    return RemoteModelsResponse(supported=True, models=models,
                                msg=f"共 {len(models)} 个模型；列表里有 ≠ 有调用权限，以逐个测试为准")


# ------------------------------------------------------------------ 单条操作
@router.put("/{provider_id}", response_model=Provider)
def update_provider(provider_id: int, body: ProviderUpdate, request: Request):
    store = _store(request)
    if not store.get_provider(provider_id):
        raise HTTPException(404, "供应商不存在")
    if body.provider_key:
        other = [p for p in store.list_providers()
                 if p["provider_key"] == body.provider_key.strip()
                 and p["id"] != provider_id]
        if other:
            raise HTTPException(409, f"供应商 ID「{body.provider_key}」已被占用")
    store.update_provider(provider_id, **body.model_dump(exclude_unset=True))
    return _to_provider(store.get_provider(provider_id) or {})


@router.delete("/{provider_id}", response_model=OkResponse)
def delete_provider(provider_id: int, request: Request):
    if not _store(request).delete_provider(provider_id):
        raise HTTPException(404, "供应商不存在")
    return OkResponse(msg="已删除")


@router.post("/{provider_id}/test", response_model=TestConnectionResponse)
def test_provider(provider_id: int, request: Request, model: str = ""):
    """测试已保存的供应商。传 model 则验证该模型是否真能调用。"""
    store = _store(request)
    row = store.provider_secret(provider_id)
    if not row:
        raise HTTPException(404, "供应商不存在")
    if not model:
        # 没指定就取它的默认模型，尽量给一个确定结论
        models = (store.get_provider(provider_id) or {}).get("models") or []
        defaults = [m for m in models if m.get("is_default")] or models
        model = (defaults[0]["model_name"] if defaults else "")
    ok, msg = probe(_cfg_of(row, model)) if model else probe_endpoint(_cfg_of(row))
    return TestConnectionResponse(connected=ok, msg=msg)


# ------------------------------------------------------------------ 模型清单
@router.post("/{provider_id}/models", response_model=Provider)
def add_model(provider_id: int, body: AddModelRequest, request: Request):
    store = _store(request)
    provider = store.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, "供应商不存在")
    name = body.model_name.strip()
    if any(m["model_name"] == name for m in provider.get("models") or []):
        raise HTTPException(409, f"模型「{name}」已在该供应商下")
    first = not (provider.get("models") or [])
    mid = store.add_provider_model(provider_id, name, make_default=first)
    if first and not store.active_provider_model():
        store.set_active_provider_model(mid)
    return _to_provider(store.get_provider(provider_id) or {})


@router.post("/{provider_id}/models/batch", response_model=Provider)
def add_models_batch(provider_id: int, body: AddModelsBatchRequest, request: Request):
    """把拉取清单里勾选的模型一次登记（重名的静默跳过——从清单来的本就可能已存在）。"""
    store = _store(request)
    provider = store.get_provider(provider_id)
    if not provider:
        raise HTTPException(404, "供应商不存在")
    existing = {m["model_name"] for m in provider.get("models") or []}
    first = not existing
    for name in [n.strip() for n in body.model_names if n.strip()]:
        if name in existing:
            continue
        mid = store.add_provider_model(provider_id, name, make_default=first)
        if first:
            first = False
            if not store.active_provider_model():
                store.set_active_provider_model(mid)
    return _to_provider(store.get_provider(provider_id) or {})


@router.delete("/{provider_id}/models/{model_id}", response_model=Provider)
def delete_model(provider_id: int, model_id: int, request: Request):
    store = _store(request)
    if not store.get_provider(provider_id):
        raise HTTPException(404, "供应商不存在")
    if not store.delete_provider_model(model_id):
        raise HTTPException(404, "模型不存在")
    return _to_provider(store.get_provider(provider_id) or {})


@router.post("/{provider_id}/models/{model_id}/activate", response_model=Provider)
def activate_model(provider_id: int, model_id: int, request: Request):
    """把某个模型设为当前 Agent 使用（同时成为该供应商的默认模型）。"""
    store = _store(request)
    if not store.set_active_provider_model(model_id):
        raise HTTPException(404, "模型不存在")
    return _to_provider(store.get_provider(provider_id) or {})
