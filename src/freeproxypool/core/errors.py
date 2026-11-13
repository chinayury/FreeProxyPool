# -*- coding: utf-8 -*-
"""统一异常类型。

全局异常处理器（:mod:`gateway.main`）按此分类映射 HTTP 状态码，
保证对外只暴露稳定文案，不泄露 traceback / 内部路径 / 驱动异常原文。
"""


class FreeProxyPoolError(Exception):
    """项目内所有主动抛出异常的基类。

    子类通过 :attr:`status_code` 与 :attr:`public_message` 声明
    "该错误应该以什么 HTTP 状态码、什么文案返回给调用方"。
    """

    status_code = 500
    public_message = "服务器内部错误"
    #: 写日志时使用；默认复用公开文案
    log_message = None

    def __init__(self, message=None, *, detail=None):
        self.detail = detail
        super().__init__(message or self.public_message)

    @property
    def log_text(self) -> str:
        """日志用文案：优先调用方传入的 message，其次 detail，最后公开文案。"""
        parts = [str(self)]
        if self.detail:
            parts.append(" | %s" % self.detail)
        return "".join(parts)


class ConfigError(FreeProxyPoolError):
    """配置项非法（类型不匹配、越界、取值不在白名单）。"""

    status_code = 400
    public_message = "配置项非法"


class ValidationError(FreeProxyPoolError):
    """外部输入非法（代理地址、地区、分页参数等）。"""

    status_code = 400
    public_message = "输入参数非法"


class AuthError(FreeProxyPoolError):
    """鉴权失败：API Key 不匹配、会话不存在或已过期。"""

    status_code = 401
    public_message = "未登录或会话已过期"


class CsrfError(FreeProxyPoolError):
    """CSRF 校验失败。"""

    status_code = 403
    public_message = "CSRF 校验失败"


class RateLimitError(FreeProxyPoolError):
    """触发限流。"""

    status_code = 429
    public_message = "请求过于频繁，已限流"


class BackendUnavailable(FreeProxyPoolError):
    """依赖不可用且**不允许**降级放行的场景。

    这是 v0.1.0 新增的关键语义：涉及鉴权/安全判定的读配置，
    一旦 Redis 不可用必须"失败关闭"(fail-closed)，
    而不是回落到默认值把鉴权直接放开。
    """

    status_code = 503
    public_message = "服务暂不可用，请稍后重试"


class NotFoundError(FreeProxyPoolError):
    """资源不存在。"""

    status_code = 404
    public_message = "资源不存在"


__all__ = [
    "FreeProxyPoolError",
    "ConfigError",
    "ValidationError",
    "AuthError",
    "CsrfError",
    "RateLimitError",
    "BackendUnavailable",
    "NotFoundError",
]