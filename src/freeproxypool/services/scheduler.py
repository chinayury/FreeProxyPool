# -*- coding: utf-8 -*-
"""调度服务（APScheduler）。

注册四类周期任务，并每 5 秒轮询一次手动触发标记：

====================  ==========================================
任务                    职责
====================  ==========================================
``recheck``           存量复检：把有效池里的代理重新验一遍，淘汰失效项
``cleanup``           数据清理：回收过期代理 + 清理过期黑名单 + 裁剪日志
``stats``             统计告警：写指标、触发阈值告警、刷新 Prometheus Gauge
``trigger_watch``     响应管理后台的"立即采集 / 立即校验"
====================  ==========================================

所有任务内部均 try/except 兜底，单个任务异常不会拖垮调度器。
"""
import signal
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .. import config_center as cc
from ..core.logging import attach_redis_sink, get_logger, set_log_level
from ..core.version import VERSION
from ..db.dbClient import get_store
from ..db.queue import ProxyQueue
from ..fetcher import baseFetcher as breaker
from ..util.alert import send

log = get_logger("scheduler")

_running = True

#: 支持热更新间隔的周期任务：job_id -> (配置键, 单位换算秒数)
_RESCHEDULABLE = {
    "recheck": ("CHECK_INTERVAL", 60),
    "cleanup": ("CLEAN_INTERVAL", 1),
    "stats": ("STAT_INTERVAL", 1),
}

#: 各任务当前生效的间隔（秒），用于识别配置热更新
_active_intervals: dict = {}

#: 调度器实例引用（供 task_reschedule 调整间隔）
_scheduler = None

#: 地域索引上次**全量重建**的时间戳（进程内）
_region_index_built_at = 0.0


def _ensure_region_index(store, force: bool = False) -> None:
    """确保地域索引存在且与代理池一致（借鉴 docs/09 P1-1）。

    地域索引是新增的，**升级前入库的数据没有索引条目**。进程启动后按当前
    Hash 重建一次即可，之后由 :meth:`RedisStore.put` / :meth:`RedisStore.delete`
    增量维护 —— 不必每轮都重建（那等于把全表扫描又搬回后台）。

    但**周期性重建一次**仍然必要：滚动发布期间可能还有旧版本进程在往池子
    里写，旧版本不会维护索引 —— 索引就会悄悄落后于真实数据，
    ``?area=`` 于是少返回代理。重建是幂等的，周期性地跑一次能让索引收敛。

    重建失败（Redis 不可用等）不抛异常 —— 重建只是优化，失败就退回
    全表扫描，结果依然正确。
    """
    global _region_index_built_at
    if not force:
        try:
            interval = int(cc.get("REGION_INDEX_REBUILD_INTERVAL", 600))
        except (TypeError, ValueError):
            interval = 600
        if _region_index_built_at and (time.time() - _region_index_built_at) < interval:
            return
    try:
        count = store.rebuild_region_index()
        _region_index_built_at = time.time()
        log.info("地域索引已重建，索引代理 %d 条", count)
    except Exception as exc:
        log.warning("地域索引重建失败（可忽略）：%s", type(exc).__name__)


def _stop(_signum=None, _frame=None):
    global _running
    _running = False
    log.info("调度服务收到退出信号，准备停止")


# ================= 周期任务 =================

def task_recheck() -> None:
    """存量复检：对有效池做 use 校验。

    v0.1.0（P1-3）：写进度记录 —— 存量复检动辄几万条，
    没有进度用户就只能对着一个"转圈"图标干等。
    """
    from ..helper import progress
    from ..helper.check import check
    from ..helper.proxy import Proxy

    store = get_store()
    items = store.get_all()
    if not items:
        log.info("存量复检：有效池为空，跳过")
        return
    total = len(items)
    progress.start("recheck", total, "存量复检")

    def _on_progress(checked, _total):
        progress.update("recheck", checked=checked, total=total)

    try:
        stats = check([Proxy.from_dict(item) for item in items],
                      work_type="use", on_progress=_on_progress)
    except Exception as exc:
        progress.finish("recheck", status=progress.STATUS_FAILED,
                        checked=0, total=total,
                        message="存量复检失败：%s" % type(exc).__name__)
        raise
    progress.finish("recheck", checked=stats.get("checked", total),
                    passed=stats.get("passed", 0), failed=stats.get("failed", 0),
                    message="存量复检完成：淘汰 %d 条" % stats.get("removed", 0))
    cc.mark_stat("recheck:last_run")
    log.info("存量复检完成：校验 %d 条，淘汰 %d 条",
             stats.get("checked", 0), stats.get("removed", 0))


