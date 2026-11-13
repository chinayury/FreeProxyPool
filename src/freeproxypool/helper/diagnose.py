# -*- coding: utf-8 -*-
"""单代理深度诊断。

回答一个具体问题：**"这个代理为什么不能用？"**

采集 → 入池 → 校验的链路只回答"能不能进池"，一旦某个代理被剔除，
就只剩下翻 validator 日志这一条路。调用方反馈"某个代理坏了"时更是无从下手。

本模块跑一遍完整的诊断链路并给出结论：

    TCP 连通  →  HTTP 功能（延迟 / 出口 IP / 匿名度）  →  HTTPS(CONNECT) 隧道
              →  地域  →  最终判定

最终判定分三档：

===============  ==========================================
``excellent``   HTTP 与 HTTPS 均可用
``limited``     仅 HTTP 可用，不支持 CONNECT 隧道
``unusable``    完全不可用（TCP 不通，或 HTTP 请求失败）
===============  ==========================================

## 与 helper/check.py 的分工

* :mod:`helper.check` 是**批量入池**流程，关心"能不能进池"，结果写 Redis；
* 本模块是**单条排查**工具，关心"为什么不行"，结果只返回给调用方，不落库。

探针实现**直接复用** :mod:`helper.check` 的同一套函数，
避免出现第二份"看起来一样但行为不同"的探针代码 ——
那种重复正是最常见的"两处实现慢慢漂移"的来源。
"""
import time

from aiohttp import ClientTimeout

from ..core.iputil import parse_host_port
from ..core.logging import get_logger
from .check import (UnsupportedProtocolError, get_loop, http_probe, https_probe,
                           probe_session, tcp_check, _get_session)
from .proxy import normalize_protocol
from .region import get_resolver, judge_anonymous

log = get_logger("diagnose", to_file=False)

#: 三档判定
VERDICT_EXCELLENT = "excellent"
VERDICT_LIMITED = "limited"
VERDICT_UNUSABLE = "unusable"

#: 判定文案（对外解释用）
VERDICT_TEXT = {
    VERDICT_EXCELLENT: "excellent（HTTP + HTTPS 均可用）",
    VERDICT_LIMITED: "limited（仅 HTTP 可用，不支持 CONNECT 隧道）",
    VERDICT_UNUSABLE: "unusable（完全不可用）",
}

#: 匿名度标签 → 中文说明
ANONYMOUS_TEXT = {
    "elite": "高匿（出口 IP == 代理 IP，且无 Via / X-Forwarded-For 头）",
    "anonymous": "匿名（隐藏了真实 IP，但响应头暴露了「用了代理」）",
    "transparent": "透明（向目标站暴露了客户端 IP）",
    "": "未知（未能取得出口 IP）",
}


def _ms(seconds: float) -> float:
    """秒转毫秒（保留一位小数）。"""
    return round(float(seconds or 0.0) * 1000.0, 1)


