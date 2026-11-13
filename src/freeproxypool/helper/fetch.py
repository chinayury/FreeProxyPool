# -*- coding: utf-8 -*-
"""采集编排：并发扫描采集源，过滤 + 去重后产出待验代理。

去重分三层（与旧版一致，但语义更严格）：

1. **内存去重** —— 本轮已采到的，直接累加来源；
2. **有效池去重** —— 已在池子里的跳过；
3. **黑名单去重** —— 近期刚被拉黑的跳过。

第 2/3 层在 Redis 异常时**放行**（记 warning），宁可多验一次也不要因为
存储抖动就丢掉整轮采集结果；真正的丢弃由后续校验层兜底。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from .. import config_center as cc
from ..core.iputil import is_internal_proxy, is_valid_proxy, strip_proxy_prefix
from ..core.logging import get_logger
from ..fetcher import baseFetcher as breaker
from ..fetcher.registry import discover
from .proxy import Proxy, normalize_protocol

log = get_logger("fetch", to_file=False)


def enabled_fetchers() -> List[type]:
    """返回当前启用的采集源类。"""
    result = []
    for cls in discover():
        if breaker.is_tripped(cls.name):
            continue
        try:
            enabled = cc.is_fetcher_enabled(cls.name, default=getattr(cls, "enabled", True))
        except Exception:
            enabled = getattr(cls, "enabled", True)
        if enabled:
            result.append(cls)
    return result


def _collect_one(cls: type, pool: Dict[str, Proxy], lock: threading.Lock,
                 store, filter_internal: bool, score_init: int) -> int:
    """执行单个源的采集，把结果并入共享 pool。返回有效条数。

    v0.1.0（P0-1）：产出代理带上**传输协议**（取自 ``cls.protocol``）。
    同一 ``ip:port`` 若同时出现在 HTTP 源与 SOCKS 源里，**先到先得** ——
    先采集到的源决定它的协议，后者只追加来源名。同一台机器上的同一个
    端口，理论上不可能既是 HTTP 代理又是 SOCKS 代理；而真正需要区分时
    可用 ``protocol=`` 参数按协议取，两者同时存在也不会互相污染。
    """
    count = 0
    protocol = normalize_protocol(getattr(cls, "protocol", "http"))
    try:
        for raw in cls().fetch():
            proxy_str = strip_proxy_prefix(raw)
            if not is_valid_proxy(proxy_str):
                continue
            if filter_internal and is_internal_proxy(proxy_str):
                continue
            count += 1
            with lock:
                existing = pool.get(proxy_str)
                if existing is not None:
                    existing.add_source(cls.name)
                    continue
                if _already_known(store, proxy_str):
                    continue
                pool[proxy_str] = Proxy(
                    proxy=proxy_str, source=[cls.name], score=score_init,
                    protocol=protocol)
    except Exception as exc:
        log.error("采集源 %s 执行异常：%s", cls.name, type(exc).__name__)
        # 异常会中断 fetch() 生成器，其末尾的 mark_success/mark_failure 不会
        # 执行，熔断计数因此漏记 —— "只会抛异常的源"永远攒不到 FETCHER_MAX_FAIL，
        # 每轮都要白等一次超时。已产出数据的算部分成功，只对零产出记失败。
        if count == 0:
            try:
                breaker.mark_failure(cls.name)
            except Exception:
                pass
    return count


def _already_known(store, proxy_str: str) -> bool:
    """检查是否已在有效池中，或仍处于死代理退避窗口内。存储异常时放行。

    v0.1.0（P0-2）：这是死代理退避**省算力的主杠杆**。
    改造前黑名单条目只存活固定 TTL（默认 1 小时），一个确认死亡的代理
    每小时就会被重新采集、重新入队、重新校验一次 —— 永远循环。
    现在"仍在退避窗口内"的代理会被跳过采集，窗口随连续失败次数
    递增（1h → 2h → 6h → 24h），直到校验通过才被重新放行。
    """
    try:
        if store.exists(proxy_str) or store.is_black(proxy_str):
            return True
    except Exception as exc:
        log.warning("去重检查异常，放行 %s（%s）", proxy_str, type(exc).__name__)
    return False


def run_once(store=None, on_progress=None) -> List[dict]:
    """执行一轮采集。

    :param store: 存储实例（省略则取默认单例）
    :param on_progress: 可选进度回调 ``(done_sources, total_sources)``，
        每完成一个源调用一次 —— 一轮采集实测约 100 秒，没有进度用户只能干等。
    :return: ``[{"proxy": "ip:port", "source": "srcA/srcB", "protocol": "http"}, ...]``

    v0.1.0 变更：此前返回的是裸 ``["ip:port", ...]``，
    **采集来源在入队时就丢失了**，导致池内代理 ``source`` 恒为空、
    无法评估各采集源的贡献。现改为携带来源的字典。

    v0.1.0（P0-1）：再带上 ``protocol``（传输协议），供队列载荷透传。
    """
    from ..db.dbClient import get_store

    store = store or get_store()
    classes = enabled_fetchers()
    if not classes:
        log.info("没有启用中的采集源，跳过本轮")
        return []

    filter_internal = bool(cc.get("FILTER_INTERNAL", True))
    score_init = int(cc.get("PROXY_SCORE_INIT", 0))
    concurrency = max(1, int(cc.get("FETCHER_CONCURRENCY", 12)))

    pool: Dict[str, Proxy] = {}
    lock = threading.Lock()
    counts: Dict[str, int] = {}
    done = {"n": 0}

    log.info("开始采集，启用源 %d 个，并发 %d", len(classes), concurrency)

    def _task(cls):
        value = _collect_one(cls, pool, lock, store, filter_internal, score_init)
        counts[cls.name] = value
        with lock:
            done["n"] += 1
            completed = done["n"]
        if on_progress is not None:
            try:
                on_progress(completed, len(classes))
            except Exception:
                # 进度回调异常不能影响采集主流程
                pass

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="fpp-fetch") as pool_executor:
        list(pool_executor.map(_task, classes))

    # 结算每个源的运行统计与熔断状态
    for cls in classes:
        count = counts.get(cls.name, 0)
        tripped = breaker.breaker_status(cls.name)
        cc.record_fetcher_stat(
            cls.name, count,
            trip_until=breaker.trip_until_text(cls.name) if tripped.get("until", 0) > time.time() else "",
        )

    result = [{"proxy": proxy, "source": obj.source, "protocol": obj.protocol}
              for proxy, obj in sorted(pool.items())]
    log.info("采集完成，本轮去重后 %d 条", len(result))
    return result


__all__ = ["run_once", "enabled_fetchers"]