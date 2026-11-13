# -*- coding: utf-8 -*-
"""输入校验（全项目唯一入口）。

设计原则：
1. **一切外部输入先过这里**，校验失败抛 :class:`core.errors.ValidationError`，
   由全局异常处理器统一转成 400，不再让 ``int('abc')`` 这类 ValueError
   冒泡成 500（旧版 ``/admin/api/blacklist/add`` 的 ttl 参数就是这个问题）。
2. 校验函数是纯函数，不依赖配置中心，便于单元测试。
"""
import re
from typing import Optional

from .errors import ValidationError

#: 地区参数：ISO 3166-1 alpha-2 两位国家码（CN / US / HK ...）
_AREA_RE = re.compile(r"^[A-Za-z]{2}$")

#: 采集源 URL：仅允许 http/https，禁止 file:// gopher:// 等可打穿本机的协议
_FETCH_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

#: 通用 http(s) URL
_HTTP_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

#: 告警 Webhook：允许 http/https
_WEBHOOK_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

#: 分发策略白名单
STRATEGIES = ("latency", "random", "roundrobin", "weighted")

#: 匿名度筛选白名单
ANON_TYPES = ("elite", "anonymous", "transparent")

#: 延迟分层筛选白名单（与 helper.proxy.TIERS 一致）
#: unknown = 尚无延迟样本（从未通过校验）
TIER_NAMES = ("fast", "medium", "slow", "unknown")

#: 传输协议白名单（与 helper.proxy.PROTOCOLS 一致）
#:
#: 注意与 ``type=https`` 的区别：``type`` 表达的是"能否用于 HTTPS 目标"
#: （历史语义 = 支持 CONNECT 隧道），而 ``protocol`` 表达的是**传输协议**
#: （http / socks4 / socks5）。两者是正交维度，不能混用。
PROTOCOL_NAMES = ("http", "socks4", "socks5")

#: 输出格式白名单（借鉴 docs/09 P2-3）
#:
#: - ``json``：默认，返回代理对象 / 信封结构（兼容历史客户端）；
#: - ``text``：纯文本，一行一个 ``ip:port``，方便 ``curl | xargs``。
FORMATS = ("json", "text")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def valid_area(area: Optional[str]) -> bool:
    """地区参数是否合法：``None``（未传）或 2 位纯字母。"""
    return area is None or bool(_AREA_RE.match(area))


def parse_area(raw: Optional[str]) -> Optional[str]:
    """解析 area 查询参数。

    v0.1.0 行为变更：旧版对非法 area **静默忽略**（等价于不过滤），
    导致用户以为在按地区筛选、实际拿到全池代理。本次改为
    **显式拒绝 400**，让调用方立刻知道自己传错了。
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    _require(bool(_AREA_RE.match(value)),
             "area 仅支持 2 位字母的国家/地区代码，如 CN、US、HK")
    return value.upper()


def parse_format(raw: Optional[str]) -> str:
    """解析输出格式参数（json / text）。

    v0.1.0 新增（借鉴 docs/09 P2-3）：与 :func:`parse_area` / :func:`parse_tier`
    同样的口径 —— 非法值**显式拒绝 400**，不静默忽略。
    静默忽略等于"用户以为要了纯文本、实际拿到 JSON"，
    调用方按行解析时会直接崩。

    纯文本模式**只输出代理地址**（一行一个 ``ip:port``），
    不带 tier / protocol 等标签 —— 要完整字段请用 JSON 模式。
    """
    if raw is None:
        return "json"
    value = raw.strip().lower()
    if not value:
        return "json"
    _require(value in FORMATS,
             "format 仅支持 %s" % "、".join(FORMATS))
    return value


def parse_type(raw: Optional[str]) -> bool:
    """解析 type 查询参数，返回是否要求 HTTPS。

    白名单语义保持不变：仅 ``https`` 视为 HTTPS，其余按 HTTP 处理，
    以兼容历史客户端（携带非法值时不会被拒绝）。
    """
    return (raw or "").strip().lower() == "https"


def parse_strategy(raw: Optional[str], default: str = "latency") -> str:
    """解析 strategy 查询参数，非法值回落默认值。"""
    value = (raw or default).strip().lower()
    return value if value in STRATEGIES else default


def parse_anon(raw: Optional[str]) -> Optional[str]:
    """解析匿名度筛选参数（elite / anonymous / transparent）。

    v0.1.0 行为变更：与 :func:`parse_area` / :func:`parse_tier` /
    :func:`parse_protocol` / :func:`parse_format` **同一口径** ——
    非法值显式拒绝 400，不再静默回落成"不过滤"。

    为什么这一类必须严格、而 :func:`parse_type` / :func:`parse_strategy`
    可以宽松：

    * **筛选类**（area / tier / protocol / anon / format）决定"要不要筛"，
      静默忽略等于"调用方以为在筛、实际拿到全池代理" —— 这正是 B-01
      修掉的那类 bug；
    * **取值类**（type / strategy）决定"按哪种方式取"，不改变"筛不筛"，
      且历史客户端可能携带脏值，回落语义不会把"筛了"变成"没筛"。

    `anon` 属筛选类：传错一个字母就拿到全池（含透明代理），对调用方
    是实打实的结果失真。
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    _require(value in ANON_TYPES,
             "anon 仅支持 %s" % "、".join(ANON_TYPES))
    return value


