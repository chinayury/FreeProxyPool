# -*- coding: utf-8 -*-
"""IP 解析与地址属性判定（全项目唯一实现）。

v0.1.0 修复点：旧版在 :mod:`helper.fetch`（:_is_internal）与
:mod:`helper.validator`（:isInternalIp）里存在**两份规则不一致**的
内网判定实现——前者漏判 100.64/10 与 0.0.0.0/8 之外的保留网段，
后者漏判协议前缀与 user:pass@ 前缀的清洗。现已合并到本模块。
"""
import ipaddress
import re
from typing import Optional, Tuple

#: ``ip:port``，允许 1~3 位端口（清洗阶段用较宽的网，最后再校验范围）
_HOST_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$")

#: 认证前缀 ``user:pass@`` 与协议前缀 ``http://`` 的统一清洗
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def strip_proxy_prefix(raw: str) -> str:
    """把 ``http://user:pass@1.2.3.4:8080`` 归一化为 ``1.2.3.4:8080``。

    无法归一化时返回去掉首尾空白后的原串，交由调用方判定合法性。
    """
    if not raw:
        return ""
    value = raw.strip().rstrip("/")
    value = _SCHEME_RE.sub("", value)
    if "@" in value:
        value = value.rsplit("@", 1)[-1]
    return value.strip()


def parse_ipv4(raw: str) -> Optional[str]:
    """解析并校验 IPv4 字面量，非法返回 ``None``。

    使用 :mod:`ipaddress` 做严格校验，会拒绝 ``999.999.999.999``、
    ``01.02.03.04`` 这类旧正则放行的畸形写法。
    """
    if not raw:
        return None
    try:
        addr = ipaddress.IPv4Address(raw.strip())
    except (ipaddress.AddressValueError, ValueError):
        return None
    return str(addr)


def parse_host_port(raw: str) -> Tuple[Optional[str], Optional[int]]:
    """解析 ``ip:port``（自动剥离协议与认证前缀）。

    返回 ``(ip, port)``；不合法时返回 ``(None, None)``。
    端口合法区间为 1~65535——**0 号端口不是可用服务端口**，故拒绝。
    """
    value = strip_proxy_prefix(raw)
    matched = _HOST_PORT_RE.match(value)
    if not matched:
        return None, None
    ip = parse_ipv4(matched.group(1))
    if ip is None:
        return None, None
    try:
        port = int(matched.group(2))
    except ValueError:
        return None, None
    if not 1 <= port <= 65535:
        return None, None
    return ip, port


def is_valid_proxy(raw: str) -> bool:
    """``ip:port`` 完整合法性判定（格式 + 网段 + 端口范围）。"""
    return parse_host_port(raw)[0] is not None


def is_internal_or_reserved(ip: str) -> bool:
    """判断 IPv4 是否为内网 / 保留 / 不可路由地址。

    覆盖范围（比旧实现更完整）：
      - ``0.0.0.0/8``        本网络
      - ``10.0.0.0/8``       私有
      - ``100.64.0.0/10``    运营商级 NAT（RFC 6598）
      - ``127.0.0.0/8``      回环
      - ``169.254.0.0/16``   链路本地
      - ``172.16.0.0/12``    私有
      - ``192.0.0.0/24``     IETF 协议保留
      - ``192.168.0.0/16``   私有
      - ``198.18.0.0/15``    基准测试（RFC 2544）
      - ``224.0.0.0/4``      组播
      - ``240.0.0.0/4``      保留 + 广播

    另外单列判定 ``255.255.255.255`` 广播地址。
    """
    normalized = parse_ipv4(ip)
    if normalized is None:
        # 非法字面量一律按"不可用"处理，避免脏数据进入校验队列
        return True

    addr = ipaddress.IPv4Address(normalized)
    if addr == ipaddress.IPv4Address("255.255.255.255"):
        return True
    if addr.is_private or addr.is_loopback or addr.is_link_local \
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True

    # ipaddress 的 is_private 已覆盖 10/8、172.16/12、192.168/16、
    # 127/8、169.254/16；但 100.64/10 与 198.18/15 需显式补充
    for cidr in _EXTRA_RESERVED:
        if addr in cidr:
            return True
    return False


def is_internal_proxy(raw: str) -> bool:
    """对 ``ip:port``（可带协议/认证前缀）做内网判定。

    解析失败时返回 ``True``（保守丢弃），与"脏数据不进入校验队列"的策略一致。
    """
    ip, _port = parse_host_port(raw)
    if ip is None:
        return True
    return is_internal_or_reserved(ip)


_EXTRA_RESERVED = (
    ipaddress.ip_network("100.64.0.0/10"),   # RFC 6598 运营商级 NAT
    ipaddress.ip_network("198.18.0.0/15"),   # RFC 2544 基准测试
    ipaddress.ip_network("192.0.0.0/24"),    # IETF 协议保留
)


__all__ = [
    "strip_proxy_prefix",
    "parse_ipv4",
    "parse_host_port",
    "is_valid_proxy",
    "is_internal_or_reserved",
    "is_internal_proxy",
]