# -*- coding: utf-8 -*-
"""FreeProxyPool 动态配置中心。

## 优先级

    环境变量  >  Redis 配置 Hash（后台热更新）  >  setting.py 默认值

环境变量优先级最高，便于容器化部署时用 env 固化安全底线，
又允许运行期通过管理后台调整非安全类参数。

## v0.1.0 关键修复：鉴权配置的 fail-safe 语义

旧实现里 :func:`get` 捕获任何异常后**静默返回 schema 默认值**。
当 Redis 不可用时：

    API_KEY       -> 回落为空串 -> 鉴权直接关闭，所有 /get/ /all/ /delete/ 裸奔
    ADMIN_ENABLED -> 回落 True   -> 管理后台被意外开启
    RATE_LIMIT_QPS-> 回落默认    -> 限流口径突变

这是典型的 **fail-open**。v0.1.0 把配置读取拆成两种语义：

* :func:`get`            —— 普通参数，Redis 异常时回落到默认值（保持可用性）
* :func:`get_secure`     —— 安全相关参数，**读不到就报错**（fail-closed）

网关的 API Key 鉴权、管理后台开关、登录限流阈值一律走 :func:`get_secure`。
这样 Redis 抖动时，行为是"拒绝服务"而不是"放开大门"。
"""
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from . import setting
from redis import Redis
from redis.connection import BlockingConnectionPool
from .core.errors import BackendUnavailable, ConfigError
from .core.security import (constant_time_compare, derive_csrf, generate_token,
                          hash_password, is_hashed, verify_password_hash)

# ---------- Redis Key 命名（集中管理，避免散落字面量） ----------
KEY_CONFIG = "fpp:config"
KEY_FETCHERS = "fpp:fetchers"
KEY_TRIGGER = "fpp:trigger"
KEY_STAT = "fpp:stat"
KEY_LOGS = "fpp:logs"
KEY_SESSIONS = "fpp:admin:sessions"
KEY_LOGIN_FAIL = "fpp:admin:loginfail:%s"
KEY_CSRF_SECRET = "fpp:admin:csrf_secret"
KEY_BREAKER = "fpp:breaker"

CACHE_TTL = 2.0

T_BOOL, T_INT, T_FLOAT, T_STR, T_LIST, T_PASSWORD = "bool", "int", "float", "str", "list", "password"

# ---------- 内部工具 ----------

def _env(key, default=None):
    """读取环境变量；空串视作"未设置"。

    这一点很重要：docker-compose 里写 ``API_KEY: ${API_KEY:-}`` 会注入空串，
    如果把空串当成有效值，就会把"未配置"变成"配了个空 key"。
    """
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def _env_bool(key, default=False):
    raw = _env(key)
    return default if raw is None else str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(key, default):
    raw = _env(key)
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _env_list(key, default=None):
    raw = _env(key)
    if raw is None:
        return default
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def coerce(value: Any, schema: dict) -> Any:
    """把任意输入强制转换为 schema 声明的类型。"""
    kind = schema["type"]
    if kind == T_BOOL:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind == T_INT:
        return int(value)
    if kind == T_FLOAT:
        return float(value)
    if kind == T_LIST:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [part.strip() for part in str(value).split(",") if part.strip()]
    if kind == T_PASSWORD:
        return str(value)
    return str(value)


