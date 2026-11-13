# -*- coding: utf-8 -*-
"""代理地域与匿名度解析。

## 为什么需要这个模块

旧版 ``PROXY_REGION`` 是一个**完全空转的配置项**：声明了、在管理后台
能配、``?area=CN`` 接口也照收，但全链路**没有任何一处给
``proxy.region`` 赋值**。结果是：

* 池内每条代理的 ``region`` 恒为空串；
* 存储层过滤条件 ``if area and data.get("region") and ...`` 因中间项为空
  而恒为假 —— 于是 ``?area=CN`` 实际返回的是**全池代理**；
* 管理后台"地域分布"面板永远是空的。

本模块补齐这条链路：用免费 GeoIP 接口把 IP 解析成两位国家码，
结果写入 Redis 缓存（默认 7 天），校验通过时随代理元数据一起落库。

## 设计约束

* **绝不阻塞主流程**：解析失败只是地域为空，不影响代理入池；
* **并发受限**：免费接口有速率限制，用信号量 + 单进程内并发上限兜底；
* **带缓存**：同一个 IP 只查一次，避免重复消耗配额。
"""
import asyncio
import atexit
import os
import time
from typing import Dict, Optional

from .. import config_center as cc
from ..core.logging import get_logger

log = get_logger("region", to_file=False)

#: 地域缓存 Key 前缀
CACHE_PREFIX = "fpp:region:"

#: 免费 GeoIP 接口（无需 API Key，45 次/分钟/IP）
IP_API = "http://ip-api.com/json/%s?fields=status,countryCode"
#: 备用接口（https，字段命名不同）
IP_API_BACKUP = "https://ipapi.co/%s/json/"

#: 已知的"无法解析"结果同样缓存，避免每次都去查注定失败的地址
UNKNOWN = ""

# B-06: 尝试导入 maxminddb；未安装时优雅降级到 HTTP
try:
    import maxminddb as _maxminddb  # noqa: F401
    _HAS_MMDB = True
except Exception:
    _HAS_MMDB = False


def cache_key(ip: str) -> str:
    return CACHE_PREFIX + ip


def lookup_cached(ip: str) -> Optional[str]:
    """读取缓存的地域；未缓存返回 ``None``。"""
    try:
        raw = cc.get_redis().get(cache_key(ip))
    except Exception:
        return None
    return None if raw is None else str(raw)


def store_cached(ip: str, region: str, ttl: int = 604800) -> None:
    try:
        cc.get_redis().set(cache_key(ip), region or UNKNOWN, ex=max(60, ttl))
    except Exception:
        pass


async def _fetch_region(ip: str, client, timeout: int = 8) -> str:
    """解析单个 IP 的国家码（两位大写字母）。失败返回空串。"""
    for url in (IP_API % ip, IP_API_BACKUP % ip):
        try:
            response = await client.get(url, timeout=timeout,
                                        allow_redirects=True)
        except Exception:
            continue
        if response.status != 200:
            continue
        try:
            payload = await response.json(content_type=None)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        code = payload.get("countryCode") or payload.get("country_code") or ""
        code = str(code).strip().upper()
        if len(code) == 2 and code.isalpha():
            return code
    return UNKNOWN


class MmdbProvider:
    """基于 MaxMind mmdb 的地域解析器（v0.1.0 B-06）。

    支持 GeoLite2-Country.mmdb；文件不存在或解析失败时返回空串，
    上层会回落 HTTP 接口。
    """

    def __init__(self, db_path: str = ""):
        self._db_path = ""
        self._reader = None
        self.set_path(db_path)

    def set_path(self, db_path: str) -> None:
        """设置 mmdb 路径；路径变化时关闭旧 reader。

        ``GEOIP_DB`` 可以在管理后台热更新，若只在 ``__init__`` 读一次，
        改完配置后不重启进程就永远不生效，且没有任何报错。
        """
        path = (db_path or "").strip()
        if path == self._db_path:
            return
        self.close()
        self._db_path = path

    def _ensure_reader(self):
        if self._reader is not None:
            return self._reader
        if not self._db_path or not os.path.isfile(self._db_path):
            return None
        try:
            import maxminddb
            self._reader = maxminddb.open_database(self._db_path)
            return self._reader
        except Exception:
            return None

    def lookup(self, ip: str) -> str:
        reader = self._ensure_reader()
        if reader is None:
            return UNKNOWN
        try:
            data = reader.get(ip)
        except Exception:
            return UNKNOWN
        if not isinstance(data, dict):
            return UNKNOWN
        # GeoLite2-Country: {"country": {"iso_code": "CN"}, ...}
        country = data.get("country") or {}
        code = country.get("iso_code") or ""
        code = str(code).strip().upper()
        return code if len(code) == 2 and code.isalpha() else UNKNOWN

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
            self._reader = None


