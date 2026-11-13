# -*- coding: utf-8 -*-
"""SDK 内部共用的小工具（同步 / 异步客户端都要用）。

**刻意不 import 项目里的任何模块** —— SDK 是给**外部调用方**用的，
他们只会拿走 ``sdk/`` 这一个目录，不会连同整个项目一起拷。
因此这里需要的少量逻辑就地实现（协议归一化、参数拼装等）。
"""


class SdkError(Exception):
    """SDK 通用异常（请求失败、响应非法、用法错误……）。"""


class NoProxyAvailable(SdkError):
    """代理池为空，或没有匹配筛选条件的代理。"""


class ProxyRequestError(SdkError):
    """经代理发起的业务请求最终失败（已尝试多条代理）。"""


#: 允许透传给服务端的筛选参数（与网关的查询参数一一对应）
FILTER_KEYS = ("type", "area", "strategy", "anon", "exclude_dnsbl",
               "tier", "protocol", "format")

#: 默认超时（秒）
DEFAULT_TIMEOUT = 10

#: 默认重试次数（仅对连接错误与 5xx 生效）
DEFAULT_RETRIES = 3

#: 默认退避基数（秒），实际等待 = backoff × 2^(尝试次数-1)
DEFAULT_BACKOFF = 0.5


def normalize_protocol(value) -> str:
    """归一化传输协议（空值 / 未知值按 ``http``）。

    与服务端 :func:`helper.proxy.normalize_protocol` 同口径 ——
    历史数据没有这个字段，缺省必须是 HTTP。
    """
    text = str(value or "").strip().lower()
    return text if text in ("http", "socks4", "socks5") else "http"


def build_params(filters: dict, extra: dict = None) -> dict:
    """把筛选条件整理成查询参数。

    * 只透传 :data:`FILTER_KEYS` 里认识的键 —— 传错 key 立刻报错，
      比悄悄发一个服务端不认的参数好；
    * ``None`` / 空串视为"不筛选"，不带进 query；
    * ``True`` / ``False`` 统一转成 ``1`` / ``0``（服务端认这两种）。
    """
    params = {}
    for key, value in dict(filters or {}).items():
        if key not in FILTER_KEYS:
            raise SdkError("不支持的筛选参数：%s（可用：%s）"
                           % (key, "、".join(FILTER_KEYS)))
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            params[key] = "1" if value else "0"
        else:
            params[key] = value
    for key, value in dict(extra or {}).items():
        if value is not None and value != "":
            params[key] = value
    return params


def describe_filters(filters: dict) -> str:
    """把筛选条件描述成一句话（异常信息里用）。"""
    if not filters:
        return "无筛选条件"
    return "、".join("%s=%s" % (k, v) for k, v in sorted(filters.items())
                     if v is not None and v != "")


def proxy_address(proxy: dict) -> str:
    """取代理地址（``ip:port``）。"""
    if isinstance(proxy, dict):
        return str(proxy.get("proxy") or "")
    return str(proxy or "")


def proxy_scheme(proxy: dict) -> str:
    """取代理的传输协议。"""
    return normalize_protocol(proxy.get("protocol") if isinstance(proxy, dict)
                              else None)


def proxy_url(proxy: dict) -> str:
    """把代理对象拼成 URL（``http://1.2.3.4:8080`` / ``socks5://...``）。"""
    scheme = proxy_scheme(proxy)
    addr = proxy_address(proxy)
    if not addr:
        raise SdkError("代理对象缺少 proxy 字段")
    return "%s://%s" % (scheme, addr)


def short_error(text, limit: int = 200) -> str:
    """截短服务端返回体（错误信息里别塞一整页 HTML）。"""
    value = str(text or "").strip()
    return value[:limit] + ("…" if len(value) > limit else "")
