# -*- coding: utf-8 -*-
"""Geonode 公开代理 API。

返回 JSON，字段包含 ip / port / protocols / country / anonymityLevel，
是少数能直接给出**地域与匿名度**的免费源，可显著降低后续地域解析的补查量。
"""
import json

from ..baseFetcher import BaseFetcher


class GeonodeFetcher(BaseFetcher):
    name = "geonode"
    url = "https://proxylist.geonode.com/free-proxy-list"
    urls = [
        "https://proxylist.geonode.com/api/proxy-list"
        "?limit=500&page=1&sort_by=lastChecked&sort_type=desc",
        "https://proxylist.geonode.com/api/proxy-list"
        "?limit=500&page=2&sort_by=lastChecked&sort_type=desc",
    ]

    def parse(self, text):
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return
        for item in (data or {}).get("data") or []:
            ip = item.get("ip")
            port = item.get("port")
            if ip and port:
                yield "%s:%s" % (ip, port)