"""通用工具函数与异常脱敏（设计文档 §5 utils/helpers.py、§10.4）。

脱敏是硬要求：返回前端的错误不得暴露数据库 IP、账号、连接串、底层调用栈。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

# 连接串 / 账号形态：user:password@host:port、mysql+pymysql://...
_DSN_RE = re.compile(r"[a-z+]+://[^\s]*@[^\s]*")
_IPPORT_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b")
# 驱动/框架异常类名：(pymysql.err.OperationalError)、(sqlalchemy.exc.SQLAlchemyError) ...
# 这类前缀属于技术栈信息，按 §10.4 不得外泄。
_EXCCLASS_RE = re.compile(
    r"\((?:pymysql|MySQLdb|mysql\.connector|sqlalchemy|sqlglot|openai|httpx|requests)"
    r"(?:\.[A-Za-z_][\w.]*)*\)")
# 账号形态的两种裸写法：DSN 正则只覆盖 user:pass@host，而驱动抛出的原文是
# "Access denied for user 'root'@'localhost' (using password: NO)" 和 "user=root"，
# 账号名同样属于本模块要抹掉的连接信息（踩过：降级提示里直接把 root 报给了前端）。
_DB_AUTH_RE = re.compile(
    r"(?:for user '[^']*'@'[^']*'|(?:user|password|passwd)\s*=\s*[^\s,;'\"）)]+)",
    re.IGNORECASE)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sanitize_error(msg: str, *, max_len: int = 200) -> str:
    """错误信息脱敏：去连接串/IP、截断，只留业务可读部分。"""
    s = str(msg or "")
    s = _DSN_RE.sub("[已脱敏连接串]", s)
    s = _IPPORT_RE.sub("[已脱敏地址]", s)
    s = _DB_AUTH_RE.sub("[已脱敏账号]", s)
    s = _EXCCLASS_RE.sub("", s)
    s = re.sub(r"\(\s*\)", "", s)          # 去掉类名被抹掉后残留的空括号
    s = " ".join(s.split())                # 压平多行（栈信息）
    return s[:max_len]


def is_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v)
            return True
        except ValueError:
            return False
    return False


def to_number(v: Any) -> float | None:
    if is_number(v):
        return float(v)
    return None


def is_time_like(v: Any) -> bool:
    """判断值是否为日期/时间形态（datetime 对象或常见日期字符串，支持到月）。"""
    if isinstance(v, datetime):
        return True
    if isinstance(v, str):
        return bool(re.match(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?", v.strip()))
    return False


def to_month(v: Any) -> str | None:
    """归一化到月份键 YYYY-MM（趋势计算用）；无法解析返回 None。"""
    if isinstance(v, datetime):
        return f"{v.year:04d}-{v.month:02d}"
    if isinstance(v, str):
        m = re.match(r"^(\d{4})[-/](\d{1,2})", v.strip())
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return None


def fmt_num(v: Any, digits: int = 2) -> str:
    """数字千分位格式化；非数字原样返回。"""
    n = to_number(v)
    if n is None:
        return str(v)
    if n == int(n) and abs(n) < 1e15:
        return f"{int(n):,}"
    return f"{n:,.{digits}f}"


def pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 2) if whole else 0.0


def uniq(seq: Iterable) -> list:
    return list(dict.fromkeys(seq))


__all__ = ["now_iso", "sanitize_error", "is_number", "to_number", "is_time_like",
           "to_month", "fmt_num", "pct", "uniq"]
