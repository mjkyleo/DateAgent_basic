"""LLM 客户端：OpenAI 兼容 chat/completions 薄封装 + 模型配置解析。

为什么单独抽一层：项目里现在有三处要用 LLM（意图解析、SQL 优化解读、数据源描述），
如果各自 requests.post，超时/JSON 容错/密钥处理就会散落三份。统一走这里的两个入口：

- chat_json()：要结构化 JSON（意图解析、优化建议结构化）；
- chat_text()：要成段文字（数据源的业务场景描述）。

模型配置从哪来（resolve_model）：
1. MySQL llm_models 表里被激活的模型（用户在前端「模型管理」里接入并激活）；
2. 否则用环境变量 LLM_API_BASE/KEY/MODEL；
3. 都没有 → 返回未就绪配置，调用方走规则兜底（不抛异常，让上层决定降级）。

失败策略：本模块只负责把错误抛清楚（带类型与状态码摘要，不含密钥），
是否降级由调用方决定 —— 意图解析有规则兜底，优化建议有规则结论，描述有空态文案。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..utils.logging import get_logger

log = get_logger("basic.core.llm")


@dataclass
class ModelConfig:
    """一个可用的模型端点。ready=False 表示没配任何模型。

    `protocol` 是接口协议路径，拼在 api_base 之后（默认 OpenAI 兼容的
    `/chat/completions`）。单独拆出来是为了后续能接别的协议，
    而不是把路径硬编码在请求代码里。
    """
    name: str = ""
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    timeout_s: int = 60
    protocol: str = "/chat/completions"

    @property
    def ready(self) -> bool:
        return bool(self.api_base and self.api_key and self.model)

    @property
    def url(self) -> str:
        """完整请求地址 = 接口地址 + 协议路径（缺前导斜杠时补上）。"""
        path = self.protocol or "/chat/completions"
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_base}{path}"

    @property
    def label(self) -> str:
        """给人看的名字：优先用户起的名字，退回模型 ID。"""
        return self.name or self.model or "未配置模型"


def resolve_model(sessions_store: Any, settings: Any) -> ModelConfig:
    """解析当前应使用的模型：激活的 (供应商, 模型) > 环境变量 > 空配置。

    注意必须走 `active_provider_model()`（含明文密钥）而不是脱敏接口——
    后者会让"接入了模型却永远用不上"，且不报错，只是静默回落到环境变量（踩过）。
    """
    timeout = int(getattr(settings, "llm_timeout_s", 60) or 60)
    if sessions_store is not None and hasattr(sessions_store, "active_provider_model"):
        try:
            active = sessions_store.active_provider_model()
        except Exception as e:  # noqa: BLE001  模型表读不到不能阻断链路
            log.warning("读取激活模型失败，退回环境变量：%s", type(e).__name__)
            active = None
        if active and active.get("api_base") and active.get("api_key") \
                and active.get("model_name"):
            return ModelConfig(name=f"{active.get('provider_name')} / "
                                    f"{active.get('model_name')}",
                               api_base=(active.get("api_base") or "").rstrip("/"),
                               protocol=active.get("protocol") or "/chat/completions",
                               api_key=active.get("api_key") or "",
                               model=active.get("model_name") or "",
                               timeout_s=timeout)
    if getattr(settings, "llm_enabled", False):
        return ModelConfig(name=settings.llm_model,
                           api_base=(settings.llm_api_base or "").rstrip("/"),
                           api_key=settings.llm_api_key or "",
                           model=settings.llm_model or "",
                           timeout_s=timeout,
                           protocol="/chat/completions")
    return ModelConfig(timeout_s=timeout)


class LLMError(RuntimeError):
    """LLM 调用失败（原因可读，且不含密钥）。

    retryable 标记"换个模型重试有没有意义"：
    - True：超时 / 连接失败 / 5xx / 429（对方临时有问题，备用模型大概率也受影响，
      但值得一试——尤其是主模型额度耗尽而备用模型额度充足时）；
    - False：400/401/403/404（参数错、密钥无效、**模型未授权**）。
      这类错误换同一把钥匙下的另一个模型不会好，盲目重试只会让每次调用都多等一轮超时。
    """

    def __init__(self, message: str, *, retryable: bool = False,
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        #: HTTP 状态码（仅状态类错误有值）：调用方要据此区分
        #: "密钥无效"和"模型未授权"——两者都给用户看同一句"403"没有帮助。
        self.status = status


#: 这些状态码属于"对方临时不行"，值得换模型再试一次
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def resolve_fallback(settings: Any) -> Optional[ModelConfig]:
    """备用模型配置：取 `LLM_FALLBACK_MODEL`，与主模型同端点同密钥。

    刻意只支持"同端点换模型 ID"：换端点/密钥是另一件事，应该用前端「模型管理」
    激活另一个模型（那里能分别测试连通性），而不是塞进同一个环境变量里猜。
    """
    model_id = (getattr(settings, "llm_fallback_model", "") or "").strip()
    if not model_id:
        return None
    return ModelConfig(name=model_id,
                       api_base=(getattr(settings, "llm_api_base", "") or "").rstrip("/"),
                       api_key=getattr(settings, "llm_api_key", "") or "",
                       model=model_id,
                       timeout_s=int(getattr(settings, "llm_timeout_s", 60) or 60),
                       protocol="/chat/completions")


def chat(cfg: ModelConfig, *, system: str, user: str,
         temperature: float = 0.0, json_mode: bool = True,
         fallback: Optional[ModelConfig] = None) -> str:
    """OpenAI 兼容 chat/completions 调用，返回文本内容。

    主模型失败时，若配了备用模型且错误是"可重试"类，自动切过去重试一次；
    否则原样抛出，由调用方走各自的降级路径（意图解析→规则兜底，描述→规则摘要）。
    """
    try:
        return _post(cfg, system=system, user=user, temperature=temperature,
                     json_mode=json_mode)
    except LLMError as e:
        if fallback is None or not fallback.ready or not e.retryable:
            raise
        log.warning("主模型 %s 不可用（%s），切换至备用模型 %s 重试",
                    cfg.model or "(未命名)", e, fallback.model)
        return _post(fallback, system=system, user=user, temperature=temperature,
                     json_mode=json_mode)


def _post(cfg: ModelConfig, *, system: str, user: str,
          temperature: float, json_mode: bool) -> str:
    """单次请求（不含重试）。"""
    if not cfg.ready:
        raise LLMError("尚未配置可用的模型：请在「模型管理」中接入并激活，或设置 LLM_API_KEY")
    import requests

    url = cfg.url
    payload: Dict[str, Any] = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = requests.post(url, json=payload, timeout=cfg.timeout_s,
                             headers={"Authorization": f"Bearer {cfg.api_key}"})
    except requests.RequestException as e:
        raise LLMError(f"模型端点不可达：{type(e).__name__}", retryable=True) from e
    if resp.status_code >= 400:
        # 只回状态码，不回响应体（可能回显请求内容）
        raise LLMError(f"模型端点返回 {resp.status_code}",
                       retryable=resp.status_code in _RETRYABLE_STATUS,
                       status=resp.status_code)
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError(f"模型响应结构异常：{type(e).__name__}", retryable=True) from e
    if not content.strip():
        raise LLMError("模型返回空内容", retryable=True)
    return content


def chat_json(cfg: ModelConfig, *, system: str, user: str,
              fallback: Optional[ModelConfig] = None) -> Dict[str, Any]:
    """要 JSON 的调用。容忍 markdown 代码块包裹；解析失败时自动重试一次（禁 json_mode）。"""
    content = chat(cfg, system=system, user=user, json_mode=True, fallback=fallback)
    parsed = _parse_json_loose(content)
    if parsed:
        return parsed
    # 少数兼容端点不认 response_format，会返回带解释的文本 —— 去掉该参数再要一次
    content = chat(cfg, system=system, user=user, json_mode=False, fallback=fallback)
    parsed = _parse_json_loose(content)
    if parsed:
        return parsed
    raise LLMError("模型未返回可解析的 JSON")


def chat_text(cfg: ModelConfig, *, system: str, user: str,
              temperature: float = 0.3,
              fallback: Optional[ModelConfig] = None) -> str:
    """要成段文字的调用（描述/解读类）。"""
    return chat(cfg, system=system, user=user, temperature=temperature,
                json_mode=False, fallback=fallback)


def probe_endpoint(cfg: ModelConfig) -> tuple[bool, str]:
    """只验证「端点可达 + 密钥有效」（`GET /models`），不验证具体模型。

    表单里「测试连接」时用户还没填模型名，只能验到这一层；
    要说"这个模型能用"必须走 probe()。
    """
    if not cfg.api_base or not cfg.api_key:
        return False, "配置不完整：需要接口地址与 API Key"
    import requests

    try:
        r = requests.get(f"{cfg.api_base}/models", timeout=min(cfg.timeout_s, 15),
                         headers={"Authorization": f"Bearer {cfg.api_key}"})
        if r.status_code in (401, 403):
            return False, f"密钥无权限（GET /models 返回 {r.status_code}），请检查 API Key"
        if r.status_code >= 500:
            return False, f"服务端错误（GET /models 返回 {r.status_code}）"
        # 404/405/501 表示端点不提供该路径，属正常差异
        return True, "端点可达、密钥有效"
    except requests.RequestException as e:
        return False, f"端点不可达：{type(e).__name__}"


def list_models(cfg: ModelConfig) -> tuple[bool, list[str]]:
    """拉取端点的模型清单（OpenAI 兼容 `GET /models`），返回 (成功?, 模型 ID 列表)。

    注意语义边界：列表里有 ≠ 有调用权限 —— probe() 的注释记录过实际教训
    （一个 Key 能列出 178 个模型，逐个调全是 403 Unpurchased）。
    所以这里只做"看得见"，"用得上"仍要靠登记后的 probe/最小对话验证。
    """
    if not cfg.api_base or not cfg.api_key:
        return False, []
    import requests

    try:
        r = requests.get(f"{cfg.api_base}/models", timeout=min(cfg.timeout_s, 15),
                         headers={"Authorization": f"Bearer {cfg.api_key}"})
    except requests.RequestException:
        return False, []
    if not r.ok:
        return False, []
    try:
        data = r.json().get("data")
    except ValueError:
        return False, []
    if not isinstance(data, list):
        return False, []
    ids = sorted({str(m.get("id")) for m in data
                  if isinstance(m, dict) and m.get("id")})
    return True, ids


def probe(cfg: ModelConfig) -> tuple[bool, str]:
    """测试**这个模型**能不能用，返回 (是否可用, 给人看的原因)。

    两个阶段，缺一不可：

    1. `GET /models`：确认端点可达、密钥有效。**但它只能证明"钥匙能进大门"**，
       证明不了"这把钥匙有这台房间的权限" —— 本项目的实际接入上就踩过：
       该端点对一个 Key 能列出 178 个模型，而每个模型调起来都是
       `403 AccessDenied.Unpurchased`。只做这一步会给出假阳性的"连接正常"。
    2. 极小对话（max_tokens=8）：唯一能证明"该模型真的可调用"的检查。
       代价是几个 token，换来的是"测试连接"按钮不说谎。

    403 在两种阶段的含义不同，所以分开报：
    第 1 阶段 403 = 密钥无效/无权限；第 2 阶段 403 = 模型未开通/未授权。
    """
    if not cfg.ready:
        return False, "配置不完整：需要 api_base、api_key、model 三项"
    ok, msg = probe_endpoint(cfg)
    if not ok:
        return False, msg
    try:
        chat(cfg, system="你是连通性探针。", user="回复 OK 两个字。",
             json_mode=False)
        return True, f"连接正常（模型 {cfg.model} 已通过最小对话验证）"
    except LLMError as e:
        if e.status == 403:
            return False, (f"模型 {cfg.model} 未授权或未开通（端点与密钥本身正常）；"
                           "请在模型服务控制台为该模型开通额度")
        if e.status == 404:
            return False, f"模型 {cfg.model} 不存在，请核对模型 ID"
        return False, str(e)


def _parse_json_loose(content: str) -> Dict[str, Any]:
    """容忍 ```json 包裹与前后废话，取出第一个 JSON 对象。"""
    text = (content or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


__all__ = ["ModelConfig", "LLMError", "resolve_model", "resolve_fallback",
           "chat", "chat_json", "chat_text", "probe", "probe_endpoint"]
