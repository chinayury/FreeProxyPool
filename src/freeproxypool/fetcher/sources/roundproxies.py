# -*- coding: utf-8 -*-
"""RoundProxies 免费代理列表（服务端渲染 HTML 表格，约 8000+ 条）。"""
from ..baseFetcher import BaseFetcher


class RoundProxiesFetcher(BaseFetcher):
    name = "roundproxies"
    url = "https://roundproxies.com/free-proxy-list/"
    is_html = True
    urls = ["https://roundproxies.com/free-proxy-list/"]