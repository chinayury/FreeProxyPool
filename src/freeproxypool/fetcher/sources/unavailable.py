# -*- coding: utf-8 -*-
"""已知不可用 / 需要额外前置条件的采集源（全部默认禁用）。

保留这些定义而不是直接删除，原因有二：

1. **留痕**。这些源都曾经工作过，写明"什么时候、因为什么失效"，
   比删掉之后过半年又有人重新加回来强。
2. **可恢复**。多数站点只是临时改版或封禁了裸 UA 抓取，
   站点恢复后把 ``enabled`` 改回 True（或在管理后台勾选）即可复用，
   不需要重新写解析逻辑。

所有源的 ``status_note`` 会展示在管理后台的采集源列表里。
"""
from ..baseFetcher import BaseFetcher


class Daili66Fetcher(BaseFetcher):
    """66 代理（66ip.cn）。"""

    name = "daili66"
    url = "http://www.66ip.cn/"
    enabled = False
    status_note = "服务器对裸 UA 抓取返回 403；免费列表页改版，原 mo.php 接口已 404。需补 Cookie + Referer 并适配新页面结构。"


class DocipFetcher(BaseFetcher):
    """多利代理（docip.com）。"""

    name = "docip"
    url = "http://www.docip.com/"
    enabled = False
    status_note = "域名已出售并重定向到 hugedomains.com，端点不可用。"


class FreeVPNNodeFetcher(BaseFetcher):
    """FreeVPNNode。"""

    name = "freevpnnode"
    url = "https://www.freevpnnode.com/free-proxy-list/"
    enabled = False
    status_note = "列表页重定向后无有效表格数据，代理数据依赖 JS 加载，requests 拿不到。"


class GoodipsFetcher(BaseFetcher):
    """ goodness 代理（goodips.cn）。"""

    name = "goodips"
    url = "http://www.goodips.cn/"
    enabled = False
    status_note = "域名 DNS 已无法解析，端点不可用。"


class IhuanFetcher(BaseFetcher):
    """小幻代理（ip.ihuan.me）。"""

    name = "ihuan"
    url = "https://ip.ihuan.me/"
    enabled = False
    is_html = True
    status_note = "免费代理数据已停更三年，几乎不可用。"


class KxdailiFetcher(BaseFetcher):
    """快代理（kxdaili.com）。"""

    name = "kxdaili"
    url = "http://www.kxdaili.com/dailiip.html"
    enabled = False
    is_html = True
    status_note = "站点可达但代理列数据约 800 天未更新；部署服务器出口 IP 曾被站点按 IP 封禁（连续 500）。"


class ProxiflyFetcher(BaseFetcher):
    """Proxifly。"""

    name = "proxifly"
    url = "https://proxifly.dev/free-proxy"
    enabled = False
    status_note = "正文为 JS 动态加载，requests 只能拿到空壳页面；已知 API 端点返回空响应。需无头浏览器或反向其内部 API。"