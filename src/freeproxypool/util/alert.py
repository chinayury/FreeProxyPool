# -*- coding: utf-8 -*-
"""告警：日志 + Redis 事件 + 可选 Webhook 推送，带冷却去重。

支持四类内置告警：

==========================  ============================================
名称                         触发条件
==========================  ============================================
``pool_low``                可用代理数低于 ``POOL_LOW_THRESHOLD``
``redis_down``              Redis 心跳失败
``fetcher_down``            熔断源数过多
``valid_rate_drop``         有效率较上次下降超过 50%
``queue_backlog``           待验队列积压超过上限的 80%
``scheduler_error``         调度任务抛出未捕获异常
==========================  ============================================
"""
import json
import threading
import time
import urllib.error
import urllib.request
from typing import List

from .. import config_center as cc
from ..core.logging import get_logger

log = get_logger("alert", to_file=False)

ALERT_KEY = "fpp:alerts"
_COOLDOWN_KEY = "fpp:alert:cooldown:%s"
_send_lock = threading.Lock()

#: 钉钉 / 企业微信机器人都接受 {"msgtype":"text","text":{"content": "..."}}
_WEBHOOK_TIMEOUT = 5


def send(name: str, message: str, level: str = "warning") -> bool:
    """发送一条告警；冷却期内直接跳过。返回是否真正发出。"""
    cooldown = int(cc.get("ALERT_COOLDOWN", 600))
    if _in_cooldown(name, cooldown):
        return False
    with _send_lock:
        # 双检：多线程同时触发时只放行一条
        if _in_cooldown(name, cooldown):
            return False
        _mark_sent(name)

    line = "[%s] %s: %s" % (level.upper(), name, message)
    log.warning("ALERT %s", line)

    try:
        conn = cc.get_redis()
        pipe = conn.pipeline()
        pipe.lpush(ALERT_KEY, "%s | %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
        pipe.ltrim(ALERT_KEY, 0, 499)
        pipe.execute()
    except Exception:
        # 告警是观测通道，写不进去也不能影响主流程
        pass

    _push_webhook(line)
    return True


def _in_cooldown(name: str, cooldown: int) -> bool:
    try:
        last = cc.get_redis().get(_COOLDOWN_KEY % name)
        return bool(last) and (time.time() - float(last)) < cooldown
    except Exception:
        # Redis 不可用时**不因为冷却而丢告警**，宁可刷屏也要让人看见
        return False


def _mark_sent(name: str) -> None:
    try:
        cc.get_redis().set(_COOLDOWN_KEY % name, time.time(), ex=86400)
    except Exception:
        pass


def _push_webhook(line: str) -> None:
    webhook = str(cc.get("ALERT_WEBHOOK", "") or "").strip()
    if not webhook:
        return

    def _do():
        payload = json.dumps({"msgtype": "text", "text": {"content": line}}).encode("utf-8")
        request = urllib.request.Request(
            webhook, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=_WEBHOOK_TIMEOUT):
                pass
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.error("Webhook 推送失败：%s", type(exc).__name__)

    threading.Thread(target=_do, name="fpp-alert-webhook", daemon=True).start()


def read_alerts(limit: int = 100) -> List[str]:
    """读取最近的告警事件。"""
    try:
        return cc.get_redis().lrange(ALERT_KEY, 0, max(0, limit - 1)) or []
    except Exception:
        return []


def clear_alerts() -> None:
    try:
        cc.get_redis().delete(ALERT_KEY)
    except Exception:
        pass


__all__ = ["send", "read_alerts", "clear_alerts", "ALERT_KEY"]