def _build_schema() -> Dict[str, dict]:
    """声明全部可调参数。

    ``secure=True`` 的参数在 Redis 不可用时不允许回落到默认值，
    由 :func:`get_secure` 强制 fail-closed。
    """
    return {
        # ---------- 服务端 / 网关 ----------
        "HOST": dict(group="server", type=T_STR, default=_env("HOST", setting.HOST),
                     desc="API 监听地址", desc_en="API listen address", restart=True),
        "PORT": dict(group="server", type=T_INT, default=_env_int("PORT", setting.PORT),
                     min=1, max=65535, desc="API 端口", desc_en="API port", restart=True),
        "API_KEY": dict(group="server", type=T_PASSWORD, default=_env("API_KEY", setting.API_KEY),
                        desc="API 密钥，留空则不鉴权（仅限内网）",
                        desc_en="API key, empty disables auth (intranet only)",
                        secure=True),
        "RATE_LIMIT_QPS": dict(group="server", type=T_INT,
                               default=_env_int("RATE_LIMIT_QPS", setting.RATE_LIMIT_QPS),
                               min=0, max=100000, desc="单标识每秒最大请求数（0=不限）",
                               desc_en="Max requests per second per identity (0 = unlimited)",
                               secure=True),
        "POOL_LOW_THRESHOLD": dict(group="server", type=T_INT,
                                   default=_env_int("POOL_LOW_THRESHOLD", setting.POOL_LOW_THRESHOLD),
                                   min=0, max=100000, desc="池枯竭阈值",
                                   desc_en="Pool exhaustion threshold"),
        "IP_REUSE_WINDOW": dict(group="server", type=T_INT,
                                default=_env_int("IP_REUSE_WINDOW", setting.IP_REUSE_WINDOW),
                                min=0, max=86400, desc="同 IP 复分发冷却窗口（秒）",
                                desc_en="Reuse cooldown window (seconds)"),
        "DISPATCH_CANDIDATE_LIMIT": dict(group="server", type=T_INT,
                                         default=_env_int("DISPATCH_CANDIDATE_LIMIT",
                                                          setting.DISPATCH_CANDIDATE_LIMIT),
                                         min=10, max=5000, desc="单次分发候选上限",
                                         desc_en="Max candidates per dispatch"),
        "ALL_MAX": dict(group="server", type=T_INT, default=_env_int("ALL_MAX", setting.ALL_MAX),
                        min=1, max=5000, desc="/all/ 单次返回上限",
                        desc_en="/all/ max items per call"),
        "BATCH_MAX": dict(group="server", type=T_INT, default=_env_int("BATCH_MAX", setting.BATCH_MAX),
                          min=1, max=1000, desc="/batch/ 单次返回上限",
                          desc_en="/batch/ max items per call"),
        "METRICS_AUTH": dict(group="server", type=T_BOOL,
                             default=_env_bool("METRICS_AUTH", setting.METRICS_AUTH),
                             desc="/metrics 是否要求 API Key", desc_en="Require API key on /metrics",
                             secure=True),
        "TRUSTED_PROXIES": dict(group="server", type=T_LIST,
                                default=_env_list("TRUSTED_PROXIES", setting.TRUSTED_PROXIES),
                                desc="可信反向代理网段，只有直连来源命中时才采信 XFF",
                                desc_en="Trusted proxy CIDRs; X-Forwarded-For honoured only from these",
                                secure=True),

        # ---------- 存储 ----------
        "TABLE_NAME": dict(group="storage", type=T_STR,
                           default=_env("TABLE_NAME", setting.TABLE_NAME),
                           desc="代理存储 Hash 名称", desc_en="Proxy hash name", restart=True),
        "DB_CONN": dict(group="storage", type=T_PASSWORD,
                        default=_env("DB_CONN", setting.DB_CONN),
                        desc="Redis 连接串（改后需重启）",
                        desc_en="Redis connection string (restart required)",
                        restart=True),
        "PROXY_SCORE_INIT": dict(group="storage", type=T_INT, default=_env_int("PROXY_SCORE_INIT", setting.PROXY_SCORE_INIT),
                                 min=0, max=100, desc="代理入池初始分",
                                 desc_en="Initial score on insert"),
        "PROXY_SCORE_MAX": dict(group="storage", type=T_INT, default=_env_int("PROXY_SCORE_MAX", setting.PROXY_SCORE_MAX),
                                min=1, max=999, desc="代理满分值（兼容字段，复合分模型未使用）",
                                desc_en="Maximum score (legacy, unused by composite score)"),
        "PROXY_SCORE_MIN": dict(group="storage", type=T_INT, default=_env_int("PROXY_SCORE_MIN", setting.PROXY_SCORE_MIN),
                                min=0, max=99, desc="低于该分值即淘汰（兼容字段，现行淘汰看错误率）",
                                desc_en="Evict at or below this score (legacy, error-rate eviction is active)"),
        "PROXY_SCORE_STEP": dict(group="storage", type=T_INT, default=_env_int("PROXY_SCORE_STEP", setting.PROXY_SCORE_STEP),
                                 min=1, max=50, desc="单次校验通过加分（兼容字段，不再手工累加）",
                                 desc_en="Score step per successful validation (legacy, no longer accumulated)"),
        "PROXY_TTL": dict(group="storage", type=T_INT, default=_env_int("PROXY_TTL", setting.PROXY_TTL),
                          min=0, max=2592000, desc="代理最长存活时间（秒，0=不过期）",
                          desc_en="Max proxy lifetime in seconds (0 = never expires)"),
        "QUEUE_MAX_LEN": dict(group="storage", type=T_INT,
                              default=_env_int("QUEUE_MAX_LEN", setting.QUEUE_MAX_LEN),
                              min=1000, max=1000000, desc="待验队列上限",
                              desc_en="Pending validation queue cap"),
        "QUEUE_DEDUP_TTL": dict(group="storage", type=T_INT,
                                default=_env_int("QUEUE_DEDUP_TTL", setting.QUEUE_DEDUP_TTL),
                                min=0, max=86400, desc="采集去重集合存活（秒，0=不设过期）",
                                desc_en="Fetch dedup set lifetime (seconds)"),
        "PROXY_MAX_ERROR_RATE": dict(group="storage", type=T_FLOAT,
                                     default=_env("PROXY_MAX_ERROR_RATE", setting.PROXY_MAX_ERROR_RATE),
                                     min=0.0, max=1.0, desc="错误率超过该值且检验次数达标即淘汰",
                                     desc_en="Evict when error rate exceeds this value"),
        "PROXY_EVICT_MIN_CHECKS": dict(group="storage", type=T_INT,
                                       default=_env_int("PROXY_EVICT_MIN_CHECKS", setting.PROXY_EVICT_MIN_CHECKS),
                                       min=1, max=10000, desc="错误率淘汰生效前的最低检验次数",
                                       desc_en="Min checks before error-rate eviction applies"),
        "TIER_FAST_MS": dict(group="storage", type=T_INT,
                             default=_env_int("TIER_FAST_MS", setting.TIER_FAST_MS),
                             min=10, max=60000, desc="延迟分层：fast 阈值（毫秒）",
                             desc_en="Latency tier: fast threshold (ms)"),
        "TIER_MEDIUM_MS": dict(group="storage", type=T_INT,
                               default=_env_int("TIER_MEDIUM_MS", setting.TIER_MEDIUM_MS),
                               min=10, max=120000, desc="延迟分层：medium 阈值（毫秒）",
                               desc_en="Latency tier: medium threshold (ms)"),

        # ---------- 采集 ----------
        "FETCH_INTERVAL": dict(group="fetch", type=T_INT, default=_env_int("FETCH_INTERVAL", setting.FETCH_INTERVAL),
                               min=1, max=1440, desc="采集周期（分钟）", desc_en="Fetch interval (minutes)"),
        "FETCHER_PAUSED": dict(group="fetch", type=T_BOOL, default=_env_bool("FETCHER_PAUSED", setting.FETCHER_PAUSED),
                               desc="暂停采集", desc_en="Pause fetching"),
        "FETCH_TIMEOUT": dict(group="fetch", type=T_INT, default=_env_int("FETCH_TIMEOUT", setting.FETCH_TIMEOUT),
                              min=1, max=120, desc="采集请求超时（秒）", desc_en="Fetch timeout (seconds)"),
        "FETCH_RETRY": dict(group="fetch", type=T_INT, default=_env_int("FETCH_RETRY", setting.FETCH_RETRY),
                            min=0, max=10, desc="采集重试次数", desc_en="Fetch retry count"),
        "FETCHER_MAX_FAIL": dict(group="fetch", type=T_INT,
                                 default=_env_int("FETCHER_MAX_FAIL", setting.FETCHER_MAX_FAIL),
                                 min=1, max=100, desc="单源连续失败熔断阈值",
                                 desc_en="Consecutive failures before a source trips"),
        "FETCHER_COOLDOWN": dict(group="fetch", type=T_INT,
                                 default=_env_int("FETCHER_COOLDOWN", setting.FETCHER_COOLDOWN),
                                 min=10, max=86400, desc="熔断冷却（秒）",
                                 desc_en="Tripped source cooldown (seconds)"),
        "FETCHER_CONCURRENCY": dict(group="fetch", type=T_INT,
                                    default=_env_int("FETCHER_CONCURRENCY", setting.FETCHER_CONCURRENCY),
                                    min=1, max=64, desc="并发采集的源数上限",
                                    desc_en="Max concurrent fetchers"),
        "FILTER_INTERNAL": dict(group="fetch", type=T_BOOL,
                                default=_env_bool("FILTER_INTERNAL", setting.FILTER_INTERNAL),
                                desc="过滤内网与保留地址", desc_en="Filter private and reserved IPs"),

        # ---------- 校验 ----------
        "CHECK_INTERVAL": dict(group="check", type=T_INT, default=_env_int("CHECK_INTERVAL", setting.CHECK_INTERVAL),
                               min=1, max=1440, desc="存量复检周期（分钟）",
                               desc_en="Recheck interval (minutes)"),
        "CHECK_CONCURRENCY": dict(group="check", type=T_INT, default=_env_int("CHECK_CONCURRENCY", setting.CHECK_CONCURRENCY),
                                  min=5, max=500, desc="异步校验并发数",
                                  desc_en="Async validation concurrency"),
        "HTTP_URL": dict(group="check", type=T_STR, default=_env("HTTP_URL", setting.HTTP_URL),
                         desc="HTTP 校验目标", desc_en="HTTP validation target"),
        "HTTPS_URL": dict(group="check", type=T_STR, default=_env("HTTPS_URL", setting.HTTPS_URL),
                          desc="HTTPS 校验目标", desc_en="HTTPS validation target"),
        "VERIFY_TIMEOUT": dict(group="check", type=T_INT,
                               default=_env_int("VERIFY_TIMEOUT", setting.VERIFY_TIMEOUT),
                               min=1, max=60, desc="校验超时（秒）", desc_en="Validation timeout (seconds)"),
        "TCP_CHECK_TIMEOUT": dict(group="check", type=T_INT,
                                  default=_env_int("TCP_CHECK_TIMEOUT", setting.TCP_CHECK_TIMEOUT),
                                  min=1, max=30, desc="TCP 连通超时（秒）",
                                  desc_en="TCP reachability timeout (seconds)"),
        "CHECK_MULTI_ROUND": dict(group="check", type=T_INT,
                                  default=_env_int("CHECK_MULTI_ROUND", setting.CHECK_MULTI_ROUND),
                                  min=1, max=5, desc="连续通过次数才判定有效",
                                  desc_en="Consecutive passes required"),
        "BLACKLIST_TTL": dict(group="check", type=T_INT,
                              default=_env_int("BLACKLIST_TTL", setting.BLACKLIST_TTL),
                              min=60, max=86400, desc="黑名单存活（秒）",
                              desc_en="Blacklist entry lifetime (seconds)"),
        "MAX_FAIL_COUNT": dict(group="check", type=T_INT,
                               default=_env_int("MAX_FAIL_COUNT", setting.MAX_FAIL_COUNT),
                               min=0, max=999, desc="超过该失败次数剔除",
                               desc_en="Consecutive failures before removal"),
        "PROXY_REGION": dict(group="check", type=T_BOOL,
                             default=_env_bool("PROXY_REGION", setting.PROXY_REGION),
                             desc="解析代理地域", desc_en="Resolve proxy region"),
        "REGION_CONCURRENCY": dict(group="check", type=T_INT,
                                   default=_env_int("REGION_CONCURRENCY", setting.REGION_CONCURRENCY),
                                   min=1, max=50, desc="地域解析并发上限",
                                   desc_en="Region lookup concurrency"),
        "REGION_CACHE_TTL": dict(group="check", type=T_INT,
                                 default=_env_int("REGION_CACHE_TTL", setting.REGION_CACHE_TTL),
                                 min=60, max=2592000, desc="地域缓存时长（秒）",
                                 desc_en="Region cache TTL (seconds)"),
        "GEOIP_ENABLED": dict(group="check", type=T_BOOL,
                              default=_env_bool("GEOIP_ENABLED", setting.GEOIP_ENABLED),
                              desc="启用 GeoIP mmdb（留空路径时自动回落 HTTP）",
                              desc_en="Enable GeoIP mmdb (falls back to HTTP if path empty)"),
        "GEOIP_DB": dict(group="check", type=T_STR,
                         default=_env("GEOIP_DB", setting.GEOIP_DB),
                         desc="GeoLite2-Country.mmdb 路径",
                         desc_en="GeoLite2-Country.mmdb path"),
        "ASN_ENABLED": dict(group="check", type=T_BOOL,
                            default=_env_bool("ASN_ENABLED", setting.ASN_ENABLED),
                            desc="启用 ASN 推导（机房/住宅）",
                            desc_en="Enable ASN inference (datacenter/residential)"),
        "ASN_DB": dict(group="check", type=T_STR,
                       default=_env("ASN_DB", setting.ASN_DB),
                       desc="GeoLite2-ASN.mmdb 路径",
                       desc_en="GeoLite2-ASN.mmdb path"),
        "DNSBL_ENABLED": dict(group="check", type=T_BOOL,
                              default=_env_bool("DNSBL_ENABLED", setting.DNSBL_ENABLED),
                              desc="启用 DNSBL 声誉过滤",
                              desc_en="Enable DNSBL reputation filter"),
        "DNSBL_ZONES": dict(group="check", type=T_LIST,
                            default=_env("DNSBL_ZONES", setting.DNSBL_ZONES),
                            desc="DNSBL zone 列表（逗号分隔）",
                            desc_en="DNSBL zones (comma-separated)"),
        "DNSBL_CACHE_TTL": dict(group="check", type=T_INT,
                                default=_env_int("DNSBL_CACHE_TTL", setting.DNSBL_CACHE_TTL),
                                min=60, max=86400, desc="DNSBL 结果缓存时长（秒）",
                                desc_en="DNSBL result cache TTL (seconds)"),

        # ---------- MySQL 冷存储归档（可选，不参与实时链路） ----------
        "MYSQL_ENABLED": dict(group="archive", type=T_BOOL,
                              default=_env_bool("MYSQL_ENABLED", False),
                              desc="启用 MySQL 冷存储归档",
                              desc_en="Enable MySQL cold archive"),
        "MYSQL_HOST": dict(group="archive", type=T_STR,
                           default=_env("MYSQL_HOST", "127.0.0.1"),
                           desc="MySQL 主机", desc_en="MySQL host"),
        "MYSQL_PORT": dict(group="archive", type=T_INT,
                           default=_env_int("MYSQL_PORT", 3306),
                           min=1, max=65535, desc="MySQL 端口", desc_en="MySQL port"),
        "MYSQL_USER": dict(group="archive", type=T_STR,
                           default=_env("MYSQL_USER", "fpp"),
                           desc="MySQL 用户", desc_en="MySQL user"),
        "MYSQL_PASSWORD": dict(group="archive", type=T_PASSWORD,
                               default=_env("MYSQL_PASSWORD", ""),
                               desc="MySQL 口令", desc_en="MySQL password"),
        "MYSQL_DB": dict(group="archive", type=T_STR,
                         default=_env("MYSQL_DB", "fpp"),
                         desc="MySQL 数据库名", desc_en="MySQL database"),

        # ---------- 调度 ----------
        "TIMEZONE": dict(group="scheduler", type=T_STR, default=_env("TIMEZONE", setting.TIMEZONE),
                         desc="时区", desc_en="Timezone", restart=True),
        "CLEAN_INTERVAL": dict(group="scheduler", type=T_INT,
                               default=_env_int("CLEAN_INTERVAL", setting.CLEAN_INTERVAL),
                               min=60, max=86400, desc="数据清理周期（秒）",
                               desc_en="Cleanup interval (seconds)"),
        "STAT_INTERVAL": dict(group="scheduler", type=T_INT,
                              default=_env_int("STAT_INTERVAL", setting.STAT_INTERVAL),
                              min=30, max=3600, desc="统计与告警周期（秒）",
                              desc_en="Stats interval (seconds)"),
        "REGION_INDEX_REBUILD_INTERVAL": dict(
            group="scheduler", type=T_INT,
            default=_env_int("REGION_INDEX_REBUILD_INTERVAL",
                             setting.REGION_INDEX_REBUILD_INTERVAL),
            min=60, max=86400, desc="地域索引全量重建周期（秒）",
            desc_en="Region index rebuild interval (seconds)"),

        # ---------- 告警 ----------
        "ALERT_WEBHOOK": dict(group="alert", type=T_STR, default=_env("ALERT_WEBHOOK", setting.ALERT_WEBHOOK),
                              desc="告警 Webhook（钉钉/企微）", desc_en="Alert webhook URL"),
        "ALERT_COOLDOWN": dict(group="alert", type=T_INT,
                               default=_env_int("ALERT_COOLDOWN", setting.ALERT_COOLDOWN),
                               min=10, max=86400, desc="同类告警冷却（秒）",
                               desc_en="Alert cooldown (seconds)"),

        # ---------- 管理后台 ----------
        "ADMIN_ENABLED": dict(group="admin", type=T_BOOL, default=_env_bool("ADMIN_ENABLED", setting.ADMIN_ENABLED),
                              desc="启用管理后台", desc_en="Enable admin console", secure=True),
        "ADMIN_PASSWORD": dict(group="admin", type=T_PASSWORD,
                               default=_env("ADMIN_PASSWORD", setting.ADMIN_PASSWORD),
                               desc="管理密码", desc_en="Admin password", secure=True),
        "ADMIN_SESSION_TTL": dict(group="admin", type=T_INT,
                                  default=_env_int("ADMIN_SESSION_TTL", setting.ADMIN_SESSION_TTL),
                                  min=300, max=2592000, desc="会话有效期（秒）",
                                  desc_en="Session lifetime (seconds)", secure=True),
        "ADMIN_FORCE_PASSWORD_CHANGE": dict(group="admin", type=T_BOOL,
                                            default=_env_bool("ADMIN_FORCE_PASSWORD_CHANGE",
                                                              setting.ADMIN_FORCE_PASSWORD_CHANGE),
                                            desc="仍使用默认口令时首登强制改密",
                                            desc_en="Force password change on first login",
                                            secure=True),
        "LOGIN_MAX_FAIL": dict(group="admin", type=T_INT, default=_env_int("LOGIN_MAX_FAIL", setting.LOGIN_MAX_FAIL),
                               min=3, max=100, desc="登录失败锁定阈值",
                               desc_en="Login attempts before lockout", secure=True),
        "LOGIN_FAIL_TTL": dict(group="admin", type=T_INT, default=_env_int("LOGIN_FAIL_TTL", setting.LOGIN_FAIL_TTL),
                               min=10, max=86400, desc="登录失败锁定时长（秒）",
                               desc_en="Login lockout duration (seconds)", secure=True),

        # ---------- 日志 ----------
        "LOG_LEVEL": dict(group="log", type=T_STR, default=_env("LOG_LEVEL", setting.LOG_LEVEL),
                          choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                          desc="日志级别", desc_en="Log level"),
        "LOG_TO_FILE": dict(group="log", type=T_BOOL, default=_env_bool("LOG_TO_FILE", setting.LOG_TO_FILE),
                            desc="同时写本地日志文件", desc_en="Also write log files"),
    }