def parse_tier(raw: Optional[str]) -> Optional[str]:
    """解析延迟分层筛选参数（fast / medium / slow / unknown）。

    v0.1.0 新增（借鉴 docs/09 P0-3）：与 :func:`parse_area` 同样的口径 ——
    非法值**显式拒绝 400**，而不是静默忽略。
    静默忽略等于"用户以为按延迟筛了、实际拿到全池代理"，
    与旧版 area 的 bug 是同一类问题。
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    _require(value in TIER_NAMES,
             "tier 仅支持 %s" % "、".join(TIER_NAMES))
    return value


def parse_protocol(raw: Optional[str]) -> Optional[str]:
    """解析传输协议筛选参数（http / socks4 / socks5）。

    v0.1.0 新增（借鉴 docs/09 P0-1）：与 :func:`parse_tier` 同样的口径 ——
    非法值**显式拒绝 400**，不静默忽略。

    与 ``type=https`` 的区别见 :data:`PROTOCOL_NAMES` 的注释：
    ``protocol`` 管传输协议，``type`` 管"能否用于 HTTPS 目标"。
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    _require(value in PROTOCOL_NAMES,
             "protocol 仅支持 %s" % "、".join(PROTOCOL_NAMES))
    return value


def parse_bool_param(raw: Optional[str], default: bool = False) -> bool:
    """解析形如 ``1/0``、``true/false`` 的开关型查询参数。"""
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def safe_int(raw, default: int, *, low: Optional[int] = None,
             high: Optional[int] = None, name: str = "参数") -> int:
    """安全地把查询参数转为 int 并 clamp 到 ``[low, high]``。

    非法输入（``None`` / ``'abc'`` / 空串）一律回落 ``default``，绝不抛异常。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if low is not None:
        value = max(value, low)
    if high is not None:
        value = min(value, high)
    return value


def parse_page(raw, *, default: int = 1, min_page: int = 1,
               max_page: int = 100000) -> int:
    """解析分页页码。"""
    return safe_int(raw, default, low=min_page, high=max_page, name="page")


def parse_size(raw, *, default: int = 50, min_size: int = 1,
               max_size: int = 500) -> int:
    """解析分页每页条数。"""
    return safe_int(raw, default, low=min_size, high=max_size, name="size")


def parse_ntl(raw, *, default: int = 10, max_n: int = 100) -> int:
    """解析批量取号数量 ``n``。"""
    value = safe_int(raw, default, low=1, high=max_n, name="n")
    return value


def parse_ttl(raw, default: int, *, low: int = 60, high: int = 86400 * 7) -> int:
    """解析黑名单 TTL。

    与 :func:`safe_int` 不同：这里对**非数字**输入直接拒绝而不是回落默认值。
    原因是这是请求体里的用户显式输入，静默替换成默认值会让用户
    以为自己设了 1 小时、实际是 1 小时（碰巧相同）或 7 天（不同时），
    属于危险的静默行为。越界则裁剪，不报错。
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("ttl 必须是整数")
    return max(low, min(value, high))


def valid_proxy(raw) -> bool:
    """代理地址 ``ip:port`` 合法性（内部复用 :mod:`core.iputil`）。"""
    from .iputil import is_valid_proxy
    return isinstance(raw, str) and is_valid_proxy(raw)


def require_proxy(raw, name: str = "proxy") -> str:
    """校验并返回归一化后的 ``ip:port``，非法抛 400。"""
    from .iputil import strip_proxy_prefix
    _require(isinstance(raw, str) and raw.strip(), "%s 不能为空" % name)
    value = raw.strip()
    _require(valid_proxy(value), "%s 格式非法，应为 ip:port" % name)
    return strip_proxy_prefix(value)


def _reject_internal_host(url: str) -> None:
    """拒绝指向内网 / 回环 / 链路本地 / 元数据端点的采集 URL。

    v0.1.0 安全加固：仅做协议白名单是不够的 —— ``http://169.254.169.254/
    latest/meta-data/`` 同样满足 http:// 白名单，却是云厂商的实例元数据端点。
    采集器以容器内身份发起请求，命中即等于把云凭证交出去。
    这里再叠一层目标主机校验。
    """
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if not host:
        raise ValidationError("url 缺少主机名：%s" % url)

    lowered = host.strip().lower()
    if lowered in ("localhost", "localhost.localdomain", "ip6-localhost"):
        raise ValidationError("url 不允许指向本机：%s" % host)

    import ipaddress
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return  # 域名交给 DNS，不在静态校验范围内（采集的是公开站点）

    from .iputil import is_internal_or_reserved
    if is_internal_or_reserved(lowered):
        raise ValidationError("url 不允许指向内网或保留地址：%s" % host)
    del address


