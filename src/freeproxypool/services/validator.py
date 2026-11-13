# -*- coding: utf-8 -*-
"""验证服务。

从待验队列批量消费 → 三层验证（TCP / 代理功能 / 多轮复检）→ 写入有效池。
所有统计（``check:last_run`` / ``check:pass_rate`` / ``check:total``）都由本服务
产出 —— 旧版这些统计由旧的 ``helper/scheduler.py`` 写入，而容器里根本不跑那个
进程，导致管理后台"校验"面板长期显示为空。
"""
import random
import signal
import time

from .. import config_center as cc
from ..core.logging import attach_redis_sink, get_logger, set_log_level
from ..core.version import VERSION
from ..db.queue import ProxyQueue
from ..helper.check import check

log = get_logger("validator")

#: 单批消费条数
BATCH_SIZE = 200
#: 队列为空时的休眠秒数
IDLE_SLEEP = 3

_running = True

#: 当前"抽干待验队列"这一轮的进度状态（P1-3）
#: total = 本轮开始时的队列深度；checked = 本轮已校验条数
_cycle = {"total": 0, "checked": 0}


def _stop(_signum=None, _frame=None):
    global _running
    _running = False
    log.info("验证服务收到退出信号，准备停止")


def validate_batch(queue: ProxyQueue = None) -> dict:
    """消费一批待验代理并完成校验，返回统计。

    v0.1.0（P1-3）：写进度记录 —— 线上待验队列曾积压 4.5 万条，
    "还剩多少要验"是值班最想知道的事。
    """
    from ..helper import progress

    queue = queue or ProxyQueue()

    if not _cycle["total"]:
        # 新一轮"抽干队列"：先把起始深度记为总量
        try:
            depth = queue.depth()
        except Exception:
            depth = 0
        _cycle["total"] = depth
        _cycle["checked"] = 0
        if depth:
            progress.start("check", depth, "正在校验待验队列")

    items = queue.pop_batch(BATCH_SIZE)
    if not items:
        if _cycle["total"]:
            progress.finish("check", checked=_cycle["checked"],
                            total=_cycle["total"], message="待验队列已抽干")
            _cycle["total"] = 0
            _cycle["checked"] = 0
        return {"checked": 0, "passed": 0, "failed": 0, "added": 0, "removed": 0}

    # v0.1.0：载荷携带采集来源，兼容旧的裸 ip:port 条目
    from ..db.queue import decode
    stats = check([decode(item) for item in items], work_type="raw")
    # checked 已含全部条目（helper.check._run 维持 passed + failed == checked），
    # 直接用它做分母；旧代码再 + failed 会把失败条数重复计入、稀释通过率
    checked = max(1, stats.get("checked", 0))
    pass_rate = round(stats.get("passed", 0) / checked, 4) if checked else 0.0

    cc.mark_stat("check:last_total", stats.get("passed", 0))
    cc.mark_stat("check:last_run")
    cc.mark_stat("check:pass_rate", pass_rate)
    cc.mark_stat("check:total", int(cc.get_stat("check:total", 0)) + checked)

    _cycle["checked"] += len(items)
    if not _cycle["total"]:
        # 起始深度没测到（并发下队列可能被别的进程抽空），至少保证总量不小于已校验数
        _cycle["total"] = _cycle["checked"]
    progress.update("check", checked=_cycle["checked"], total=_cycle["total"],
                    passed=stats.get("passed", 0), failed=stats.get("failed", 0),
                    message="已校验 %d/%d 条" % (_cycle["checked"], _cycle["total"]))

    log.info("本批验证完成：消费 %d 条，通过 %d 条，入池 %d 条，通过率 %.1f%%",
             len(items), stats.get("passed", 0), stats.get("added", 0), pass_rate * 100)
    return stats


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    set_log_level(cc.get("LOG_LEVEL", "INFO"))
    attach_redis_sink(cc.KEY_LOGS, max_logs=2000)
    log.info("FreeProxyPool 验证服务 v%s 已启动", VERSION)

    queue = ProxyQueue()
    while _running:
        try:
            stats = validate_batch(queue)
            if not stats.get("checked"):
                time.sleep(IDLE_SLEEP)
        except Exception as exc:
            log.error("验证轮次异常：%s", exc, exc_info=True)
            time.sleep(IDLE_SLEEP)

    log.info("验证服务已退出")


if __name__ == "__main__":
    main()