CONFIG_SCHEMA = _build_schema()

GROUP_LABELS = {
    "server": "服务端", "storage": "存储与评分", "fetch": "采集",
    "check": "校验", "scheduler": "调度", "alert": "告警",
    "admin": "管理后台", "archive": "归档", "log": "日志",
}
GROUP_LABELS_EN = {
    "server": "Server", "storage": "Storage & Scoring", "fetch": "Fetching",
    "check": "Validation", "scheduler": "Scheduler", "alert": "Alerting",
    "admin": "Admin Console", "archive": "Archive", "logging": "Logging", "log": "Logging",
}

# ---------- Redis 连接 ----------

_pool = None
_pool_lock = threading.RLock()
_cache: Dict[str, tuple] = {}
_cache_version = 0


def _parse_conn(raw: str) -> dict:
    """解析 redis URL，返回连接参数。"""
    from urllib.parse import urlparse
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("redis", "rediss"):
        raise ConfigError("DB_CONN 仅支持 redis:// 或 rediss:// 连接串")
    db = 0
    if parsed.path and len(parsed.path) > 1:
        try:
            db = int(parsed.path[1:])
        except ValueError:
            db = 0
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 6379,
        "password": parsed.password or None,
        "username": parsed.username or None,
        "db": db,
        "ssl": parsed.scheme.lower() == "rediss",
    }