def task_cleanup() -> None:
    """数据清理：回收过期代理 + 清理过期黑名单 + 裁剪日志。"""
    store = get_store()
    _ensure_region_index(store)
    evicted = 0
    try:
        while True:
            batch = store.evict_expired(500)
            evicted += batch
            if batch < 500:
                break
        if evicted:
            log.info("回收过期代理 %d 条", evicted)
    except Exception as exc:
        log.error("回收过期代理失败：%s", type(exc).__name__)

    try:
        blacklist = store.black_count()
        cc.mark_stat("pool:blacklist", blacklist)
    except Exception as exc:
        log.error("黑名单清理失败：%s", type(exc).__name__)

    # P0-2: 回收已不在黑名单里的连续失败计数。
    # 计数记在独立 Hash（代理移出池后仍需保留以维持退避档位），
    # 不显式清理会随永久死亡的代理无限增长。
    try:
        stale = store.prune_black_fail()
        if stale:
            log.info("回收失效失败计数 %d 条", stale)
    except Exception as exc:
        log.error("失败计数回收失败：%s", type(exc).__name__)

    try:
        from ..log_sink import MAX_LOGS, LOG_KEY
        cc.get_redis().ltrim(LOG_KEY, 0, MAX_LOGS - 1)
    except Exception:
        pass


def task_stats() -> None:
    """统计与告警：写指标、刷新 Prometheus Gauge、触发阈值告警。"""
    from .. import metrics

    store = get_store()
    count = store.get_count()
    total = count["total"]
    https = count["https"]

    try:
        blacklist = store.black_count()
    except Exception:
        blacklist = 0

    # 有效率 = 有效池 /（有效池 + 黑名单），近似反映整体健康度
    denominator = total + blacklist
    valid_rate = (total / denominator) if denominator else 0.0

    avg_speed = _avg_speed(store)
    depth = ProxyQueue().depth()

    cc.mark_stat("pool:total", total)
    cc.mark_stat("pool:https", https)
    cc.mark_stat("pool:blacklist", blacklist)
    cc.mark_stat("pool:valid_rate", round(valid_rate, 4))
    cc.mark_stat("pool:avg_speed", round(avg_speed, 4))

    try:
        online = bool(cc.get_redis().ping())
    except Exception:
        online = False
    cc.mark_stat("redis:up", 1 if online else 0)

    try:
        metrics.update_metrics()
    except Exception as exc:
        log.error("刷新指标失败：%s", type(exc).__name__)

    _raise_alerts(total, valid_rate, online, depth)


def _avg_speed(store) -> float:
    """有效池平均响应延迟（秒）。"""
    try:
        speeds = [float(item.get("speed") or 0) for item in store.get_all()]
        speeds = [s for s in speeds if s > 0]
        return sum(speeds) / len(speeds) if speeds else 0.0
    except Exception:
        return 0.0


