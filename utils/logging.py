"""统一日志（basic.utils.logging）。

MVP 之前"静默降级、静默兜底"让线上问题无法追溯（例如 LLM 调用失败自动走规则兜底，
用户只会看到一条规则 SQL，却不知道 LLM 挂了）。这里补一层极简日志：
- 进程内只配置一次（幂等），避免重复 handler 刷屏；
- 日志级别走 LOG_LEVEL 环境变量，默认 INFO；
- 调用方只拿 logger，**绝不**把连接串/密码/查询结果写进日志。
"""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"


def setup_logging(level: str | None = None, *, force: bool = False) -> None:
    """配置根 logger。重复调用是安全的（除非 force=True）。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    lvl = (level or os.environ.get("LOG_LEVEL", "INFO")).strip().upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, lvl, logging.INFO))
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
        root.addHandler(h)
    # 第三方库太吵：只留 WARNING 以上
    for noisy in ("sqlalchemy", "sqlalchemy.engine", "sqlglot", "urllib3",
                  "httpx", "openai", "pymysql"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """取 logger（首次调用时顺带完成配置）。"""
    setup_logging()
    return logging.getLogger(name)


__all__ = ["get_logger", "setup_logging"]
