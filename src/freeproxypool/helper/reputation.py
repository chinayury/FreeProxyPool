# -*- coding: utf-8 -*-
"""DNSBL / IP 声誉查询。

v0.1.0 B-08：判断代理 IP 是否已被列入公开垃圾邮件 / 滥用黑名单。
使用标准 DNS A 记录查询（反序八位组），无需外部 HTTP API。

设计约束：

* **绝不阻塞主流程**：DNS 查询失败或超时只保留 ``dnsbl=""``，不影响代理入池；
* **异步安全**：同步 DNS 查询放在 ``run_in_executor`` 里运行，不阻塞事件循环；
* **带缓存**：同一个 IP 只查一次 per TTL，避免重复消耗 DNS 配额；
* **零外部依赖**：只用标准库 ``socket`` + ``asyncio``，不引入 ``dnspython``。
"""
import asyncio
import socket
from typing import List, Optional

from .. import config_center as cc
from ..core.logging import get_logger

log = get_logger("reputation", to_file=False)

#: DNSBL 缓存 Key 前缀
_CACHE_PREFIX = "fpp:dnsbl:"

#: 默认 zone 列表
_DEFAULT_ZONES = ("zen.spamhaus.org", "bl.spamcop.net", "dnsbl.sorbs.net")

#: 认为"被列入"的响应地址段（DNSBL 返回 127.0.0.x）
_LISTED_PREFIX = "127.0.0."

#: DNS 查询超时（秒）
_DNS_TIMEOUT = 3


def _cache_key(ip: str, zone: str) -> str:
    return "%s%s:%s" % (_CACHE_PREFIX, ip, zone)


def lookup_cached(ip: str, zone: str) -> Optional[bool]:
    """读取缓存的 DNSBL 结果；未缓存返回 ``None``。"""
    try:
        raw = cc.get_redis().get(_cache_key(ip, zone))
    except Exception:
        return None
    if raw is None:
        return None
    if raw == "listed":
        return True
    if raw == "clean":
        return False
    return None


def store_cached(ip: str, zone: str, listed: bool, ttl: int = 3600) -> None:
    try:
        cc.get_redis().set(
            _cache_key(ip, zone),
            "listed" if listed else "clean",
            ex=max(60, ttl),
        )
    except Exception:
        pass


def _reverse_ip(ip: str) -> str:
    """把 IP 反序成 DNSBL 查询名。"""
    parts = ip.strip().split(".")
    if len(parts) != 4:
        return ""
    return ".".join(reversed(parts))


async def _dns_a_record(domain: str) -> List[str]:
    """异步查询 A 记录，返回 IP 地址列表。"""
    loop = asyncio.get_event_loop()
    try:
        results = await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, domain, None),
            timeout=_DNS_TIMEOUT,
        )
    except (asyncio.TimeoutError, socket.gaierror, OSError):
        return []
    except Exception:
        return []
    addresses = []
    for r in results:
        addr = r[4][0]
        if addr not in addresses:
            addresses.append(addr)
    return addresses


async def check_zone(ip: str, zone: str, ttl: int = 3600) -> bool:
    """检查单个 zone 是否把该 IP 列入黑名单。

    返回 ``True`` = 被列入，``False`` = 未列入或查询失败。

    > 注意：DNS 查询失败（超时 / 网络不通）与"确认未列入"（NXDOMAIN）
    > 在 ``getaddrinfo`` 层面都表现为拿不到地址，因此查询失败时按
    > **未列入**处理并同样缓存 —— 这意味着 zone 整体宕机会被误判为 clean。
    > 声誉过滤是"加分项"而非安全边界，该取舍可接受；对误判敏感的场景
    > 应缩短 ``DNSBL_CACHE_TTL`` 或改用返回码可区分的 DNS 库。
    """
    cached = lookup_cached(ip, zone)
    if cached is not None:
        return cached
    reversed_ip = _reverse_ip(ip)
    if not reversed_ip:
        return False
    query_name = "%s.%s" % (reversed_ip, zone)
    try:
        addresses = await _dns_a_record(query_name)
    except Exception:
        return False
    listed = any(a.startswith(_LISTED_PREFIX) for a in addresses)
    if listed or not addresses:
        # 空响应也缓存为 clean（避免下次再查）， listed 立即缓存
        store_cached(ip, zone, listed, ttl)
    return listed


async def check_dnsbl(ip: str, zones: Optional[List[str]] = None,
                      ttl: int = 3600) -> dict:
    """检查 IP 在多个 zone 的声誉状态。

    返回 ``{"ip": ip, "listed": bool, "zones": {zone: bool}}``。
    任何 zone 命中即 ``listed=True``。
    """
    if zones is None:
        raw = cc.get("DNSBL_ZONES", "")
        if isinstance(raw, str):
            zones = [z.strip() for z in raw.split(",") if z.strip()]
        elif isinstance(raw, (list, tuple)):
            zones = [str(z).strip() for z in raw if str(z).strip()]
        else:
            zones = list(_DEFAULT_ZONES)
    if not zones:
        zones = list(_DEFAULT_ZONES)
    result = {"ip": ip, "listed": False, "zones": {}}
    if not ip:
        return result
    tasks = [check_zone(ip, zone, ttl) for zone in zones]
    findings = await asyncio.gather(*tasks, return_exceptions=True)
    for zone, found in zip(zones, findings):
        if isinstance(found, BaseException):
            result["zones"][zone] = False
            continue
        result["zones"][zone] = found
        if found:
            result["listed"] = True
    return result


class ReputationChecker:
    """DNSBL 声誉检查器（单例，随验证器进程生命周期复用）。"""

    async def check_many(self, ips) -> dict:
        """批量检查，返回 ``{ip: check_dnsbl 结果}``。"""
        if not bool(cc.get("DNSBL_ENABLED", True)):
            return {}
        unique = []
        seen = set()
        for ip in ips:
            if ip and ip not in seen:
                seen.add(ip)
                unique.append(ip)
        if not unique:
            return {}
        results = {}
        raw = cc.get("DNSBL_ZONES", "")
        if isinstance(raw, str):
            zones = [z.strip() for z in raw.split(",") if z.strip()]
        elif isinstance(raw, (list, tuple)):
            zones = [str(z).strip() for z in raw if str(z).strip()]
        else:
            zones = list(_DEFAULT_ZONES)
        if not zones:
            zones = list(_DEFAULT_ZONES)
        # 缓存时长每次现读：旧实现在 __init__ 里读一次，后台改 DNSBL_CACHE_TTL
        # 后不重启进程不生效
        ttl = int(cc.get("DNSBL_CACHE_TTL", 3600))
        for ip in unique:
            results[ip] = await check_dnsbl(ip, zones, ttl)
        return results


_checker: Optional[ReputationChecker] = None


def get_checker() -> ReputationChecker:
    global _checker
    if _checker is None:
        _checker = ReputationChecker()
    return _checker


__all__ = [
    "ReputationChecker", "get_checker",
    "check_dnsbl", "check_zone", "lookup_cached", "store_cached",
]
