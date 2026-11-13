# -*- coding: utf-8 -*-
"""采集源集合。

约定：
- 每个源是一个 :class:`fetcher.baseFetcher.BaseFetcher` 子类；
- ``name`` 全局唯一，是管理后台与配置中心的操作句柄；
- 站点已下线 / 改版 / 需要 Cookie 的源 **保留但默认禁用**，
  并在 ``status_note`` 里写明现状，避免后来者重复踩坑；
- 需要在抓取前做认证或渲染的源不要放进来 —— 本采集器只做无状态 HTTP GET。
"""