def get_redis():
    """获取 Redis 客户端（连接池单例）。

    惰性建立连接池：``Redis(connection_pool=...)`` 本身不会发起网络连接，
    因此模块 import 阶段不会因为 Redis 未启动而失败。

    v0.1.0 关键修复（两处，均由真实 Redis 端到端验证发现）：

    1. ``from redis import Redis`` 原本写在 ``if _pool is None:`` 分支内部，
       而 ``return Redis(...)`` 在分支之外 —— Python 因为函数体内存在
       import 语句而把 ``Redis`` 当作**局部变量**，于是**第二次**调用
       （_pool 已存在、跳过 import）直接抛 ``UnboundLocalError``。
    2. 手工拼 ``BlockingConnectionPool(host=..., ssl=...)`` 在 redis-py 5+
       会把 ``ssl`` 透传给连接类，触发
       ``TypeError: AbstractConnection.__init__() got an unexpected keyword
       argument 'ssl'``。这里改用官方 ``from_url``，由 redis-py 自己处理
       ``redis://`` 与 ``rediss://`` 的差异。

    import 已提到模块顶层，避免再次踩入局部变量陷阱。
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            # 先做一次格式校验，配置写错时给出明确错误而不是等到首次请求
            _parse_conn(_effective_db_conn())
            _pool = BlockingConnectionPool.from_url(
                _effective_db_conn(),
                decode_responses=True,
                # 固定 RESP2：redis-py 8 默认走 RESP3（HELLO 握手），
                # 而大量在用的 Redis 5/6 与部分托管实例不支持 HELLO，
                # 会直接报 "unknown command `HELLO`"。本项目用不到 RESP3 特性。
                protocol=2,
                socket_connect_timeout=2,
                socket_timeout=5,
                socket_keepalive=True,
                max_connections=64,
            )
        return Redis(connection_pool=_pool)


def _effective_db_conn() -> str:
    """确定实际使用的 Redis 连接串：环境变量 > setting 默认值。

    DB_CONN 标注为"需重启"，因此这里**不读 Redis 里的覆盖值**——
    否则会出现"用 Redis A 连上 Redis B 去改 DB_CONN"的自指死结。
    """
    return _env("DB_CONN", setting.DB_CONN)


def reset_connection_pool() -> None:
    """重建连接池（测试用）。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.disconnect()
            except Exception:
                pass
        _pool = None
        _bump_cache()


