# -*- coding: utf-8 -*-
"""FreeProxyPool 自检套件。

全部用例在内存版 Redis 上运行，不依赖外部服务、不发网络请求。
"""

__all__ = ["fake_redis", "run_selftest"]