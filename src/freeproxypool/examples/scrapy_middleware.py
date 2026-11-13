# -*- coding: utf-8 -*-
"""Scrapy 下载中间件示例（借鉴 docs/09 P1-4）。

**这不是本项目的运行时依赖** —— Scrapy 不在 ``requirements.txt`` 里，
本文件只是给使用方一个可直接抄走的范例。

它解决的是 Scrapy 使用者最常踩的两个坑：

1. **代理失效没人管** —— Scrapy 拿到一条坏代理会一直重试，直到被
   Scrapy 自己的重试次数耗尽，池子里的坏代理却一直还在。
   这里在 ``process_exception`` / ``process_response`` 里**自动回报**，
   服务端立即把它淘汰并拉黑。
2. **每个请求都去取代理** —— 中间件把代理缓存在 spider 上，按需补充，
   不做"每个请求都打一次池子"的浪费。

用法（``settings.py``）::

    DOWNLOADER_MIDDLEWERS = {
        "examples.scrapy_middleware.ProxyPoolMiddleware": 500,
    }
    PROXY_POOL_URL = "http://127.0.0.1:5020"
    PROXY_POOL_API_KEY = "你的密钥"
    PROXY_POOL_FILTERS = {"area": "CN", "type": "https"}
    PROXY_POOL_MAX_PER_SPIDER = 100   # 每个 spider 最多缓存多少条代理

**合规边界**：中间件只做"从池子取代理 + 给 Scrapy 用 + 失效回报"，
**不生成任何本地正向代理配置** —— 那会把系统变成流量中转代理，
直接违反本项目合规声明。
"""
import logging

logger = logging.getLogger(__name__)


class ProxyPoolMiddleware:
    """从 FreeProxyPool 取代理、用完自动回报失效的 Scrapy 中间件。

    Scrapy 的 ``meta["proxy"]`` 形如 ``http://1.2.3.4:8080``，
    与 SDK 内部使用的代理 URL 格式一致。
    """

    def __init__(self, proxy_pool_url: str = None, api_key: str = None,
                 filters: dict = None, max_per_spider: int = 100):
        # Scrapy 通过 ``from_crawler`` 注入 settings；直接实例化时用默认值。
        self.proxy_pool_url = (proxy_pool_url
                               or "http://127.0.0.1:5020").rstrip("/")
        self.api_key = api_key
        self.filters = dict(filters or {})
        self.max_per_spider = max(1, int(max_per_spider))
        self._pool = None

    # ---------- Scrapy 生命周期 ----------

    @classmethod
    def from_crawler(cls, crawler):
        settings = crawler.settings
        middleware = cls(
            proxy_pool_url=settings.get("PROXY_POOL_URL"),
            api_key=settings.get("PROXY_POOL_API_KEY"),
            filters=settings.get("PROXY_POOL_FILTERS") or {},
            max_per_spider=settings.get("PROXY_POOL_MAX_PER_SPIDER", 100),
        )
        crawler.signals.connect(middleware.spider_opened, signal="spider_opened")
        crawler.signals.connect(middleware.spider_closed, signal="spider_closed")
        return middleware

    def spider_opened(self, spider) -> None:
        spider.proxy_pool = []
        spider.proxy_pool_used = set()

    def spider_closed(self, spider) -> None:
        pool = getattr(self, "_pool", None)
        if pool is not None:
            pool.close()
            self._pool = None

    # ---------- 中间件钩子 ----------

    def process_request(self, request, spider):
        """给请求挂一条代理；池子里没有可用代理就放行（不拦请求）。"""
        proxy = self._next_proxy(spider)
        if not proxy:
            return None
        request.meta["proxy"] = proxy
        return None

    def process_response(self, request, response, spider):
        """代理侧报错（407 / 502 / 503 / 504）时回报并清掉这条代理。

        目标站自己的 5xx **不回报** —— 那是目标站的问题，
        回报它等于把好代理误伤。
        """
        if response.status_code in (407, 502, 503, 504):
            self._report(request, spider)
        return response

    def process_exception(self, request, exception, spider):
        """连接异常时回报这条代理 —— Scrapy 侧的超时 / 连接失败
        基本都意味着这条代理已经废了。"""
        self._report(request, spider)
        return None

    # ---------- 内部 ----------

    def _client(self):
        """惰性创建 SDK 客户端（避免在中间件构造期就发网络请求）。"""
        if self._pool is None:
            from ..sdk import ProxyPoolClient
            self._pool = ProxyPoolClient(self.proxy_pool_url,
                                         api_key=self.api_key)
        return self._pool

    def _next_proxy(self, spider):
        """取一条代理：优先用 spider 上缓存的，用完再补。"""
        cached = getattr(spider, "proxy_pool", None)
        if cached is None:
            cached = []
            spider.proxy_pool = cached
        used = getattr(spider, "proxy_pool_used", None)
        if used is None:
            used = set()
            spider.proxy_pool_used = used

        while cached:
            proxy = cached.pop(0)
            if proxy in used:
                continue
            used.add(proxy)
            return proxy
        if len(used) >= self.max_per_spider:
            logger.warning("代理缓存已达上限 %d，本 spider 不再补充",
                           self.max_per_spider)
            return None
        try:
            item = self._client().get_proxy(**self.filters)
        except Exception as exc:
            logger.warning("取代理失败：%s", exc)
            return None
        if not item:
            return None
        url = item.get("proxy")
        if not url:
            return None
        scheme = str(item.get("protocol") or "http").lower()
        if scheme == "http":
            scheme = "http"
        return "%s://%s" % (scheme, url)

    def _report(self, request, spider) -> None:
        """回报当前请求用的那条代理（回报失败不影响抓取）。"""
        proxy = request.meta.get("proxy")
        if not proxy:
            return
        # "http://1.2.3.4:8080" → "1.2.3.4:8080"
        address = proxy.split("://", 1)[-1]
        try:
            self._client().report([address])
        except Exception as exc:
            logger.warning("回报失效代理 %s 失败：%s", address, exc)
        cached = getattr(spider, "proxy_pool", None)
        if cached and proxy in cached:
            cached.remove(proxy)
