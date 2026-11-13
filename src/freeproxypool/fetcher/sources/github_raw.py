# -*- coding: utf-8 -*-
"""GitHub 持续维护的免费代理纯文本列表。

这一组是本项目**最稳定**的数据来源：仓库由社区机器人按小时更新，
内容就是一行一个 ``ip:port``，不依赖 HTML 解析、不限速、不需要认证。
"""
from ..baseFetcher import BaseFetcher


class TheSpeedXHttpFetcher(BaseFetcher):
    """TheSpeedX/PROXY-List — HTTP 列表，日更量级。"""

    name = "thespeedx_http"
    url = "https://github.com/TheSpeedX/PROXY-List"
    urls = ["https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"]


class TheSpeedXFetcher(BaseFetcher):
    """TheSpeedX/PROXY-List — HTTPS 列表（代理支持 CONNECT 隧道的部分）。"""

    name = "thespeedx"
    url = "https://github.com/TheSpeedX/PROXY-List"
    urls = ["https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/https.txt"]


class MonosansFetcher(BaseFetcher):
    """monosans/proxy-list — 附带匿名代理子列表。"""

    name = "monosans"
    url = "https://github.com/monosans/proxy-list"
    urls = [
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/http.txt",
    ]


class JetkaiFetcher(BaseFetcher):
    """jetkai/proxy-list — 仅收录"在线可用"的代理。"""

    name = "jetkai"
    url = "https://github.com/jetkai/proxy-list"
    urls = [
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
    ]


class MuRongPigFetcher(BaseFetcher):
    """MuRongPIG/Proxy-Master。"""

    name = "murongpig"
    url = "https://github.com/MuRongPIG/Proxy-Master"
    urls = ["https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt"]


class PrxchkFetcher(BaseFetcher):
    """prxchk/proxy-list — 带质量评分字段，取 ``proxies/http.txt``。"""

    name = "prxchk"
    url = "https://github.com/prxchk/proxy-list"
    urls = ["https://raw.githubusercontent.com/prxchk/proxy-list/main/proxies/http.txt"]


class ProxyListToFetcher(BaseFetcher):
    name = "proxylistto"
    url = "https://github.com/proxylist-to/proxy-list"
    urls = ["https://raw.githubusercontent.com/proxylist-to/proxy-list/main/http.txt"]


class RdavydovFetcher(BaseFetcher):
    name = "rdavydov"
    url = "https://github.com/rdavydov/proxy-list"
    urls = ["https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt"]


class Rx4096Fetcher(BaseFetcher):
    name = "rx4096"
    url = "https://github.com/RX4096/proxy-list"
    urls = [
        "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/http.txt",
        "https://raw.githubusercontent.com/RX4096/proxy-list/main/online/https.txt",
    ]


class IplocateFetcher(BaseFetcher):
    name = "iplocate"
    url = "https://github.com/iplocate/free-proxy-list"
    urls = ["https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt"]


class MertguvencliFetcher(BaseFetcher):
    name = "mertguvencli"
    url = "https://github.com/mertguvencli/http-proxy-list"
    urls = ["https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list/data.txt"]


class Proxy4parsingFetcher(BaseFetcher):
    name = "proxy4parsing"
    url = "https://github.com/proxy4parsing/proxy-list"
    urls = ["https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt"]


class HendrikbgrFetcher(BaseFetcher):
    name = "hendrikbgr"
    url = "https://github.com/hendrikbgr/Free-Proxy-Repo"
    urls = ["https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Repo/master/proxy_list.txt"]


class UptimerBotFetcher(BaseFetcher):
    name = "uptimerbot"
    url = "https://github.com/UptimerBot/proxy-list"
    urls = ["https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt"]


class ClarketmFetcher(BaseFetcher):
    name = "clarketm"
    url = "https://github.com/clarketm/proxy-list"
    urls = ["https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"]


class HProxyFetcher(BaseFetcher):
    """hproxy 官方 API，一次可拉上万条。"""

    name = "hproxy"
    url = "https://hproxy.com/free-proxy-list"
    urls = ["https://hproxy.com/api/proxy-list?format=txt&protocol=http&limit=10000"]


class ProxyScrapeFetcher(BaseFetcher):
    """ProxyScrape v4 API。"""

    name = "proxyscrape"
    url = "https://proxyscrape.com/free-proxy-list"
    urls = [
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies&protocol=http&proxy_format=ipport&format=text"
    ]