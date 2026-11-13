# -*- coding: utf-8 -*-
"""采集服务。

循环：扫描采集源 → 过滤去重 → 推入待验队列。
支持 ``FETCHER_PAUSED`` 暂停开关与调度器下发的"立即采集"触发。
"""
import random
import signal
import time

from .. import config_center as cc
from ..core.logging import attach_redis_sink, get_logger, set_log_level
from ..core.version import VERSION
from ..db.queue import ProxyQueue
from ..helper.fetch import run_once

log = get_logger("collector")

_running = True


def _stop(_signum=None, _frame=None):
    global _running
    _running = False
    log.info("采集服务收到退出信号，准备停止")


def collect_once(queue: ProxyQueue = None) -> int:
    """执行一轮采集并入队，返回入队条数。

    v0.1.0（P1-3）：写进度记录 —— 一轮采集实测约 100 秒，
    管理后台点完"立即采集"看不到任何反馈。
    """
    from ..helper import progress

    started = time.perf_counter()
    state = {"total": 0}

    def _on_progress(done, total):
        state["total"] = total
        progress.update("fetch", checked=done, total=total,
                        message="已扫描 %d/%d 个采集源" % (done, total))

    progress.start("fetch", 0, "正在采集")
    try:
        proxies = run_once(on_progress=_on_progress)  # [{"proxy":..., "source":[...], "protocol":...}, ...]
    except Exception as exc:
        progress.finish("fetch", status=progress.STATUS_FAILED,
                        checked=state["total"], total=state["total"],
                        message="采集失败：%s" % type(exc).__name__)
        raise
    queue = queue or ProxyQueue()
    pushed = queue.push_many(proxies) if proxies else 0

    elapsed = round(time.perf_counter() - started, 2)
    cc.record_collector_stat(pushed)
    cc.mark_stat("fetch:last_total", pushed)
    cc.mark_stat("fetch:last_run")
    # 供 Prometheus 计算"距上次采集完成多少秒"（告警 FppFetchStalled 用）。
    # 必须存 unix 时间戳：存格式化的 datetime 字符串没法参与 PromQL 运算，
    # 而存"本轮耗时"会被误当成时间戳，导致 time() - 耗时 恒为当前时间、告警恒真。
    cc.mark_stat("fetch:last_run_ts", int(time.time()))
    cc.mark_stat("fetch:total", int(cc.get_stat("fetch:total", 0)) + pushed)
    cc.mark_stat("fetch:round_seconds", elapsed)
    progress.finish("fetch", checked=state["total"], total=state["total"],
                    message="本轮采集完成：去重后 %d 条，入队 %d 条"
                            % (len(proxies), pushed))

    log.info("本轮采集完成：去重后 %d 条，入队 %d 条，耗时 %.2fs", len(proxies), pushed, elapsed)
    if proxies and pushed == 0:
        log.warning("采集到 %d 条但全部被去重集合拦截（%ds 内已推过）",
                    len(proxies), int(cc.get("QUEUE_DEDUP_TTL", 300)))
    return pushed


def sleep_until_next(interval_minutes: int) -> None:
    """可被中断的睡眠（每秒轮询一次手动触发标记）。"""
    deadline = time.time() + max(1, interval_minutes) * 60
    while _running and time.time() < deadline:
        if cc.consume_trigger("fetch"):
            log.info("收到手动采集触发")
            return
        time.sleep(1)


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    set_log_level(cc.get("LOG_LEVEL", "INFO"))
    attach_redis_sink(cc.KEY_LOGS, max_logs=2000)
    log.info("FreeProxyPool 采集服务 v%s 已启动", VERSION)

    queue = ProxyQueue()
    # 启动即先跑一轮，避免冷启动时池子是空的
    while _running:
        try:
            if cc.get("FETCHER_PAUSED", False):
                log.info("采集已暂停，等待恢复")
                time.sleep(30)
                continue
            collect_once(queue)
        except Exception as exc:
            log.error("采集轮次异常：%s", exc, exc_info=True)
            try:
                from ..util.alert import send
                send("collector_error", "采集轮次异常：%s" % type(exc).__name__)
            except Exception:
                pass
            time.sleep(30)
            continue
        sleep_until_next(int(cc.get("FETCH_INTERVAL", 5)))

    log.info("采集服务已退出")


if __name__ == "__main__":
    main()