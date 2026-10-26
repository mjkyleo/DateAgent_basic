"""极简 TTL 缓存（进程内，单实例 MVP 够用）。

用途：元数据（库/表清单）短时间复用。
一轮问答里 parse_intent → generate_sql 会连着问两次"这个库有哪些表"，
每次都是一次 information_schema 全表扫描——表多的时候这两次是白白付的。

刻意不做 LRU/持久化：MVP 单进程，量级就是几十个 key，过期即弃。
多实例部署时改用 Redis，接口保持 get/set 即可替换。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

_lock = threading.Lock()
_store: Dict[str, Tuple[float, Any]] = {}


def cache_get(key: str, ttl_s: float) -> Tuple[bool, Any]:
    """取缓存。返回 (命中?, 值)；未命中/已过期返回 (False, None)。"""
    if ttl_s <= 0:
        return False, None
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is None:
            return False, None
        expires, value = hit
        if expires < now:
            _store.pop(key, None)
            return False, None
    return True, value


def cache_set(key: str, value: Any, ttl_s: float) -> None:
    if ttl_s <= 0:
        return
    with _lock:
        _store[key] = (time.monotonic() + ttl_s, value)


def cache_invalidate(prefix: str = "") -> int:
    """按前缀清缓存（数据源增删改后调用）。返回清除条数。"""
    with _lock:
        if not prefix:
            n = len(_store)
            _store.clear()
            return n
        keys = [k for k in _store if k.startswith(prefix)]
        for k in keys:
            _store.pop(k, None)
        return len(keys)


def cached(key: str, ttl_s: float, producer, default: Optional[Any] = None) -> Any:
    """取缓存，未命中则调 producer 写入。producer 抛异常时返回 default 且不缓存。"""
    ok, value = cache_get(key, ttl_s)
    if ok:
        return value
    try:
        value = producer()
    except Exception:  # noqa: BLE001  缓存层不该改变调用方的失败语义
        return default
    cache_set(key, value, ttl_s)
    return value


__all__ = ["cache_get", "cache_set", "cache_invalidate", "cached"]