def validate_fetch_urls(urls, *, max_count: int = 100, max_length: int = 2000) -> list:
    """校验采集源 URL 覆盖列表。

    v0.1.0 安全修复：旧版 :func:`config_center.set_fetcher_urls` 直接把
    管理端传入的任意字符串写进 Redis，采集器随后用 requests 发起请求，
    构成**服务端请求伪造(SSRF)** 面（可探测 ``http://169.254.169.254/``
    之类内网元数据端点）。这里同时做协议白名单与目标主机校验。

    - ``urls`` 为空表示"取消覆盖"，返回 ``[]``。
    - 超过 ``max_count`` 条直接拒绝，避免单个源配置撑爆内存。
    """
    if urls is None:
        return []
    if isinstance(urls, str):
        urls = [line.strip() for line in urls.splitlines()]
    if not isinstance(urls, (list, tuple)):
        raise ValidationError("urls 必须是字符串数组")

    cleaned = []
    for item in urls:
        if not isinstance(item, str):
            raise ValidationError("urls 每一项必须是字符串")
        value = item.strip()
        if not value:
            continue
        _require(len(value) <= max_length, "url 过长（上限 %d 字符）" % max_length)
        _require(bool(_FETCH_URL_RE.match(value)), "url 仅支持 http/https 协议")
        _require(bool(_HTTP_URL_RE.match(value)), "url 格式非法：%s" % value)
        _reject_internal_host(value)
        cleaned.append(value)

    _require(len(cleaned) <= max_count, "url 数量超出上限 %d" % max_count)
    return cleaned


def validate_webhook(raw) -> str:
    """校验告警 Webhook URL，空串表示关闭推送。

    v0.1.0 正式版补充（渗透测试发现）：Webhook 是由**服务端进程主动发起**的
    请求（`util/alert._push_webhook` → `urllib.request.urlopen`），因此
    "仅校验 http/https 协议"不够 —— 配成 `http://169.254.169.254/...` 或
    `http://127.0.0.1:6379/` 就构成一个**以管理员权限触发的 SSRF**，
    可用来探测/攻击内网服务与云元数据端点。这里与采集源 URL 同等对待，
    叠加目标主机黑名单。
    """
    if raw is None:
        return ""
    value = str(raw).strip()
    if not value:
        return ""
    _require(bool(_WEBHOOK_RE.match(value)), "Webhook 仅支持 http/https URL")
    _reject_internal_host(value)
    return value


def validate_check_url(raw, name: str) -> str:
    """校验校验目标 URL（HTTP_URL / HTTPS_URL）。

    v0.1.0 正式版补充（渗透测试发现）：校验目标同样不应指向内网。
    虽然请求是经候选代理发出的（不直接从本机出），但配成内网地址等于
    给"能操纵代理的人"提供一条到达内网的路径，且与采集源 URL 的校验
    口径不一致。这里统一加上目标主机黑名单。
    """
    value = (raw or "").strip()
    _require(bool(_HTTP_URL_RE.match(value)), "%s 必须是合法的 http/https URL" % name)
    _reject_internal_host(value)
    return value


def validate_name(raw, *, name: str = "name", max_length: int = 128) -> str:
    """校验标识符类字符串（采集源名等），只允许安全字符。"""
    value = (raw or "").strip() if isinstance(raw, str) else ""
    _require(bool(value), "%s 不能为空" % name)
    _require(len(value) <= max_length, "%s 过长" % name)
    _require(bool(re.match(r"^[A-Za-z0-9_\-.]+$", value)),
             "%s 仅允许字母、数字、下划线、中划线和点" % name)
    return value


def validate_cidr_list(raw) -> list:
    """校验可信代理网段列表（CIDR）。"""
    import ipaddress
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("TRUSTED_PROXIES 必须是列表或逗号分隔字符串")

    networks = []
    for item in raw:
        value = str(item).strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            raise ValidationError("TRUSTED_PROXIES 含非法网段：%s" % value)
    return networks


__all__ = [
    "STRATEGIES", "ANON_TYPES", "TIER_NAMES", "PROTOCOL_NAMES", "ValidationError",
    "valid_area", "parse_area", "parse_type", "parse_strategy", "parse_bool_param",
    "parse_anon", "parse_tier", "parse_protocol",
    "safe_int", "parse_page", "parse_size", "parse_ntl", "parse_ttl",
    "valid_proxy", "require_proxy",
    "validate_fetch_urls", "validate_webhook", "validate_check_url",
    "validate_name", "validate_cidr_list",
]