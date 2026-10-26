"""获取模型清单（remote-models）回归测试。

覆盖点：
1. list_models() 正确解析 OpenAI 兼容的 {"data":[{"id":...}]}，去重排序、跳过坏项；
2. GET /api/providers/{id}/remote-models：用存量密钥拉清单，密钥不外泄；
3. GET /api/providers/remote-models：按环境变量配置的拉取入口；
4. POST /api/providers/{id}/models/batch：批量登记、已存在的静默跳过、
   首个模型在全局无激活时自动激活。
端点层用假会话存储 + 桩掉的 list_models（不发真实网络请求）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

import basic.api.provider_api as provider_api  # noqa: E402
from basic.core.llm import ModelConfig, list_models  # noqa: E402
from basic.main import create_app  # noqa: E402


# ---------------------------------------------------------------- 1. list_models 解析
class _Resp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok
        self.status_code = 200 if ok else 500

    def json(self):
        return self._payload


def test_list_models_parses_and_dedups(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Resp(
        {"data": [{"id": "b-model"}, {"id": "a-model"}, {"id": "b-model"}, {"x": 1}, None]}))
    cfg = ModelConfig(api_base="https://x/v1", api_key="k")
    ok, ids = list_models(cfg)
    assert ok and ids == ["a-model", "b-model"]     # 排序、去重、跳过坏项


def test_list_models_bad_payload(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Resp({}, ok=False))
    ok, ids = list_models(ModelConfig(api_base="https://x/v1", api_key="k"))
    assert not ok and ids == []
    ok, ids = list_models(ModelConfig(api_base="", api_key=""))   # 配置不全不发请求
    assert not ok and ids == []


# ---------------------------------------------------------------- 端点层夹具
class _FakeProviderStore:
    """只实现 provider_api 用到的方法（会话存储在生产是 MySQL）。"""

    def __init__(self):
        self.models = []                 # 已登记
        self.activated = []

    def provider_secret(self, pid):
        return {"id": pid, "name": "P", "api_base": "https://x/v1",
                "api_key": "sk-secret", "protocol": "/chat/completions"}

    def get_provider(self, pid):
        return {"id": pid, "provider_key": "p", "name": "P",
                "api_base": "https://x/v1", "protocol": "/chat/completions",
                "enabled": True, "has_key": True,
                "models": [dict(m) for m in self.models]}

    def add_provider_model(self, pid, name, make_default=False):
        mid = len(self.models) + 1
        self.models.append({"id": mid, "model_name": name,
                            "is_default": make_default, "is_active": False})
        return mid

    def active_provider_model(self):
        return None

    def set_active_provider_model(self, mid):
        self.activated.append(mid)
        return True


@pytest.fixture
def client():
    app = create_app()
    app.state.sessions = _FakeProviderStore()
    return TestClient(app)


# ---------------------------------------------------------------- 2. 供应商维度拉取
def test_provider_remote_models(client, monkeypatch):
    got: list = []

    def fake_list(cfg):
        got.append(cfg)
        return True, ["m1", "m2"]

    monkeypatch.setattr(provider_api, "list_models", fake_list)
    r = client.get("/api/providers/1/remote-models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["supported"] is True and body["models"] == ["m1", "m2"]
    assert got[0].api_key == "sk-secret"        # 走的是内部明文密钥路径
    assert "sk-secret" not in r.text            # 响应绝不回显密钥


def test_provider_remote_models_unsupported(client, monkeypatch):
    monkeypatch.setattr(provider_api, "list_models", lambda cfg: (False, []))
    r = client.get("/api/providers/1/remote-models")
    assert r.status_code == 200 and r.json()["supported"] is False


def test_provider_remote_models_404(client):
    class _None(_FakeProviderStore):
        def provider_secret(self, pid):
            return None
    client.app.state.sessions = _None()
    assert client.get("/api/providers/99/remote-models").status_code == 404


# ---------------------------------------------------------------- 3. 环境变量维度拉取
def test_env_remote_models(client, monkeypatch):
    monkeypatch.setattr(provider_api, "list_models",
                        lambda cfg: (True, ["qwen3.8-flash"]))
    r = client.get("/api/providers/remote-models")
    assert r.status_code == 200, r.text
    if r.json()["supported"]:            # .env 未配 LLM 时走"未配置"分支
        assert "qwen3.8-flash" in r.json()["models"]


# ---------------------------------------------------------------- 4. 批量登记
def test_add_models_batch_skips_existing_and_activates_first(client):
    store = client.app.state.sessions
    r = client.post("/api/providers/1/models/batch",
                    json={"model_names": ["qwen3.8-flash", " deepseek-v4-pro ", ""]})
    assert r.status_code == 200, r.text
    names = [m["model_name"] for m in r.json()["models"]]
    assert names == ["qwen3.8-flash", "deepseek-v4-pro"]   # trim、丢空
    assert store.activated == [1], "首个模型应在全局未激活时自动激活"

    r2 = client.post("/api/providers/1/models/batch",
                     json={"model_names": ["qwen3.8-flash", "m-new"]})
    names2 = [m["model_name"] for m in r2.json()["models"]]
    assert names2 == ["qwen3.8-flash", "deepseek-v4-pro", "m-new"]  # 重名跳过
    assert store.activated == [1]        # 已有激活候选，不再自动顶
