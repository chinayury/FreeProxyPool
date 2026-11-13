# -*- coding: utf-8 -*-
"""快代理（kuaidaili.com）免费代理列表。

服务端渲染的 HTML 表格，分四类：优质密集(fps)、高匿开放(dps)、
普通开放(inha)、普通高匿(intr)。**分页从 1 开始且首页是 index.html**，
这里统一从第 2 页开始抓取（首页已由上一次抓取覆盖，重复抓收益低）。
"""
from ..baseFetcher import BaseFetcher


class KuaidailiFetcher(BaseFetcher):
    name = "kuaidaili"
    url = "https://www.kuaidaili.com/free/"
    is_html = True
    urls = [
        "https://www.kuaidaili.com/free/dps/%d" % page for page in range(2, 5)
    ] + [
        "https://www.kuaidaili.com/free/inha/%d" % page for page in range(2, 4)
    ]