async def diagnose(proxy_str: str, http_url: str, https_url: str,
                   timeout: float = 10.0, tcp_timeout: float = 3.0,
                   with_region: bool = True, protocol: str = "http") -> dict:
    """诊断单个代理，返回结构化结果。

    :param proxy_str: ``ip:port``（调用方应先做格式校验）
    :param http_url: HTTP 探针目标
    :param https_url: HTTPS 探针目标
    :param timeout: 单次 HTTP/HTTPS 探针超时（秒）
    :param tcp_timeout: TCP 连通超时（秒）
    :param with_region: 是否解析代理 IP 的国家码
    :param protocol: 传输协议（``http`` / ``socks4`` / ``socks5``）—— P0-1
    :return: 结果字典（结构见模块文档）
    """
    ip, port = parse_host_port(proxy_str)
    protocol = normalize_protocol(protocol)
    result = {
        "proxy": proxy_str,
        "ip": ip or "",
        "port": int(port or 0),
        "protocol": protocol,
        "tcp": {"ok": False, "elapsed_ms": 0.0},
        "http": {"ok": False, "elapsed_ms": 0.0, "speed": 0.0,
                 "origin_ip": "", "anonymous": ""},
        "https": {"ok": False, "elapsed_ms": 0.0},
        "region": "",
        "verdict": VERDICT_UNUSABLE,
        "verdict_text": VERDICT_TEXT[VERDICT_UNUSABLE],
    }
    if not ip or not port:
        result["error"] = "代理地址格式非法"
        return result

    # ---------- 第一层：TCP 连通 ----------
    started = time.perf_counter()
    tcp_ok = await tcp_check(ip, port, tcp_timeout)
    result["tcp"]["ok"] = tcp_ok
    result["tcp"]["elapsed_ms"] = _ms(time.perf_counter() - started)
    if not tcp_ok:
        result["error"] = "TCP 端口不通"
        return result

    # ---------- 第二层：HTTP 功能 + HTTPS 隧道 ----------
    shared = await _get_session(4)
    client_timeout = ClientTimeout(total=int(max(1, timeout)))

    try:
        async with probe_session(proxy_str, protocol, shared,
                                int(max(1, timeout))) as (sess, use_proxy):
            started = time.perf_counter()
            ok, speed, origin, headers = await http_probe(
                sess, proxy_str, http_url, client_timeout,
                use_proxy_arg=use_proxy)
            result["http"]["ok"] = ok
            result["http"]["elapsed_ms"] = _ms(time.perf_counter() - started)
            if ok:
                result["http"]["speed"] = round(float(speed or 0.0), 4)
                result["http"]["origin_ip"] = origin
                result["http"]["anonymous"] = judge_anonymous(ip, origin, headers)

            started = time.perf_counter()
            https_ok = await https_probe(sess, proxy_str, https_url,
                                         client_timeout,
                                         use_proxy_arg=use_proxy)
            result["https"]["ok"] = https_ok
            result["https"]["elapsed_ms"] = _ms(time.perf_counter() - started)
    except UnsupportedProtocolError as exc:
        result["error"] = str(exc)
        return result

    # ---------- 判定 ----------
    if result["http"]["ok"] and result["https"]["ok"]:
        verdict = VERDICT_EXCELLENT
    elif result["http"]["ok"]:
        verdict = VERDICT_LIMITED
    else:
        verdict = VERDICT_UNUSABLE
    result["verdict"] = verdict
    result["verdict_text"] = VERDICT_TEXT[verdict]
    if verdict == VERDICT_UNUSABLE and not result.get("error"):
        result["error"] = "经代理请求 HTTP 目标失败"

    # ---------- 地域 ----------
    if with_region:
        try:
            mapping = await get_resolver().resolve_many([ip])
            result["region"] = mapping.get(ip, "")
        except Exception as exc:
            # 地域解析失败不影响诊断结论
            log.debug("地域解析失败：%s", type(exc).__name__)
    return result


def diagnose_sync(proxy_str: str, http_url: str, https_url: str,
                  timeout: float = 10.0, tcp_timeout: float = 3.0,
                  with_region: bool = True, protocol: str = "http") -> dict:
    """同步入口（CLI 用）。复用进程内事件循环。"""
    loop = get_loop()
    return loop.run_until_complete(diagnose(
        proxy_str, http_url, https_url, timeout=timeout,
        tcp_timeout=tcp_timeout, with_region=with_region, protocol=protocol))


def format_text(result: dict) -> str:
    """把诊断结果渲染成人读的一行行文本。"""
    lines = ["代理      %s" % result.get("proxy", "")]

    protocol = result.get("protocol")
    if protocol:
        lines.append("协议      %s" % protocol)

    tcp = result.get("tcp") or {}
    lines.append("TCP       %s  %s ms" % (
        "[OK]  " if tcp.get("ok") else "[FAIL]",
        tcp.get("elapsed_ms", 0)))

    http = result.get("http") or {}
    if http.get("ok"):
        lines.append("HTTP      [OK]  %s ms  出口 IP %s  匿名度 %s" % (
            http.get("elapsed_ms", 0),
            http.get("origin_ip") or "-",
            http.get("anonymous") or "unknown"))
    else:
        lines.append("HTTP      [FAIL]  %s ms" % http.get("elapsed_ms", 0))

    https = result.get("https") or {}
    lines.append("HTTPS     %s  %s ms" % (
        "[OK]  " if https.get("ok") else "[FAIL]",
        https.get("elapsed_ms", 0)))

    lines.append("国家      %s" % (result.get("region") or "-"))

    anonymous = (result.get("http") or {}).get("anonymous") or ""
    if anonymous:
        lines.append("匿名度说明 %s" % ANONYMOUS_TEXT.get(anonymous, anonymous))

    lines.append("判定      %s" % result.get("verdict_text", ""))
    if result.get("error"):
        lines.append("原因      %s" % result["error"])
    return "\n".join(lines)


__all__ = [
    "diagnose", "diagnose_sync", "format_text",
    "VERDICT_EXCELLENT", "VERDICT_LIMITED", "VERDICT_UNUSABLE",
    "VERDICT_TEXT", "ANONYMOUS_TEXT",
]