def _bump_cache() -> None:
    global _cache_version
    _cache_version += 1
    _cache.clear()


# ---------- 读取 ----------

def get(key: str, default: Any = None) -> Any:
    """读取配置项；Redis 异常时回落到 schema 默认值（保持可用性）。"""
    schema = CONFIG_SCHEMA.get(key)
    if schema is None:
        return default
    now = time.time()
    cached = _cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    try:
        raw = get_redis().hget(KEY_CONFIG, key)
        value = coerce(raw, schema) if raw is not None else schema["default"]
    except Exception:
        value = schema["default"]
    _cache[key] = (value, now + CACHE_TTL)
    return value


def get_secure(key: str) -> Any:
    """读取**安全相关**配置项；读不到就抛 :class:`BackendUnavailable`。

    仅当 Redis 确实可用、只是"该 key 没被覆盖"时才返回默认值。
    Redis 不可用 → 抛 503，由网关拒绝服务，而不是放开鉴权。
    """
    schema = CONFIG_SCHEMA.get(key)
    if schema is None:
        raise ConfigError("未知配置项：%s" % key)
    try:
        raw = get_redis().hget(KEY_CONFIG, key)
    except Exception as exc:
        raise BackendUnavailable(
            "配置服务不可用，已拒绝请求以保证安全",
            detail="config key=%s error=%s" % (key, type(exc).__name__),
        )
    if raw is None:
        return schema["default"]
    try:
        return coerce(raw, schema)
    except (ValueError, TypeError) as exc:
        raise ConfigError("配置项 %s 取值非法：%s" % (key, exc))


def all_values() -> Dict[str, Any]:
    return {key: get(key) for key in CONFIG_SCHEMA}


def describe() -> list:
    """返回配置项元信息（管理后台配置页使用）。

    密码类字段只回 ``has_value`` 布尔标记，**不回任何明文或掩码串**。
    旧版回 ``***``，前端在某些路径下会把 ``***`` 当成新密码回传，
    最终把真实口令改成三个星号 —— 详见文档《BUG 修复清单》#36。
    """
    items = []
    for key, schema in CONFIG_SCHEMA.items():
        value = get(key)
        item = {
            "key": key,
            "group": schema["group"],
            "group_label": GROUP_LABELS.get(schema["group"], schema["group"]),
            "group_label_en": GROUP_LABELS_EN.get(schema["group"], schema["group"]),
            "type": schema["type"],
            "default": schema["default"] if schema["type"] != T_PASSWORD else None,
            "desc": schema.get("desc", ""),
            "desc_en": schema.get("desc_en", schema.get("desc", "")),
            "require_restart": schema.get("restart", False),
            "secure": schema.get("secure", False),
            "value": value if schema["type"] != T_PASSWORD else None,
            "has_value": bool(value) if schema["type"] == T_PASSWORD else None,
        }
        for bound in ("min", "max", "choices"):
            if bound in schema:
                item[bound] = schema[bound]
        items.append(item)
    return items


