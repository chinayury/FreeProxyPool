# -*- coding: utf-8 -*-
"""FreeProxyPool 异步客户端（借鉴 docs/09 P1-4）。

与 :mod:`sdk.client` 同语义，只是走 ``aiohttp``。

**为什么值得单独写一个异步版**：``aiohttp`` 的 SOCKS 支持走
``aiohttp_socks.ProxyConnector``（连接器在构造时绑定代理地址），
而 ``requests`` 的 SOCKS 要额外装 PySocks。异步场景本来就是爬虫的主场，
SOCKS5 代理用起来更顺。

**合规边界**：同 :mod:`sdk.client` —— 只做取代理 / 经代理发请求 / 回报失效，
不提供任何"本地正向代理"能力。
"""
import asyncio

import aiohttp

from ._common import (DEFAULT_BACKOFF, DEFAULT_RETRIES, DEFAULT_TIMEOUT,
                         FILTER_KEYS, NoProxyAvailable, ProxyRequestError,
                         SdkError, build_params, describe_filters, proxy_url,
                         short_error)


class AsyncProxyPoolClient:
    """异步客户端。

    ::

        from . import AsyncProxyPoolClient

        async with AsyncProxyPoolClient(api_key="你的密钥") as pool:
            resp, proxy = await pool.fetch("https://example.com")
    """

    def __init__(self, base_url: str = "http://localhost:5020",
                 api_key: str = None, timeout: int = DEFAULT_TIMEOUT,
                 retries: int = DEFAULT_RETRIES,
                 backoff: float = DEFAULT_BACKOFF,
                 session: aiohttp.ClientSession = None):
        self.base_url = (base_url or "http://localhost:5020").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.backoff = max(0.0, float(backoff))
        self._session = session
        self._owns_session = session is None

    # ---------- 内部 ----------

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["API-KEY"] = self.api_key
        return headers

    def _url(self, path: str) -> str:
        return "%s/%s" % (self.base_url, str(path).lstrip("/"))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout))
            self._owns_session = True
        return self._session

    async def _request(self, method: str, path: str, params: dict = None,
                       json_body: dict = None):
        """发一个请求，**只对连接错误与 5xx 做指数退避重试**。"""
        url = self._url(path)
        session = await self._get_session()
        last = None
        for attempt in range(self.retries):
            try:
                async with session.request(method, url, params=params,
                                           json=json_body,
                                           headers=self._headers()) as resp:
                    if resp.status < 400:
                        return resp.status, await resp.read()
                    body = await resp.text()
                    if resp.status < 500:
                        raise SdkError("请求 %s 失败（HTTP %d）：%s"
                                       % (url, resp.status, short_error(body)))
                    last = SdkError("服务端返回 %d" % resp.status)
            except SdkError:
                raise
            except aiohttp.ClientError as exc:
                last = exc
            except asyncio.TimeoutError as exc:
                last = exc
            if attempt < self.retries - 1:
                await asyncio.sleep(self.backoff * (2 ** attempt))
        raise SdkError("请求 %s 失败：%s" % (url, last))

    async def _report_quiet(self, proxies) -> None:
        """回报失效代理；回报失败不影响主流程。"""
        items = [p for p in (proxies or []) if p]
        if not items:
            return
        try:
            await self._request("POST", "/report/",
                                json_body={"proxies": items})
        except SdkError:
            pass

    # ---------- 取代理 ----------

    async def get_proxy(self, **filters) -> dict:
        """取一个代理；池空或无匹配时返回 ``None``。"""
        import json as _json
        status, body = await self._request("GET", "/get/",
                                            params=build_params(filters))
        try:
            data = _json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("proxy"):
            return None
        return data

    async def batch(self, n: int = 10, **filters) -> list:
        """批量取代理。"""
        import json as _json
        status, body = await self._request(
            "GET", "/batch/", params=build_params(filters, {"n": n}))
        try:
            data = _json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        if not isinstance(data, dict):
            return []
        return list(data.get("data") or [])

    async def all(self, **filters) -> list:
        """取代理列表。"""
        import json as _json
        status, body = await self._request("GET", "/all/",
                                            params=build_params(filters))
        try:
            data = _json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        return list(data) if isinstance(data, list) else []

    async def count(self) -> dict:
        """取池统计。"""
        import json as _json
        status, body = await self._request("GET", "/count/")
        try:
            data = _json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- 回报 ----------

    async def report(self, proxies) -> bool:
        """回报失效代理（服务端立即淘汰并拉黑）。失败抛 :class:`SdkError`。"""
        items = [p for p in (proxies or []) if p]
        if not items:
            return False
        await self._request("POST", "/report/", json_body={"proxies": items})
        return True

    # ---------- 经代理发请求 ----------

    async def fetch(self, url: str, method: str = "GET", headers: dict = None,
                    data=None, timeout: int = None, max_attempts: int = 3,
                    **filters):
        """经代理抓取 ``url``；代理不可用时**自动回报**并换一条重试。

        返回 ``(status, body, proxy)``（异步版返回原始字节，不给
        ``aiohttp`` 的响应对象 —— 它离开 ``async with`` 就失效了）。

        判定"这条代理不行"的口径：连接层异常或 **407**。
        目标站 5xx 是目标站的问题，不回报。
        """
        attempts = max(1, int(max_attempts))
        last = None
        for _ in range(attempts):
            proxy = await self.get_proxy(**filters)
            if not proxy:
                raise NoProxyAvailable("代理池为空，或没有匹配 %s 的代理"
                                       % describe_filters(filters))
            try:
                status, body = await self._fetch_once(
                    method, url, proxy, headers=headers, data=data,
                    timeout=timeout)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = exc
                await self._report_quiet([proxy.get("proxy")])
                continue
            if status == 407:
                last = SdkError("代理返回 407（代理认证被拒）")
                await self._report_quiet([proxy.get("proxy")])
                continue
            return status, body, proxy
        raise ProxyRequestError("经代理请求 %s 失败（已尝试 %d 条代理）：%s"
                                % (url, attempts, last))

    async def _fetch_once(self, method: str, url: str, proxy: dict,
                          headers: dict = None, data=None, timeout: int = None):
        """经一条代理发一次请求。SOCKS 走专用连接器（构造时绑定地址）。"""
        scheme = proxy_url(proxy)
        total = timeout or self.timeout
        if scheme.startswith("socks"):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError as exc:
                raise SdkError(
                    "缺少 aiohttp-socks 依赖，无法使用 SOCKS 代理：%s" % exc)
            connector = ProxyConnector.from_url(scheme, limit=4, ssl=False)
            session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=total))
            try:
                async with session.request(method, url, headers=headers,
                                           data=data) as resp:
                    return resp.status, await resp.read()
            finally:
                await session.close()
        session = await self._get_session()
        async with session.request(method, url, headers=headers, data=data,
                                   proxy=scheme,
                                   timeout=aiohttp.ClientTimeout(total=total)
                                   ) as resp:
            return resp.status, await resp.read()

    # ---------- 生命周期 ----------

    async def close(self) -> None:
        """关闭底层会话。"""
        if self._session is not None and not self._session.closed \
                and self._owns_session:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
        return False


__all__ = ["AsyncProxyPoolClient", "SdkError", "NoProxyAvailable",
           "ProxyRequestError", "FILTER_KEYS"]
