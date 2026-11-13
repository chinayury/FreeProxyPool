# -*- coding: utf-8 -*-
"""长任务进度反馈（借鉴 docs/09 P1-3）。

**问题**：管理后台的"立即采集 / 立即校验"点下去之后，用户**看不到任何进度**。
只能干等，或者反复刷 ``/admin/api/process/status`` 去猜。
而一轮采集实测约 107 秒，存量复检 4.5 万条要更久 —— 这种"黑盒等待"体验很差。

**做法**：维护一个进度对象（``status`` / ``started_at`` / ``finished_at`` /
``checked`` / ``total``），进度值派生 ``progress_percent`` 与 ``elapsed_seconds``，
由 ``/admin/api/process/status`` 返回，前端概览页画进度条。

三个关键取舍：

1. **Redis 存一份，不进内存** —— 写进度的是采集/验证/调度进程，
   读进度的是网关进程，**不同容器**。放内存里网关永远读不到。
2. **轮询而非 SSE** —— SSE 要处理连接断开、重连、心跳，
   而管理后台本来就在轮询 ``/admin/api/process/status``，加个字段成本最低。
3. **心跳判定"已中断"** —— 进程崩了进度会永远停在 ``running``。
   因此每写一次都刷新 ``heartbeat_at``，读取时若心跳超时则标记为
   ``stale``（已中断），而不是让界面一直转圈骗用户。

进度对象按 **processor** 分开记（``fetch`` / ``check`` / ``recheck``），
同一时刻三类任务可以各跑各的，互不覆盖。
"""
import json
import time

from .. import config_center as cc

#: 进度 Hash：field = processor 名，value = JSON
PROGRESS_KEY = "fpp:progress"

#: 心跳超时（秒）：超过这个时间没有更新，任务视为已中断
HEARTBEAT_TIMEOUT = 120

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
#: 心跳超时后读取时给出的状态（不写回 Redis，只在读取时派生）
STATUS_STALE = "stale"

#: 允许记录进度的任务名
PROCESSORS = ("fetch", "check", "recheck")


def _now() -> float:
    return time.time()


def _write(processor: str, data: dict) -> None:
    """写入一条进度记录（Redis 异常时静默 —— 进度只是观测，不该拖垮业务）。"""
    try:
        cc.get_redis().hset(PROGRESS_KEY, processor,
                             json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def _read_raw(processor: str) -> dict:
    try:
        raw = cc.get_redis().hget(PROGRESS_KEY, processor)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def start(processor: str, total: int = 0, message: str = "") -> None:
    """标记一个长任务开始。

    :param processor: ``fetch`` / ``check`` / ``recheck``
    :param total: 本次任务的总条数（0 表示未知）
    :param message: 展示用的一句话说明
    """
    now = _now()
    _write(processor, {
        "processor": processor,
        "status": STATUS_RUNNING,
        "started_at": now,
        "finished_at": 0,
        "heartbeat_at": now,
        "total": int(total or 0),
        "checked": 0,
        "passed": 0,
        "failed": 0,
        "message": message,
    })


def update(processor: str, checked: int = None, total: int = None,
           passed: int = None, failed: int = None, message: str = None) -> None:
    """更新进度。

    只覆盖显式传入的字段，其余保持原值 —— 这样"只更新 checked"的调用方
    不会把 ``passed`` / ``message`` 抹成默认值。
    """
    data = _read_raw(processor)
    if not data:
        data = {"processor": processor, "status": STATUS_RUNNING,
                "started_at": _now(), "finished_at": 0,
                "total": 0, "checked": 0, "passed": 0, "failed": 0,
                "message": ""}
    if checked is not None:
        data["checked"] = int(checked)
    if total is not None:
        data["total"] = int(total)
    if passed is not None:
        data["passed"] = int(passed)
    if failed is not None:
        data["failed"] = int(failed)
    if message is not None:
        data["message"] = str(message)
    data["status"] = STATUS_RUNNING
    data["heartbeat_at"] = _now()
    _write(processor, data)


def finish(processor: str, status: str = STATUS_DONE,
           checked: int = None, total: int = None,
           passed: int = None, failed: int = None,
           message: str = None) -> None:
    """标记任务结束。"""
    data = _read_raw(processor)
    if not data:
        data = {"processor": processor, "started_at": _now(),
                "total": 0, "checked": 0, "passed": 0, "failed": 0,
                "message": ""}
    if checked is not None:
        data["checked"] = int(checked)
    if total is not None:
        data["total"] = int(total)
    if passed is not None:
        data["passed"] = int(passed)
    if failed is not None:
        data["failed"] = int(failed)
    if message is not None:
        data["message"] = str(message)
    data["status"] = status
    data["finished_at"] = _now()
    data["heartbeat_at"] = data["finished_at"]
    _write(processor, data)


def clear(processor: str) -> None:
    """清除某类任务的进度记录。"""
    try:
        cc.get_redis().hdel(PROGRESS_KEY, processor)
    except Exception:
        pass


def _derive(data: dict) -> dict:
    """派生展示字段：百分比、耗时、是否已中断。"""
    if not data:
        return {}
    result = dict(data)
    checked = int(result.get("checked") or 0)
    total = int(result.get("total") or 0)
    if total > 0:
        percent = round(min(100.0, checked * 100.0 / total), 1)
    elif result.get("status") == STATUS_DONE:
        percent = 100.0
    else:
        percent = 0.0
    result["progress_percent"] = percent

    started = float(result.get("started_at") or 0)
    finished = float(result.get("finished_at") or 0)
    if started:
        result["elapsed_seconds"] = round((finished or _now()) - started, 1)
    else:
        result["elapsed_seconds"] = 0.0

    # 心跳超时 ⇒ 进程崩了/卡死了，界面不该一直转圈
    heartbeat = float(result.get("heartbeat_at") or 0)
    stale = (result.get("status") == STATUS_RUNNING and heartbeat
             and (_now() - heartbeat) > HEARTBEAT_TIMEOUT)
    if stale:
        result["status"] = STATUS_STALE
    result["stale"] = bool(stale)
    return result


def read(processor: str = None) -> dict:
    """读取进度。

    :param processor: 指定任务名时返回该任务的进度字典；
                      省略时返回**全部**任务名的进度字典（``{processor: {...}}``）。
    """
    if processor:
        return _derive(_read_raw(processor))
    try:
        raw = cc.get_redis().hgetall(PROGRESS_KEY) or {}
    except Exception:
        raw = {}
    result = {}
    for name in PROCESSORS:
        item = raw.get(name)
        if not item:
            continue
        try:
            data = json.loads(item)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            result[name] = _derive(data)
    return result


__all__ = [
    "PROGRESS_KEY", "HEARTBEAT_TIMEOUT", "PROCESSORS",
    "STATUS_IDLE", "STATUS_RUNNING", "STATUS_DONE", "STATUS_FAILED", "STATUS_STALE",
    "start", "update", "finish", "clear", "read",
]
