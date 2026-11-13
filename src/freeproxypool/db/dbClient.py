# -*- coding: utf-8 -*-
"""存储层入口：按配置创建 Redis 客户端并缓存。

v0.1.0 变更：旧版 :class:`db.dbClient.DbClient` 用 metaclass 单例 +
**类属性**保存连接参数（``cls.db_type`` / ``cls.db_host`` ...），
导致同进程内任何一次构造都会污染全局状态，且无法测试。
这里改为普通对象 + 模块级缓存，参数保存在实例上。
"""
import threading

from .. import config_center as cc
from .redisClient import RedisStore

_lock = threading.RLock()
_store = None


def get_store(force_reload: bool = False) -> RedisStore:
    """获取全局唯一的 :class:`RedisStore` 实例。"""
    global _store
    with _lock:
        if _store is None or force_reload:
            table = cc.get("TABLE_NAME", "use_proxy")
            _store = RedisStore(cc.get_redis(), table=table)
        return _store


def reset_store() -> None:
    """清空缓存（测试 / 配置重载用）。"""
    global _store
    with _lock:
        _store = None


__all__ = ["get_store", "reset_store", "RedisStore"]