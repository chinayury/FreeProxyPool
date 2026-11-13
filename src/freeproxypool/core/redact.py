# -*- coding: utf-8 -*-
"""敏感信息脱敏。

统一在此实现，避免"日志处理器里一份、工具函数里又一份"导致漏改。
"""
import re
from urllib.parse import urlparse, urlunparse

#: 形如 scheme://user:pass@host:port/path 的 URL 片段（不含空白与引号）
URL_LIKE_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>]+")

#: Redis 连接串里常见的 "redis://:password@host" 形态
_REDIS_USERINFO_RE = re.compile(r"(redis(?:s)?://)([^:@/\s]*)(:[^@/\s]*)?@", re.IGNORECASE)

MASK = "***"


def mask_conn(conn: str) -> str:
    """脱敏 URL 中的密码部分。

    - 含密码：``redis://user:***@host:6379/0``
    - 不含密码：原样返回（调用方据此判断"无需脱敏"）
    - 无法解析：返回 ``***``（保守处理，宁可多遮也不泄露）
    """
    if not conn:
        return conn
    try:
        parsed = urlparse(conn)
        if parsed.password:
            host = parsed.hostname or ""
            if parsed.port:
                host = "%s:%d" % (host, parsed.port)
            netloc = "%s:%s@%s" % (parsed.username or "", MASK, host)
            return urlunparse((parsed.scheme, netloc, parsed.path,
                               parsed.params, parsed.query, parsed.fragment))
    except (ValueError, AttributeError):
        return MASK
    return conn


def mask_url_passwords(text) -> str:
    """把文本中所有"带密码的 URL"片段脱敏，其余内容原样保留。

    判定方式：:func:`mask_conn` 对**不含密码**的 URL 原样返回，
    对含密码的 URL 返回脱敏后的串，因此这里直接采用其返回值即可。
    （早期文档曾写"返回 None 表示无需脱敏"，与实现不符，已更正。）
    """
    if not isinstance(text, str) or "://" not in text:
        return text

    def _sub(match):
        url = match.group(0)
        masked = mask_conn(url)
        return url if masked is None else masked

    return URL_LIKE_RE.sub(_sub, text)


def mask_redis_inline(text: str) -> str:
    """兜底脱敏：直接处理正文里出现的 ``redis://:pwd@host`` 字面量。

    URL_LIKE_RE 已经能覆盖绝大多数情况，这里针对正则未命中的畸形写法
    （例如被日志格式化拆散）做一次更宽松的替换。
    """
    if not isinstance(text, str) or "redis://" not in text.lower() and "rediss://" not in text.lower():
        return text
    return _REDIS_USERINFO_RE.sub(lambda m: "%s%s%s@%s" % (m.group(1), m.group(2), MASK, ""), text)


def redact(text) -> str:
    """对外暴露的唯一脱敏入口：URL 脱敏 + 兜底替换。"""
    if not isinstance(text, str) or not text:
        return text
    return mask_redis_inline(mask_url_passwords(text))


__all__ = ["mask_conn", "mask_url_passwords", "mask_redis_inline", "redact", "URL_LIKE_RE", "MASK"]