# ---------- 写入 ----------

def _validate_value(key: str, raw_value: Any, schema: dict) -> Any:
    value = coerce(raw_value, schema)
    if "min" in schema and value < schema["min"]:
        raise ConfigError("%s 小于最小值 %s" % (key, schema["min"]))
    if "max" in schema and value > schema["max"]:
        raise ConfigError("%s 大于最大值 %s" % (key, schema["max"]))
    if "choices" in schema and value not in schema["choices"]:
        raise ConfigError("%s 取值必须为 %s" % (key, schema["choices"]))
    return value


def _check_tier_thresholds(prepared: Dict[str, Any]) -> list:
    """校验延迟分层阈值的顺序（跨字段约束）。

    ``TIER_FAST_MS`` 必须**小于** ``TIER_MEDIUM_MS``，否则 ``fast`` 档会把
    整个 ``medium`` 区间吞掉，表现为"medium 档永远为空"的畸形分布。
    这里把本次待写入的值与库中已有值合并后再校验，
    因此"只改其中一个"和"两个一起改"都能被正确拦截。
    """
    touched = ("TIER_FAST_MS" in prepared) or ("TIER_MEDIUM_MS" in prepared)
    if not touched:
        return []

    fast = prepared.get("TIER_FAST_MS")
    medium = prepared.get("TIER_MEDIUM_MS")
    if fast is None:
        try:
            fast = get("TIER_FAST_MS")
        except Exception:
            fast = None
    if medium is None:
        try:
            medium = get("TIER_MEDIUM_MS")
        except Exception:
            medium = None
    if fast is None or medium is None:
        return []
    try:
        fast = int(fast)
        medium = int(medium)
    except (TypeError, ValueError):
        return []
    if fast >= medium:
        return [{"key": "TIER_FAST_MS",
                 "error": "TIER_FAST_MS 必须小于 TIER_MEDIUM_MS（当前 %d >= %d）"
                          % (fast, medium)}]
    return []


def set_many(updates: Dict[str, Any]) -> list:
    """批量写入配置，返回校验失败的条目列表。

    密码类字段写入前做强度校验；所有值校验通过后才统一写入，
    避免"一半成功一半失败"造成的配置漂移。
    """
    from .core.validators import validate_check_url, validate_webhook

    prepared = {}
    failed = []
    for key, raw_value in (updates or {}).items():
        schema = CONFIG_SCHEMA.get(key)
        if schema is None:
            failed.append({"key": key, "error": "未知配置项"})
            continue
        try:
            value = _validate_value(key, raw_value, schema)
            if key == "ADMIN_PASSWORD":
                if not value:
                    raise ConfigError("管理密码不能为空")
                from .core.security import password_strength_error
                problem = password_strength_error(value)
                if problem:
                    raise ConfigError(problem)
            elif key == "ALERT_WEBHOOK":
                value = validate_webhook(value)
            elif key in ("HTTP_URL", "HTTPS_URL"):
                value = validate_check_url(value, key)
            elif key == "TABLE_NAME":
                if not value.isalnum():
                    raise ConfigError("TABLE_NAME 仅允许字母与数字")
            prepared[key] = value
        except (ConfigError, ValueError, TypeError) as exc:
            failed.append({"key": key, "error": str(exc)})

    failed.extend(_check_tier_thresholds(prepared))

    if failed:
        return failed

    pipe = get_redis().pipeline()
    for key, value in prepared.items():
        schema = CONFIG_SCHEMA[key]
        if schema["type"] == T_BOOL:
            pipe.hset(KEY_CONFIG, key, "1" if value else "0")
        elif schema["type"] == T_LIST:
            pipe.hset(KEY_CONFIG, key, ",".join(value))
        elif key == "ADMIN_PASSWORD":
            pipe.hset(KEY_CONFIG, key, hash_password(value))
        else:
            pipe.hset(KEY_CONFIG, key, str(value))
    pipe.execute()
    _bump_cache()
    return []


def reset(keys: Optional[list] = None) -> None:
    """重置配置项（恢复为默认值）。``keys`` 为空或 None 表示全部重置。"""
    if keys is None:
        targets = list(CONFIG_SCHEMA.keys())
    elif isinstance(keys, (list, tuple)):
        targets = [k for k in keys if isinstance(k, str)]
    else:
        # v0.1.0 修复：旧版传字符串时会被逐字符迭代，结果"什么都没重置"却返回成功。
        raise ConfigError("keys 必须是字符串数组")

    pipe = get_redis().pipeline()
    for key in targets:
        if key in CONFIG_SCHEMA:
            pipe.hdel(KEY_CONFIG, key)
    pipe.execute()
    _bump_cache()


# ---------- 采集源 ----------

def list_fetchers() -> list:
    """列出全部采集源及其启用状态与运行统计。"""
    from .fetcher.registry import discover
    conn = get_redis()
    saved = conn.hgetall(KEY_FETCHERS)
    result = []
    for cls in discover():
        name = cls.name
        saved_value = saved.get(name)
        enabled = saved_value == "1" if saved_value is not None else getattr(cls, "enabled", True)
        try:
            stat = json.loads(conn.hget(KEY_STAT, "fetcher:%s" % name) or "{}")
        except (TypeError, ValueError):
            stat = {}
        result.append({
            "name": name,
            "class": cls.__name__,
            "url": getattr(cls, "url", ""),
            "enabled": enabled,
            "urls_override": get_fetcher_urls(name, None) or [],
            "is_html": getattr(cls, "is_html", False),
            # P0-1: 该源产出的代理的传输协议（http / socks4 / socks5）
            "protocol": getattr(cls, "protocol", "http"),
            "status_note": getattr(cls, "status_note", ""),
            "stat": {
                "total": stat.get("total", 0),
                "last_count": stat.get("last_count", 0),
                "last_run": stat.get("last_run", ""),
                "tripped_until": stat.get("tripped_until", ""),
            },
        })
    return result


