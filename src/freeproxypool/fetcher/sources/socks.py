# -*- coding: utf-8 -*-
"""SOCKS4 / SOCKS5 代理采集源（借鉴 docs/09 P0-1）。

这一组源与 :mod:`fetcher.sources.github_raw` 里的 HTTP 源**格式完全相同**
（一行一个 ``ip:port``），唯一区别是它们是 **SOCKS 代理**，因此每个类
都要把 :attr:`fetcher.baseFetcher.BaseFetcher.protocol` 覆盖成
``socks5`` / ``socks4`` —— 采集时这个值会写进
:class:`helper.proxy.Proxy`，后续校验与分发据此选择对应的连接方式。

**列表格式说明**：这些源不需要 ``is_html``（正文就是纯文本），
默认正则 ``ip:port`` 提取即可；部分条目形如 ``user:pass@ip:port``，
正则只取后半段的 ``ip:port``（免费 SOCKS 列表基本不带认证，
即便带了也不能入库 —— 本项目不保存代理账号密码）。

**默认启用的取舍**：只启用"体量与现有 HTTP 源相当"的列表
（数千条量级）。体量明显异常的源一律留成**禁用**并写明原因，
否则单个超大会独占采集队列的上限，把其它源的条目挤出去。
"""
from ..baseFetcher import BaseFetcher


class TheSpeedXSocks5Fetcher(BaseFetcher):
    """TheSpeedX/PROXY-List — SOCKS5 列表。"""

    name = "thespeedx_socks5"
    url = "https://github.com/TheSpeedX/PROXY-List"
    urls = ["https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"]
    protocol = "socks5"


class TheSpeedXSocks4Fetcher(BaseFetcher):
    """TheSpeedX/PROXY-List — SOCKS4 列表。"""

    name = "thespeedx_socks4"
    url = "https://github.com/TheSpeedX/PROXY-List"
    urls = ["https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"]
    protocol = "socks4"


class MonosansSocks5Fetcher(BaseFetcher):
    """monosans/proxy-list — SOCKS5 子列表。"""

    name = "monosans_socks5"
    url = "https://github.com/monosans/proxy-list"
    urls = ["https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"]
    protocol = "socks5"


class MonosansSocks4Fetcher(BaseFetcher):
    """monosans/proxy-list — SOCKS4 子列表。"""

    name = "monosans_socks4"
    url = "https://github.com/monosans/proxy-list"
    urls = ["https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"]
    protocol = "socks4"


class JetkaiSocks5Fetcher(BaseFetcher):
    """jetkai/proxy-list — 仅收录"在线可用"的 SOCKS5。"""

    name = "jetkai_socks5"
    url = "https://github.com/jetkai/proxy-list"
    urls = [
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
    ]
    protocol = "socks5"


class JetkaiSocks4Fetcher(BaseFetcher):
    """jetkai/proxy-list — 仅收录"在线可用"的 SOCKS4。"""

    name = "jetkai_socks4"
    url = "https://github.com/jetkai/proxy-list"
    urls = [
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt",
    ]
    protocol = "socks4"


class HProxySocks5Fetcher(BaseFetcher):
    """hproxy 官方 API 的 SOCKS5 分区（一次可拉上千条）。"""

    name = "hproxy_socks5"
    url = "https://hproxy.com/free-proxy-list"
    urls = ["https://hproxy.com/api/proxy-list?format=txt&protocol=socks5&limit=10000"]
    protocol = "socks5"


class ProxyScrapeSocks5Fetcher(BaseFetcher):
    """ProxyScrape v4 API 的 SOCKS5 分区。"""

    name = "proxyscrape_socks5"
    url = "https://proxyscrape.com/free-proxy-list"
    urls = [
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies&protocol=socks5&proxy_format=ipport&format=text"
    ]
    protocol = "socks5"


class HookzofSocks5Fetcher(BaseFetcher):
    """hookzof/socks5_list —— 体量过大，默认禁用。

    保留定义是为了**留痕**：这个源是真实可用的，但它单文件就有 1.3 MB
    （约 8~9 万条），是现有最大 HTTP 源（TheSpeedX http.txt，约 2.5 千条）
    的 30 倍以上。单源就能塞满 ``QUEUE_MAX_LEN``（5 万）的采集队列，
    把其它源的条目挤掉，也会让校验器把算力花在一个源上。

    需要更多 SOCKS5 供给时，在管理后台单独启用它即可 ——
    队列上限与背压机制（``db/queue.py``）会兜住溢出。
    """

    name = "hookzof_socks5"
    url = "https://github.com/hookzof/socks5_list"
    urls = ["https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"]
    protocol = "socks5"
    enabled = False
    status_note = ("单文件 1.3MB（约 8~9 万条），单源即可塞满 5 万条采集队列，"
                   "会挤掉其它来源；默认禁用，需要更多 SOCKS5 供给时在后台单独启用。")
