# -*- coding: utf-8 -*-
"""FreeProxyPool 核心基础层。

本包只放"所有模块都会用到、且不依赖任何业务模块"的公共能力：

- :mod:`core.version`     版本单一来源
- :mod:`core.errors`      统一异常类型
- :mod:`core.logging`     日志器工厂（实例缓存 + 单一 Redis 汇聚 + 凭据脱敏）
- :mod:`core.validators`  输入校验（代理地址 / 地区 / 分页 / 策略）
- :mod:`core.iputil`      IP 解析与内网判定（全局唯一实现）
- :mod:`core.security`    密码哈希、令牌、常量时间比较
- :mod:`core.redact`      敏感信息脱敏

设计约束：
1. 本包禁止 import 任何业务模块（fetcher / gateway / db / handler ...），
   否则会形成循环导入。
2. 对外只暴露 :func:`get_logger`，业务代码不要再直接 new LogHandler。
"""

__all__ = ["version", "errors", "logging", "validators", "iputil", "security", "redact"]