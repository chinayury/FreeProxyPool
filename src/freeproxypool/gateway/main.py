# -*- coding: utf-8 -*-
"""FastAPI 应用装配与启动入口。

v0.1.0 变更：**删除了整套 Flask 实现**（旧版 ``api/proxyApi.py``
与 ``admin/__init__.py``，合计约 770 行）。它们与 FastAPI 版功能一一对应，
但存在两套鉴权、两套限流、两套响应格式，并带来两类真实故障：

* 容器部署跑的是 FastAPI，``proxyPool.py server`` 起的却是 Flask ——
  同一份代码两种行为，排查问题时极具迷惑性；
* 安全修复必须改两遍，漏改一处就是漏洞。

现在只有一套实现、一套鉴权、一套响应格式。
"""
import uvicorn

from .. import config_center as cc
from ..core.logging import get_logger
from ..core.version import VERSION
from .admin_api import router as admin_router
from .api import app

log = get_logger("gateway.main", to_file=False)

app.include_router(admin_router)


def main() -> None:
    """以 uvicorn 启动服务。"""
    host = str(cc.get("HOST", "0.0.0.0"))
    port = int(cc.get("PORT", 5020))
    try:
        cc.get_redis().ping()
    except Exception:
        log.warning("Redis 当前不可达，服务将以 503 响应鉴权类请求（fail-closed）")
    log.info("FreeProxyPool %s 监听 %s:%s", VERSION, host, port)
    uvicorn.run(app, host=host, port=port, log_level="info",
                access_log=True, server_header=False)


if __name__ == "__main__":
    main()