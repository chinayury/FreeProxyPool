# -*- coding: utf-8 -*-
"""89 代理（89ip.cn）分页列表。服务端渲染 HTML 表格，每页约 40 条。"""
from ..baseFetcher import BaseFetcher


class IP89Fetcher(BaseFetcher):
    name = "ip89"
    url = "https://www.89ip.cn/"
    is_html = True
    urls = ["https://www.89ip.cn/index_%d.html" % page for page in range(1, 6)]