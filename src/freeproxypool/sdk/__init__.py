# -*- coding: utf-8 -*-
"""FreeProxyPool 客户端 SDK（借鉴 docs/09 P1-4）。

对外只暴露两个客户端：

* :class:`ProxyPoolClient` —— 同步（``requests``）；
* :class:`AsyncProxyPoolClient` —— 异步（``aiohttp`` + ``aiohttp-socks``）。

用法见各自的 docstring。SDK **不 import 项目内的任何模块**，
外部调用方只需要拷走 ``sdk/`` 这一个目录。
"""

from ._common import (FILTER_KEYS, NoProxyAvailable, ProxyRequestError,
                         SdkError)
from .async_client import AsyncProxyPoolClient
from .client import ProxyPoolClient

__all__ = ["ProxyPoolClient", "AsyncProxyPoolClient", "SdkError",
           "NoProxyAvailable", "ProxyRequestError", "FILTER_KEYS"]
