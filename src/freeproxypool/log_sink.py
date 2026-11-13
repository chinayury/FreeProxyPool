# -*- coding: utf-8 -*-
"""日志汇聚常量。

真正的日志写入逻辑在 :mod:`core.logging`（RedisLogHandler + 后台 flush 线程），
本模块只保留 Key 定义与容量上限，供调度器裁剪与管理后台读取共用，
避免"Key 字符串在多处硬编码"改一处漏一处。
"""
from .config_center import KEY_LOGS as LOG_KEY

#: Redis 中保留的最近日志条数
MAX_LOGS = 2000

__all__ = ["LOG_KEY", "MAX_LOGS"]