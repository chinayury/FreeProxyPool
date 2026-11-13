# -*- coding: utf-8 -*-
"""FreeProxyPool 网关包。

- :mod:`gateway.api`       公共代理分发端点 + 运维端点
- :mod:`gateway.admin_api` 管理后台 JSON API
- :mod:`gateway.deps`      鉴权 / 限流 / CSRF / 分发策略

FastAPI 应用实例为 ``gateway.main:app``。
"""

__all__ = ["api", "admin_api", "deps"]