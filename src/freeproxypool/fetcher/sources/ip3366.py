# -*- coding: utf-8 -*-
"""云代理（ip3366.net）免费列表。

分为国内内网(stype=1)与国内普通(stype=2)两类。
本项目默认启用 ``FILTER_INTERNAL`` 过滤内网地址，
因此内网分类仍会被逐条丢弃 —— 保留分类是为了在关闭内网过滤时可切换取数。
"""
from ..baseFetcher import BaseFetcher


class IP3366Fetcher(BaseFetcher):
    name = "ip3366"
    url = "http://www.ip3366.net/free/"
    is_html = True
    urls = [
        "http://www.ip3366.net/free/?stype=1&page=%d" % page for page in range(1, 4)
    ] + [
        "http://www.ip3366.net/free/?stype=2&page=%d" % page for page in range(1, 3)
    ]