def is_known_fetcher(name: str) -> bool:
    from .fetcher.registry import get_fetcher
    return get_fetcher(name) is not None


def set_fetcher(name: str, enabled: bool) -> None:
    if not is_known_fetcher(name):
        raise ConfigError("未知的采集源：%s" % name)
    get_redis().hset(KEY_FETCHERS, name, "1" if enabled else "0")


def reset_fetcher(name: str) -> None:
    if not is_known_fetcher(name):
        raise ConfigError("未知的采集源：%s" % name)
    conn = get_redis()
    pipe = conn.pipeline()
    pipe.hdel(KEY_FETCHERS, name)
    pipe.hdel(KEY_FETCHERS, "%s:urls" % name)
    pipe.execute()


def is_fetcher_enabled(name: str, default: bool = True) -> bool:
    try:
        value = get_redis().hget(KEY_FETCHERS, name)
    except Exception:
        return default
    return default if value is None else value == "1"


def get_fetcher_urls(name: str, default: Optional[list] = None) -> Optional[list]:
    """读取采集源 URL 覆盖；未覆盖返回 ``default``。"""
    try:
        raw = get_redis().hget(KEY_FETCHERS, "%s:urls" % name)
    except Exception:
        return default
    if raw is None:
        return default
    return [item.strip() for item in str(raw).split("\n") if item.strip()]


def set_fetcher_urls(name: str, urls: list) -> None:
    """覆盖采集源 URL（已做 http/https 白名单校验，防 SSRF）。"""
    from .core.validators import validate_fetch_urls
    if not is_known_fetcher(name):
        raise ConfigError("未知的采集源：%s" % name)
    cleaned = validate_fetch_urls(urls)
    if cleaned:
        get_redis().hset(KEY_FETCHERS, "%s:urls" % name, "\n".join(cleaned))
    else:
        get_redis().hdel(KEY_FETCHERS, "%s:urls" % name)


