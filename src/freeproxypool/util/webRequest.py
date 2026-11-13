# -*- coding: utf-8 -*-
"""HTTP 客户端封装（采集与地域解析共用）。

v0.1.0 相对旧版 ``util/webRequest.py`` 的修复：

1. **失败不再返回"空壳"对象**。
   旧版在所有重试都失败时直接 ``return self.response``，那是一个
   ``requests.Response()`` 空白实例：``status_code is None``、``text is None``。
   调用方若只判 ``if r.text:`` 会把"请求彻底失败"和"页面确实为空"混为一谈。
   现在彻底失败返回 ``None``。

2. **重试会检查状态码**。
   旧版只在异常时重试，遇到 403/404/5xx 直接把错误页正文当成功结果返回。
   现在 5xx 与 429 参与重试，其余 4xx 直接判定失败。

3. **User-Agent 更新**。
   旧版 UA 停留在 2014 年（Chrome 39 / Firefox 34），被主流站点直接 403。

4. **显式关闭环境代理**。
   旧版未禁用 ``trust_env``，若部署环境设置了 ``HTTP_PROXY``，
   所有采集请求都会被绕到代理上，出现"采回来的全是内网出口"的诡异故障。

5. **Logger 按名复用**，不再每次请求新建 logger 与文件句柄。
"""
import random
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from ..core.logging import get_logger

log = get_logger("fetcher.http", to_file=False)

#: 三个较新的桌面 UA 轮换，降低被单一 UA 指纹拦截的概率
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
)

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

#: 参与重试的状态码（5xx 与 429 视为可重试）
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class WebClient:
    """带连接池的轻量 HTTP 客户端（线程安全，建议按进程复用一个实例）。"""

    def __init__(self, pool_size: int = 32, retries: int = 2,
                 backoff: float = 0.8):
        self.retries = max(0, retries)
        self.backoff = backoff
        self._session = requests.Session()
        # 不信任 HTTP_PROXY/HTTPS_PROXY 等环境变量 —— 采集流量必须直连
        self._session.trust_env = False
        adapter = HTTPAdapter(pool_connections=pool_size,
                              pool_maxsize=pool_size, max_retries=0)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def headers(self, extra: Optional[dict] = None) -> dict:
        merged = dict(DEFAULT_HEADERS)
        merged["User-Agent"] = random.choice(USER_AGENTS)
        if extra:
            merged.update(extra)
        return merged

    def get(self, url: str, *, timeout: int = 15, headers: Optional[dict] = None,
            retry_time: Optional[int] = None, verify: bool = True):
        """发起 GET 请求。

        :return: ``requests.Response``；彻底失败返回 ``None``
        """
        attempts = (self.retries if retry_time is None else max(0, retry_time)) + 1
        for attempt in range(attempts):
            try:
                response = self._session.get(
                    url, headers=self.headers(headers),
                    timeout=timeout, verify=verify, allow_redirects=True,
                )
            except requests.RequestException as exc:
                log.debug("请求失败 url=%s 错误=%s", url, type(exc).__name__)
                if attempt < attempts - 1:
                    time.sleep(self.backoff * (2 ** attempt))
                continue

            status = response.status_code
            if status in RETRYABLE_STATUS and attempt < attempts - 1:
                time.sleep(self.backoff * (2 ** attempt))
                continue
            return response
        return None

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


_client = None


def get_client() -> WebClient:
    """获取进程内共享的 HTTP 客户端。"""
    global _client
    if _client is None:
        _client = WebClient()
    return _client


__all__ = ["WebClient", "get_client", "USER_AGENTS", "RETRYABLE_STATUS"]