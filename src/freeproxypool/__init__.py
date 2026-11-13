# -*- coding: utf-8 -*-
"""FreeProxyPool —— 免费代理 IP 池系统。

自动采集公开免费代理 → 三层递进验证 → 标准化 API 分发。

包内分层：

- :mod:`freeproxypool.core`       核心基础（版本 / 异常 / 日志 / 校验 / 安全 / 脱敏）
- :mod:`freeproxypool.db`         存储层（Redis 客户端 / 队列 / MySQL 冷存储）
- :mod:`freeproxypool.fetcher`    采集层（声明式采集源 + 注册表）
- :mod:`freeproxypool.gateway`    API 网关（FastAPI：分发 / 管理后台 / 指标）
- :mod:`freeproxypool.helper`     校验与诊断（TCP/HTTP/HTTPS 探测 / 地域 / 声誉）
- :mod:`freeproxypool.services`   四个进程（collector / validator / scheduler）
- :mod:`freeproxypool.sdk`        客户端 SDK（同步 / 异步，不依赖项目内其他模块）
- :mod:`freeproxypool.storage`    MySQL 冷存储归档
- :mod:`freeproxypool.util`       告警 / HTTP 请求工具
- :mod:`freeproxypool.admin`      管理后台静态资源
- :mod:`freeproxypool.tools`      运维工具（CI 门禁 / 每日快照）
- :mod:`freeproxypool.examples`   集成示例（Scrapy 下载中间件）

命令行入口见 :mod:`freeproxypool.proxyPool`，也可用 ``python -m freeproxypool``。
"""
from .core.version import VERSION

__version__ = VERSION
__all__ = ["VERSION", "__version__"]