def _raise_alerts(total: int, valid_rate: float, redis_up: bool, depth: int) -> None:
    """触发阈值告警。"""
    threshold = int(cc.get("POOL_LOW_THRESHOLD", 50))
    if total < threshold:
        send("pool_low", "可用代理仅 %d 个，低于阈值 %d" % (total, threshold))

    if not redis_up:
        send("redis_down", "Redis 连接异常，服务可能不可用")
        return  # Redis 挂了就别再查采集源了

    # 有效率暴跌（较上次下降超过 50%）
    previous = cc.get_stat_float("pool:last_valid_rate", 0)
    if previous > 0 and valid_rate < previous * 0.5:
        send("valid_rate_drop",
             "代理有效率从 %.1f%% 跌至 %.1f%%" % (previous * 100, valid_rate * 100))
    cc.mark_stat("pool:last_valid_rate", round(valid_rate, 4))

    # 队列积压
    max_len = int(cc.get("QUEUE_MAX_LEN", 50000))
    if depth > max_len * 0.8:
        send("queue_backlog",
             "待验队列积压 %d 条，超过上限的 80%%（%d）" % (depth, max_len))

    # 大面积熔断
    try:
        fetchers = cc.list_fetchers()
        enabled = [f for f in fetchers if f.get("enabled")]
        down = [f["name"] for f in enabled if breaker.is_tripped(f["name"])]
        if enabled and (len(down) >= 5 or len(down) >= (len(enabled) + 1) // 2):
            send("fetcher_down", "%d 个采集源已熔断（启用源 %d 个）"
                 % (len(down), len(enabled)))
        cc.mark_stat("fetcher:down", len(down))
    except Exception:
        pass


# ================= 手动触发监听 =================

def task_trigger_watch() -> None:
    """响应管理后台的"立即校验"。

    v0.1.0 修复：旧版 ``cc.trigger()`` 只写 Redis 标记，**没有任何消费者**，
    按钮点了毫无反应。

    注意分工：**只消费 check 标记**。"立即采集"由 collector 进程自己轮询消费
    （见 ``services/collector.sleep_until_next``）。两边都消费 fetch 标记会互相
    抢消息，导致一半请求被空消费掉 —— 这是 v0.1.0 刻意划清的边界。
    """
    if not cc.consume_trigger("check"):
        return
    log.info("收到手动校验触发")
    try:
        task_recheck()
    except Exception as exc:
        log.error("手动校验失败：%s", exc, exc_info=True)


def _configured_interval(job_id: str) -> int:
    """读取某任务当前配置的间隔（秒）；配置非法返回 0 表示保持现状。"""
    key, unit = _RESCHEDULABLE[job_id]
    try:
        return max(1, int(cc.get(key, 0))) * unit
    except (TypeError, ValueError):
        return 0


def task_reschedule() -> None:
    """把周期任务间隔同步为当前配置（配置热更新）。

    v0.1.0 正式版修复：``build_scheduler`` 只在进程启动时读一次
    ``CHECK_INTERVAL`` / ``CLEAN_INTERVAL`` / ``STAT_INTERVAL``，
    管理后台改这三个值后**必须重启才生效**，与"非重启项热更新"的承诺不符
    （旧版 ``CHECK_INTERVAL`` 完全失效属同一类问题：配置读了却不用）。
    现在每 30 秒比对一次，不一致就 ``reschedule_job``。
    """
    scheduler = _scheduler
    if scheduler is None:
        return
    for job_id in _RESCHEDULABLE:
        seconds = _configured_interval(job_id)
        if seconds <= 0 or _active_intervals.get(job_id) == seconds:
            continue
        try:
            scheduler.reschedule_job(job_id, trigger=IntervalTrigger(seconds=seconds))
            _active_intervals[job_id] = seconds
            log.info("配置 %s 已热更新，任务 %s 间隔调整为 %d 秒",
                     _RESCHEDULABLE[job_id][0], job_id, seconds)
        except Exception as exc:
            log.warning("调整任务 %s 间隔失败：%s", job_id, type(exc).__name__)


# ================= 启动 =================

def build_scheduler() -> BackgroundScheduler:
    """构建并注册全部周期任务。"""
    scheduler = BackgroundScheduler(timezone=cc.get("TIMEZONE", "Asia/Shanghai"))
    scheduler.add_job(
        task_recheck, IntervalTrigger(minutes=int(cc.get("CHECK_INTERVAL", 5))),
        id="recheck", name="存量复检",
        coalesce=True, max_instances=1, misfire_grace_time=300)
    scheduler.add_job(
        task_cleanup,
        IntervalTrigger(seconds=int(cc.get("CLEAN_INTERVAL", 600))),
        id="cleanup", name="数据清理",
        coalesce=True, max_instances=1, misfire_grace_time=300)
    scheduler.add_job(
        task_stats, IntervalTrigger(seconds=int(cc.get("STAT_INTERVAL", 60))),
        id="stats", name="统计告警",
        coalesce=True, max_instances=1, misfire_grace_time=60)
    scheduler.add_job(
        task_trigger_watch, IntervalTrigger(seconds=5),
        id="trigger_watch", name="手动校验监听",
        coalesce=True, max_instances=1, misfire_grace_time=10)
    scheduler.add_job(
        task_reschedule, IntervalTrigger(seconds=30),
        id="reschedule_watch", name="配置热更新监听",
        coalesce=True, max_instances=1, misfire_grace_time=10)
    for job_id in _RESCHEDULABLE:
        _active_intervals[job_id] = _configured_interval(job_id)
    return scheduler


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    set_log_level(cc.get("LOG_LEVEL", "INFO"))
    attach_redis_sink(cc.KEY_LOGS, max_logs=2000)
    log.info("FreeProxyPool 调度服务 v%s 已启动", VERSION)

    _init_mysql_archive()

    # 启动即重建一次地域索引：升级前入库的数据没有索引条目，
    # 重建完成前读侧会退回全表扫描（结果正确，只是慢）。
    try:
        _ensure_region_index(get_store(), force=True)
    except Exception as exc:
        log.warning("地域索引初始化失败（可忽略）：%s", type(exc).__name__)

    global _scheduler
    scheduler = build_scheduler()
    _scheduler = scheduler
    scheduler.start()
    log.info("已注册任务：%s", ", ".join(
        "%s=%s" % (job.id, job.trigger) for job in scheduler.get_jobs()))

    while _running:
        time.sleep(1)

    scheduler.shutdown(wait=False)
    log.info("调度服务已退出")


def _init_mysql_archive() -> None:
    """初始化 MySQL 冷存储表（未启用或失败均不影响实时业务）。"""
    try:
        from ..storage.mysql_client import archive_enabled, ensure_tables
        if archive_enabled():
            ensure_tables()
            log.info("MySQL 冷存储归档已启用")
        else:
            log.info("MySQL 冷存储归档未启用（MYSQL_ENABLED=false）")
    except Exception as exc:
        log.warning("MySQL 归档初始化失败（可忽略）：%s", type(exc).__name__)


if __name__ == "__main__":
    main()