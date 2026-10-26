"""备用模型切换的回归测试。

这里的核心不是"能不能切"，而是**什么时候不该切**：
主模型返回 403/401（未授权、密钥错）时，换同一把钥匙下的另一个模型必然也失败，
盲目重试只会让每次调用多等一轮超时。这类错误必须直接抛给调用方去走规则兜底。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from basic.core import llm
from basic.core.llm import LLMError, ModelConfig, resolve_fallback

PRIMARY = ModelConfig(name="主模型", api_base="https://x/v1", api_key="k",
                      model="qwen-plus", timeout_s=5)
BACKUP = ModelConfig(name="备用模型", api_base="https://x/v1", api_key="k",
                     model="qwen3.8-max-0902", timeout_s=5)


def _call(cfg, **kw):
    return llm.chat(cfg, system="s", user="u", json_mode=False, **kw)


def test_transient_error_switches_to_fallback(monkeypatch):
    """超时/5xx/429 这类临时故障 → 换备用模型重试并返回它的结果。"""
    calls: list[str] = []

    def fake_post(cfg, **kw):
        calls.append(cfg.model)
        if cfg.model == PRIMARY.model:
            raise LLMError("模型端点不可达：Timeout", retryable=True)
        return "备用模型的回复"

    monkeypatch.setattr(llm, "_post", fake_post)
    assert _call(PRIMARY, fallback=BACKUP) == "备用模型的回复"
    assert calls == [PRIMARY.model, BACKUP.model], "应当只重试一次，且顺序是先主后备"


def test_auth_error_does_not_switch(monkeypatch):
    """403（模型未授权）不该触发切换：同密钥换模型必然同样失败。"""
    calls: list[str] = []

    def fake_post(cfg, **kw):
        calls.append(cfg.model)
        raise LLMError("模型端点返回 403", retryable=False)

    monkeypatch.setattr(llm, "_post", fake_post)
    with pytest.raises(LLMError) as ei:
        _call(PRIMARY, fallback=BACKUP)
    assert "403" in str(ei.value)
    assert calls == [PRIMARY.model], "403 不应调用备用模型"


def test_fallback_failure_propagates(monkeypatch):
    """备用模型也挂了 → 抛备用模型的错误（而不是把主模型的错误丢掉）。"""

    def fake_post(cfg, **kw):
        if cfg.model == PRIMARY.model:
            raise LLMError("模型端点返回 503", retryable=True)
        raise LLMError("模型端点返回 500", retryable=True)

    monkeypatch.setattr(llm, "_post", fake_post)
    with pytest.raises(LLMError) as ei:
        _call(PRIMARY, fallback=BACKUP)
    assert "500" in str(ei.value)


def test_no_fallback_keeps_original_error(monkeypatch):
    def fake_post(cfg, **kw):
        raise LLMError("模型端点返回 503", retryable=True)

    monkeypatch.setattr(llm, "_post", fake_post)
    with pytest.raises(LLMError) as ei:
        _call(PRIMARY)
    assert "503" in str(ei.value)


def test_unready_fallback_is_skipped(monkeypatch):
    """备用模型配置残缺（缺 key/endpoint）时不能拿它去重试。"""
    broken = ModelConfig(name="残缺", api_base="", api_key="", model="m")
    calls: list[str] = []

    def fake_post(cfg, **kw):
        calls.append(cfg.model)
        raise LLMError("模型端点返回 503", retryable=True)

    monkeypatch.setattr(llm, "_post", fake_post)
    with pytest.raises(LLMError):
        _call(PRIMARY, fallback=broken)
    assert calls == [PRIMARY.model]


def test_chat_json_passes_fallback(monkeypatch):
    """chat_json 的两次尝试都要带上备用模型（否则第一步失败就丢了切换能力）。"""
    seen: list[tuple[str, bool]] = []

    def fake_chat(cfg, *, json_mode, fallback=None, **kw):
        seen.append((cfg.model, fallback is not None))
        return '{"ok": 1}' if json_mode else '{"ok": 1}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    llm.chat_json(PRIMARY, system="s", user="u", fallback=BACKUP)
    assert seen and all(has_flag for _, has_flag in seen)


def test_resolve_fallback_from_settings():
    st = SimpleNamespace(llm_fallback_model="qwen3.8-max-0902",
                         llm_api_base="https://x/v1/", llm_api_key="k",
                         llm_timeout_s=30)
    cfg = resolve_fallback(st)
    assert cfg is not None and cfg.model == "qwen3.8-max-0902"
    assert cfg.api_base == "https://x/v1", "末尾斜杠要归一，否则拼出 //chat/completions"
    assert cfg.ready is True


def test_resolve_fallback_empty_returns_none():
    st = SimpleNamespace(llm_fallback_model="  ", llm_api_base="https://x/v1",
                         llm_api_key="k", llm_timeout_s=30)
    assert resolve_fallback(st) is None


def test_probe_reports_model_not_entitled(monkeypatch):
    """GET /models 成功但模型 403 —— 必须报"未授权"，不能说"连接正常"。

    这正是本项目接入时的真实情况：一个 Key 能列出 178 个模型，
    每个模型调起来都是 403 AccessDenied.Unpurchased。
    """
    import requests as _rq

    class _Resp:
        status_code = 200

    monkeypatch.setattr(_rq, "get", lambda *a, **kw: _Resp())

    def fake_chat(cfg, **kw):
        raise LLMError("模型端点返回 403", retryable=False, status=403)

    monkeypatch.setattr(llm, "chat", fake_chat)
    ok, msg = llm.probe(BACKUP)
    assert ok is False
    assert "未授权" in msg or "未开通" in msg, msg


def test_probe_reports_bad_key(monkeypatch):
    """GET /models 就 401 → 报密钥问题（与"模型未开通"要区分开）。"""
    import requests as _rq

    class _Resp:
        status_code = 401

    monkeypatch.setattr(_rq, "get", lambda *a, **kw: _Resp())
    ok, msg = llm.probe(BACKUP)
    assert ok is False and "密钥" in msg


def test_probe_ok_when_chat_succeeds(monkeypatch):
    import requests as _rq

    class _Resp:
        status_code = 404          # 端点不提供 /models，属正常差异

    monkeypatch.setattr(_rq, "get", lambda *a, **kw: _Resp())
    monkeypatch.setattr(llm, "chat", lambda cfg, **kw: "OK")
    ok, msg = llm.probe(BACKUP)
    assert ok is True and BACKUP.model in msg


def test_probe_incomplete_config():
    ok, msg = llm.probe(ModelConfig(model="m"))
    assert ok is False and "配置不完整" in msg


def test_retryable_flags_by_status():
    """状态码 → 可重试性 的判定表（这是整个切换策略的依据）。"""
    retryable = {408, 429, 500, 502, 503, 504}
    for code in (400, 401, 403, 404, 422):
        assert code not in retryable, f"{code} 属于配置/授权错误，不该重试"
    for code in retryable:
        assert code in llm._RETRYABLE_STATUS