class RegionResolver:
    """异步地域解析器（单例，随验证器进程生命周期复用）。"""

    def __init__(self):
        self._session = None
        self._mmdb = MmdbProvider(cc.get("GEOIP_DB", ""))

    async def _ensure_session(self, limit: int):
        if self._session is None or self._session.closed:
            import aiohttp
            connector = aiohttp.TCPConnector(limit=limit, ssl=False)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def resolve_many(self, ips) -> Dict[str, str]:
        """批量解析，返回 ``{ip: 两位国家码}``（查不到的 IP 不会出现在结果里）。"""
        unique = []
        seen = set()
        for ip in ips:
            if ip and ip not in seen:
                seen.add(ip)
                unique.append(ip)
        if not unique:
            return {}

        ttl = int(cc.get("REGION_CACHE_TTL", 604800))
        pending = []
        resolved: Dict[str, str] = {}
        for ip in unique:
            cached = lookup_cached(ip)
            if cached is None:
                pending.append(ip)
            else:
                resolved[ip] = cached

        if not pending:
            return resolved

        # B-06: mmdb 启用时优先走本地库（无需网络、无并发压力）。
        # GEOIP_DB 可能被管理后台热更新，先同步一次路径再查库。
        if _HAS_MMDB and bool(cc.get("GEOIP_ENABLED", True)):
            self._mmdb.set_path(cc.get("GEOIP_DB", ""))
            for ip in list(pending):
                region = self._mmdb.lookup(ip)
                if region:
                    store_cached(ip, region, ttl)
                    resolved[ip] = region
                    pending.remove(ip)
            if not pending:
                return resolved

        concurrency = max(1, int(cc.get("REGION_CONCURRENCY", 10)))
        semaphore = asyncio.Semaphore(concurrency)
        session = await self._ensure_session(concurrency)

        async def _one(ip):
            async with semaphore:
                # 双检：并发等待期间可能已被别的协程写入缓存
                cached = lookup_cached(ip)
                if cached is not None:
                    return ip, cached
                region = await _fetch_region(ip, session)
                store_cached(ip, region, ttl)
                return ip, region

        try:
            results = await asyncio.gather(
                *[_one(ip) for ip in pending], return_exceptions=True)
        finally:
            pass  # session 留给下次复用，避免每次批次都握手

        for item in results:
            if isinstance(item, BaseException):
                continue
            ip, region = item
            if region:
                resolved[ip] = region
        return resolved

    async def close(self):
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None


_resolver: Optional[RegionResolver] = None


def _shutdown() -> None:
    """进程退出时关闭地域解析器的 HTTP 会话。

    不注册的话每次进程结束都会打出 ``Unclosed client session`` 告警，
    在长时间反复重启的容器编排里会持续刷屏，掩盖真正的告警
    （:mod:`helper.check` 已为此注册 atexit，这里保持一致）。
    """
    resolver = _resolver
    if resolver is None or resolver._session is None:
        return
    try:
        from .check import get_loop
        loop = get_loop()
        if not loop.is_closed():
            loop.run_until_complete(resolver.close())
    except Exception:
        pass


atexit.register(_shutdown)


def get_resolver() -> RegionResolver:
    global _resolver
    if _resolver is None:
        _resolver = RegionResolver()
    return _resolver


def judge_anonymous(proxy_ip: str, origin_ip: str,
                   headers: Optional[dict] = None) -> str:
    """匿名度判定（v0.1.0 三段式）。

    - 出口 IP == 代理 IP，且响应头中无 Via / X-Forwarded-For → ``elite``
    - 出口 IP != 代理 IP → ``transparent``（向目标站暴露了客户端 IP）
    - 出口 IP == 代理 IP，但响应头中出现 Via / X-Forwarded-For → ``anonymous``
      （隐藏了真实 IP，但暴露了"用了代理"）
    - 无法取得出口 IP → 空串（未知）

    :param headers: 校验探针的响应头字典；为 None 时退化为旧的两态判定。
    """
    if not proxy_ip or not origin_ip:
        return ""
    proxy_ip = proxy_ip.strip()
    origin_ip = origin_ip.strip()
    if origin_ip != proxy_ip:
        return "transparent"
    # 出口等于代理，再按响应头区分 elite / anonymous
    if headers:
        lower = {k.lower(): v for k, v in headers.items()}
        if ("via" in lower) or ("x-forwarded-for" in lower):
            return "anonymous"
    return "elite"


def parse_origin_ip(text: str) -> str:
    """从校验响应正文中解析出口 IP。"""
    import json
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    origin = payload.get("origin") or payload.get("ip") or ""
    origin = str(origin).strip()
    # 多出口场景取第一个
    return origin.split(",")[0].strip()


__all__ = [
    "MmdbProvider", "RegionResolver", "get_resolver", "lookup_cached", "store_cached",
    "cache_key", "judge_anonymous", "parse_origin_ip", "CACHE_PREFIX",
]