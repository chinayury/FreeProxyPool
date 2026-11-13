# -*- coding: utf-8 -*-
"""Prometheus 指标定义与采集。

按"采集 / 校验 / 网关 / 存储"四类组织。

## v0.1.0 修复

旧版定义了 6 个 Gauge（``fpp_collect_count`` / ``fpp_valid_rate`` /
``fpp_avg_speed`` / ``fpp_fetcher_down`` 等）却**没有任何一处会更新它们** ——
要么从没被 ``.set()``，要么写入的 Redis stat key 在真实运行的进程里
（collector / validator / scheduler_aps）根本不存在。结果是
Grafana 上这几条曲线恒为 0，告警规则 ``fpp_valid_rate < 0.05``
也会因为 0 而**永远误报**。

现在：

* 指标由谁写、在哪写，全部在采集端显式落 Redis；
* ``update_metrics()`` 只做"读取 + set"，不再假设谁会写；
* 对确实没有数据源的指标直接删除，避免展示恒 0 的假曲线。
"""
from prometheus_client import Counter, Gauge

import time

# ---------- 网关（进程内 Counter，由中间件与路由埋点） ----------
gateway_requests = Counter(
    "fpp_gateway_requests_total", "网关累计请求数")
gateway_errors = Counter(
    "fpp_gateway_errors_total", "网关累计错误响应数（4xx/5xx）")
gateway_served = Counter(
    "fpp_gateway_served_total", "网关累计分发代理数")
gateway_rate_limited = Counter(
    "fpp_gateway_rate_limited_total", "网关累计限流触发次数")
gateway_reports = Counter(
    "fpp_gateway_reports_total", "客户端回报失效代理数")

# ---------- 采集 ----------
fetch_total = Gauge(
    "fpp_fetch_total", "累计采集到的原始代理总数")
fetch_round = Gauge(
    "fpp_fetch_round_seconds", "上一轮采集耗时（秒）")
fetch_stalled = Gauge(
    "fpp_fetch_stalled_seconds", "距上次采集完成的秒数（0 = 从未完成）")
fetcher_total = Gauge(
    "fpp_fetcher_total", "采集源总数")
fetcher_enabled = Gauge(
    "fpp_fetcher_enabled", "启用的采集源数")
fetcher_down = Gauge(
    "fpp_fetcher_down", "当前处于熔断状态的采集源数")

# ---------- 校验 ----------
check_total = Gauge(
    "fpp_check_total", "累计完成的校验条数")
check_pass_rate = Gauge(
    "fpp_check_pass_rate", "最近一轮校验通过率（0~1）")
avg_speed = Gauge(
    "fpp_avg_speed_seconds", "有效代理平均响应延迟（秒）")

# ---------- 存储 ----------
pool_total = Gauge(
    "fpp_pool_total", "有效代理池总数")
pool_https = Gauge(
    "fpp_pool_https", "支持 HTTPS 的代理数")
blacklist_total = Gauge(
    "fpp_blacklist_total", "黑名单条目数")
queue_depth = Gauge(
    "fpp_queue_depth", "待验代理队列积压")
redis_up = Gauge(
    "fpp_redis_up", "Redis 在线状态（1=在线，0=离线）")
redis_memory = Gauge(
    "fpp_redis_memory_bytes", "Redis 已用内存（字节）")


def set_gauge(gauge: Gauge, value, default=0) -> None:
    """容错地设置 Gauge：非法值一律落到 default，绝不让 /metrics 整体 500。"""
    try:
        gauge.set(float(value))
    except (TypeError, ValueError):
        try:
            gauge.set(float(default))
        except Exception:
            pass


def inc_counter(counter: Counter, amount: int = 1) -> None:
    try:
        counter.inc(amount)
    except Exception:
        pass


def update_metrics() -> None:
    """从 Redis / 采集端统计刷新全部 Gauge。"""
    from . import config_center as cc
    from .db.dbClient import get_store

    # ---- 存储 ----
    try:
        count = get_store().get_count()
        set_gauge(pool_total, count.get("total", 0))
        set_gauge(pool_https, count.get("https", 0))
        set_gauge(blacklist_total, cc.get_stat("pool:blacklist", 0))
    except Exception:
        set_gauge(pool_total, 0)
        set_gauge(pool_https, 0)

    try:
        from .db.queue import ProxyQueue
        set_gauge(queue_depth, ProxyQueue().depth())
    except Exception:
        set_gauge(queue_depth, 0)

    # ---- Redis ----
    try:
        conn = cc.get_redis()
        online = bool(conn.ping())
        set_gauge(redis_up, 1 if online else 0)
        if online:
            info = conn.info(section="memory") or {}
            set_gauge(redis_memory, info.get("used_memory", 0))
        else:
            set_gauge(redis_memory, 0)
    except Exception:
        set_gauge(redis_up, 0)
        set_gauge(redis_memory, 0)

    # ---- 采集源 ----
    try:
        fetchers = cc.list_fetchers()
        set_gauge(fetcher_total, len(fetchers))
        set_gauge(fetcher_enabled, sum(1 for f in fetchers if f.get("enabled")))
        set_gauge(fetcher_down, sum(
            1 for f in fetchers
            if f.get("enabled") and f.get("stat", {}).get("tripped_until")))
    except Exception:
        set_gauge(fetcher_total, 0)
        set_gauge(fetcher_enabled, 0)
        set_gauge(fetcher_down, 0)

    # ---- 采集 / 校验（数据由 collector / validator / scheduler 写入） ----
    set_gauge(fetch_total, cc.get_stat("fetch:total", 0))
    set_gauge(fetch_round, cc.get_stat("fetch:round_seconds", 0))
    try:
        last_run_ts = float(cc.get_stat("fetch:last_run_ts", 0) or 0)
    except (TypeError, ValueError):
        last_run_ts = 0.0
    set_gauge(fetch_stalled,
              max(0.0, time.time() - last_run_ts) if last_run_ts else 0.0)
    set_gauge(check_total, cc.get_stat("check:total", 0))
    set_gauge(check_pass_rate, cc.get_stat("check:pass_rate", 0))
    set_gauge(avg_speed, cc.get_stat("pool:avg_speed", 0))
    set_gauge(fetch_round, cc.get_stat("fetch:round_seconds", 0))


__all__ = [
    "gateway_requests", "gateway_errors", "gateway_served",
    "gateway_rate_limited", "gateway_reports",
    "fetch_total", "fetch_round", "fetch_stalled",
    "fetcher_total", "fetcher_enabled", "fetcher_down",
    "check_total", "check_pass_rate", "avg_speed",
    "pool_total", "pool_https", "blacklist_total", "queue_depth",
    "redis_up", "redis_memory",
    "set_gauge", "inc_counter", "update_metrics",
]