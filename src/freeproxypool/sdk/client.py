# -*- coding: utf-8 -*-
"""FreeProxyPool 同步客户端（借鉴 docs/09 P1-4）。

为什么要有 SDK：每个调用方都要自己写一遍
"带 Key 请求 → 失败重试 → 失效回报 → 退避"。
这段逻辑重复实现且容易漏 —— 尤其**失效回报**：不回报，脏代理就一直留在
池子里被分发出去，只能等下一轮复检才发现它坏了。

同步版基于 ``requests``（项目已有依赖，不新增）。

**合规边界**：SDK 只做三件事 —— 取代理、经代理发请求、回报失效代理。
**不提供**生成本地正向代理配置（squid）之类的能力，那会把系统变成
流量中转代理，直接违反本项目合规声明。
"""
import time

import requests

from ._common import (DEFAULT_BACKOFF, DEFAULT_RETRIES, DEFAULT_TIMEOUT,
                         FILTER_KEYS, NoProxyAvailable, ProxyRequestError,
                         SdkError, build_params, describe_filters, proxy_url,
                         short_error)


class ProxyPoolClient:
    """同步客户端。

    ::

        from . import ProxyPoolClient

        pool = ProxyPoolClient("http://127.0.0.1:5020", api_key="你的密钥")
        resp, proxy = pool.fetch("https://example.com")
        print(resp.status_code, proxy["proxy"])
    """

    def __init__(self, base_url: str = "http://localhost:5020",
                 api_key: str = None, timeout: int = DEFAULT_TIMEOUT,
                 retries: int = DEFAULT_RETRIES,
                 backoff: float = DEFAULT_BACKOFF,
                 session: requests.Session = None):
        self.base_url = (base_url or "http://localhost:5020").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.backoff = max(0.0, float(backoff))
        self.session = session or requests.Session()

    # ---------- 内部 ----------

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["API-KEY"] = self.api_key
        return headers

    def _url(self, path: str) -> str:
        return "%s/%s" % (self.base_url, str(path).lstrip("/"))

    def _request(self, method: str, path: str, params: dict = None,
                 json_body: dict = None) -> requests.Response:
        """发一个请求，**只对连接错误与 5xx 做指数退避重试**。

        4xx 直接抛 —— 参数写错时重试多少次都是一样的错，
        重试只会把错误放大成"看起来像是服务端挂了"。
        """
        url = self._url(path)
        last = None
        for attempt in range(self.retries):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body,
                    headers=self._headers(), timeout=self.timeout)
            except requests.RequestException as exc:
                last = exc
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code < 500:
                    raise SdkError("请求 %s 失败（HTTP %d）：%s"
                                   % (url, resp.status_code,
                                      short_error(resp.text)))
                last = SdkError("服务端返回 %d" % resp.status_code)
            if attempt < self.retries - 1:
                time.sleep(self.backoff * (2 ** attempt))
        raise SdkError("请求 %s 失败：%s" % (url, last))

    def _report_quiet(self, proxies) -> None:
        """回报失效代理；**回报失败不影响主流程**。

        回报只是"让池子尽快剔掉坏代理"，回报不了也不该让业务请求失败。
        """
        items = [p for p in (proxies or []) if p]
        if not items:
            return
        try:
            self._request("POST", "/report/", json_body={"proxies": items})
        except SdkError:
            pass

    @staticmethod
    def _proxies(proxy: dict) -> dict:
        """把代理对象转成 ``requests`` 的 ``proxies`` 参数。"""
        scheme = proxy_url(proxy)
        if scheme.startswith("socks"):
            # requests 的 SOCKS 支持来自 PySocks（`requests[socks]` 附加包），
            # 没装就别让调用方拿到一个看不懂的连接错误。
            try:
                import socks  # noqa: F401
            except ImportError:
                raise SdkError(
                    "使用 SOCKS 代理需要 PySocks：pip install 'requests[socks]'"
                    "（或改用同步之外的异步客户端，它走 aiohttp-socks）")
        return {"http": scheme, "https": scheme}

    # ---------- 取代理 ----------

    def get_proxy(self, **filters) -> dict:
        """取一个代理；池空或无匹配时返回 ``None``。"""
        resp = self._request("GET", "/get/", params=build_params(filters))
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("proxy"):
            return None
        return data

    def batch(self, n: int = 10, **filters) -> list:
        """批量取代理（返回 list）。"""
        resp = self._request("GET", "/batch/",
                             params=build_params(filters, {"n": n}))
        try:
            data = resp.json()
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        return list(data.get("data") or [])

    def all(self, **filters) -> list:
        """取代理列表（返回 list）。"""
        resp = self._request("GET", "/all/", params=build_params(filters))
        try:
            data = resp.json()
        except ValueError:
            return []
        return list(data) if isinstance(data, list) else []

    def count(self) -> dict:
        """取池统计。"""
        resp = self._request("GET", "/count/")
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- 回报 ----------

    def report(self, proxies) -> bool:
        """回报失效代理（服务端立即淘汰并拉黑）。

        失败会抛 :class:`SdkError` —— 显式调用时用户应该知道回报没成功；
        自动回报走 :meth:`_report_quiet`（吞掉异常）。
        """
        items = [p for p in (proxies or []) if p]
        if not items:
            return False
        self._request("POST", "/report/", json_body={"proxies": items})
        return True

    # ---------- 经代理发请求 ----------

    def fetch(self, url: str, method: str = "GET", headers: dict = None,
              data=None, timeout: int = None, max_attempts: int = 3,
              **filters):
        """经代理抓取 ``url``；代理不可用时**自动回报**并换一条重试。

        返回 ``(response, proxy)``。

        判定"这条代理不行"的口径只有两类：**连接层异常**（超时 / 连接被拒
        / 读失败）与 **407 Proxy Authentication Required**。目标站返回 5xx
        是目标站的问题，回报它等于误伤好代理 —— 因此不回报。
        """
        attempts = max(1, int(max_attempts))
        last = None
        for _ in range(attempts):
            proxy = self.get_proxy(**filters)
            if not proxy:
                raise NoProxyAvailable("代理池为空，或没有匹配 %s 的代理"
                                       % describe_filters(filters))
            try:
                resp = self.session.request(
                    method, url, proxies=self._proxies(proxy),
                    headers=headers, data=data,
                    timeout=timeout or self.timeout)
            except requests.RequestException as exc:
                last = exc
                self._report_quiet([proxy.get("proxy")])
                continue
            if resp.status_code == 407:
                last = SdkError("代理返回 407（代理认证被拒）")
                self._report_quiet([proxy.get("proxy")])
                continue
            return resp, proxy
        raise ProxyRequestError("经代理请求 %s 失败（已尝试 %d 条代理）：%s"
                                % (url, attempts, last))

    def close(self) -> None:
        """关闭底层会话。"""
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


__all__ = ["ProxyPoolClient", "SdkError", "NoProxyAvailable",
           "ProxyRequestError", "FILTER_KEYS"]