def record_fetcher_stat(name: str, count: int, *, trip_until: str = "") -> None:
    """累加单源统计。失败不影响采集主流程。"""
    try:
        conn = get_redis()
        raw = conn.hget(KEY_STAT, "fetcher:%s" % name)
        try:
            stat = json.loads(raw or "{}")
        except (TypeError, ValueError):
            stat = {}
        stat["total"] = stat.get("total", 0) + int(count)
        stat["last_count"] = int(count)
        stat["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if trip_until:
            stat["tripped_until"] = trip_until
        conn.hset(KEY_STAT, "fetcher:%s" % name, json.dumps(stat, ensure_ascii=False))
    except Exception:
        pass


def record_collector_stat(count: int) -> None:
    """记录采集服务整体统计（独立命名空间，不污染 fetcher:*）。"""
    try:
        conn = get_redis()
        try:
            stat = json.loads(conn.hget(KEY_STAT, "collector") or "{}")
        except (TypeError, ValueError):
            stat = {}
        stat["total"] = stat.get("total", 0) + int(count)
        stat["last_count"] = int(count)
        stat["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.hset(KEY_STAT, "collector", json.dumps(stat, ensure_ascii=False))
    except Exception:
        pass


def breaker_state() -> dict:
    """返回当前处于熔断状态的采集源集合。

    v0.1.0 新增：熔断状态原先只存在于**采集进程内存**的模块级 dict 中，
    导致管理后台与 Prometheus 永远查不到别的进程/重启前的熔断状态。
    这里把熔断写入 Redis Hash，网关与调度器都能读取。
    """
    try:
        return {k: v for k, v in (get_redis().hgetall(KEY_BREAKER) or {}).items()}
    except Exception:
        return {}


def set_breaker(name: str, until_ts: float = 0) -> None:
    try:
        if until_ts and until_ts > time.time():
            get_redis().hset(KEY_BREAKER, name, str(int(until_ts)))
        else:
            get_redis().hdel(KEY_BREAKER, name)
    except Exception:
        pass


# ---------- 手动触发 ----------

def trigger(processor: str) -> None:
    if processor not in ("fetch", "check"):
        raise ConfigError("processor 仅支持 fetch 或 check")
    get_redis().hset(KEY_TRIGGER, processor, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def consume_trigger(processor: str) -> bool:
    """读取并清除一次性触发标记。

    v0.1.0 修复：旧版 ``cc.trigger()`` 写入的 key 没有任何消费者，
    管理后台的"立即采集/立即校验"按钮点了完全没有反应。
    """
    try:
        pipe = get_redis().pipeline()
        pipe.hget(KEY_TRIGGER, processor)
        pipe.hdel(KEY_TRIGGER, processor)
        value, _ = pipe.execute()
        return bool(value)
    except Exception:
        return False


# ---------- 运行统计 ----------

def mark_stat(name: str, value: Any = None) -> None:
    if value is None:
        value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        get_redis().hset(KEY_STAT, name, str(value))
    except Exception:
        pass


def get_stat(name: str, default: Any = None) -> Any:
    try:
        value = get_redis().hget(KEY_STAT, name)
    except Exception:
        return default
    return default if value is None else value


def get_stat_float(name: str, default: float = 0.0) -> float:
    try:
        return float(get_stat(name, default))
    except (TypeError, ValueError):
        return default


# ---------- 管理会话 ----------

def verify_password(raw: str) -> bool:
    """校验管理密码。

    存储值为 PBKDF2 哈希时走哈希校验；否则常量时间明文比较
    （兼容"直接用环境变量注入明文口令"的部署方式）。
    """
    stored = get_secure("ADMIN_PASSWORD")
    stored = "" if stored is None else str(stored)
    if is_hashed(stored):
        return verify_password_hash(str(raw), stored)
    if not stored:
        # 没有任何口令时，无论输入什么都不放行
        return False
    return constant_time_compare(raw, stored)


def is_default_password() -> bool:
    """当前是否仍在使用默认口令（用于首登强制改密）。"""
    try:
        stored = str(get_secure("ADMIN_PASSWORD") or "")
    except BackendUnavailable:
        return False
    if is_hashed(stored):
        # 默认口令一旦被改过就会存成哈希，不再等于明文默认值
        return False
    return constant_time_compare(stored, setting.ADMIN_PASSWORD)


def create_session() -> tuple:
    """创建管理会话，返回 ``(token, ttl)``。"""
    token = generate_token()
    ttl = int(get("ADMIN_SESSION_TTL", setting.ADMIN_SESSION_TTL))
    get_redis().hset(KEY_SESSIONS, token, str(time.time() + ttl))
    return token, ttl


def csrf_for(session_token: str) -> str:
    """由会话令牌确定性派生 CSRF 令牌。

    密钥取自一个**独立随机生成的 CSRF 专用密钥**（存于 Redis）。
    v0.1.0 变更：旧版用"管理密码的存储值"当密钥，导致
    ① 口令改成明文注入时密钥强度不可控；
    ② 改密码会连带让所有会话的 CSRF 失效，行为耦合。
    独立密钥后，改密行为只影响会话，不影响 CSRF 派生。
    """
    secret = _csrf_secret()
    return derive_csrf(session_token, secret)


def _csrf_secret() -> str:
    try:
        secret = get_redis().get(KEY_CSRF_SECRET)
        if secret:
            return secret
        secret = generate_token(32)
        get_redis().set(KEY_CSRF_SECRET, secret)
        return secret
    except Exception:
        # Redis 不可用时退化为进程内临时密钥：
        # 该模式下新建的会话 CSRF 仍自洽（登录与校验都在同一进程内完成），
        # 多副本部署下会不一致 —— 但此时登录本身也因 Redis 不可用而失败。
        return _fallback_csrf_secret


_fallback_csrf_secret = generate_token(32)


def verify_session(token: str) -> bool:
    """校验会话是否有效（过期自动清理）。"""
    if not token:
        return False
    try:
        raw = get_redis().hget(KEY_SESSIONS, token)
        if raw is None:
            return False
        if time.time() > float(raw):
            get_redis().hdel(KEY_SESSIONS, token)
            return False
        return True
    except Exception:
        # Redis 异常时判定为"未登录"，而不是放行
        return False


def destroy_session(token: str) -> None:
    if not token:
        return
    try:
        get_redis().hdel(KEY_SESSIONS, token)
    except Exception:
        pass


def destroy_other_sessions(keep_token: str) -> int:
    """踢除除 ``keep_token`` 外的全部会话（改密后调用）。"""
    removed = 0
    try:
        conn = get_redis()
        tokens = conn.hkeys(KEY_SESSIONS) or []
        pipe = conn.pipeline()
        for token in tokens:
            if token != keep_token:
                pipe.hdel(KEY_SESSIONS, token)
                removed += 1
        pipe.execute()
    except Exception:
        pass
    return removed


# ---------- 登录失败锁定 ----------

def login_lock_key(client_id: str) -> str:
    """构造登录失败计数的 Redis Key。

    v0.1.0 安全修复：旧版直接把不可信的 ``X-Forwarded-For`` 拼进 key
    （``fpp:loginfail:%s``），攻击者可构造任意长度/含控制字符的字符串，
    既能绕过锁定（每次换 key），也能打爆 Redis 内存。
    这里先做哈希归一化 + 白名单过滤，key 长度固定。
    """
    import hashlib
    import re
    safe = re.sub(r"[^0-9a-fA-F:.]", "", str(client_id or ""))[:64]
    digest = hashlib.sha256(safe.encode("utf-8")).hexdigest()[:32]
    return KEY_LOGIN_FAIL % digest


def login_fails(client_id: str) -> int:
    try:
        return int(get_redis().get(login_lock_key(client_id)) or 0)
    except Exception:
        return 0


def record_login_failure(client_id: str) -> int:
    """记录一次登录失败，返回当前累计失败次数。"""
    try:
        conn = get_redis()
        key = login_lock_key(client_id)
        pipe = conn.pipeline()
        pipe.incr(key)
        pipe.expire(key, int(get("LOGIN_FAIL_TTL", setting.LOGIN_FAIL_TTL)))
        count, _ = pipe.execute()
        return int(count)
    except Exception:
        return 0


def clear_login_failures(client_id: str) -> None:
    try:
        get_redis().delete(login_lock_key(client_id))
    except Exception:
        pass


def is_login_locked(client_id: str) -> bool:
    """是否已被锁定。

    Redis 不可用时 ``login_fails`` 返回 0，即**不锁定** —— 这是有意的取舍：
    存储故障时把管理员锁在门外会放大故障面。注意本函数**不发告警**，
    需要告警的调用方应自行判断 Redis 状态。
    """
    return login_fails(client_id) >= int(get("LOGIN_MAX_FAIL", setting.LOGIN_MAX_FAIL))


__all__ = [
    "CONFIG_SCHEMA", "GROUP_LABELS", "GROUP_LABELS_EN",
    "KEY_CONFIG", "KEY_FETCHERS", "KEY_TRIGGER", "KEY_STAT", "KEY_LOGS",
    "KEY_SESSIONS", "KEY_BREAKER", "KEY_CSRF_SECRET",
    "get", "get_secure", "get_redis", "all_values", "describe",
    "set_many", "reset", "coerce", "reset_connection_pool",
    "list_fetchers", "set_fetcher", "reset_fetcher", "is_fetcher_enabled",
    "get_fetcher_urls", "set_fetcher_urls", "record_fetcher_stat",
    "record_collector_stat", "is_known_fetcher", "breaker_state", "set_breaker",
    "trigger", "consume_trigger",
    "mark_stat", "get_stat", "get_stat_float",
    "verify_password", "is_default_password", "create_session", "verify_session",
    "destroy_session", "destroy_other_sessions", "csrf_for",
    "login_fails", "record_login_failure", "clear_login_failures",
    "is_login_locked", "login_lock_key",
]